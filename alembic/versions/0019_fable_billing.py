"""Account for Fable subscription usage and repair the currently open rounds."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa

from alembic import op
from reclaude_bot.domain.billing import fable_total_usd

revision = "0019_fable_billing"
down_revision = "0018_round_quota"
branch_labels = None
depends_on = None

_ZERO = Decimal("0")
_MONEY_LIMIT = Decimal("100000000")
_ESTIMATE_FIELDS = ("estimated_total_usd", "estimate_used_usd", "estimate_percent", "estimate_snapshot_at", "estimate_recorded_at")


def _endpoint_fable(bind, snapshots, association, total, sampled_at):
    if total is None or sampled_at is None:
        return None
    if total == _ZERO:
        return _ZERO
    rows = bind.execute(sa.select(snapshots).where(
        snapshots.c.org_id == association["org_id"],
        snapshots.c.device_id == association["device_id"],
        snapshots.c.range == "all",
        snapshots.c.sampled_at == sampled_at,
    )).mappings().all()
    amounts = set()
    for row in rows:
        payload = row["payload"]
        if row["total_usd"] != total or not isinstance(payload, dict) or payload.get("range") != "all":
            return None
        overview = payload.get("overview")
        if not isinstance(overview, dict) or isinstance(overview.get("total_usd"), bool):
            return None
        try:
            payload_total = Decimal(str(overview.get("total_usd")))
            if not payload_total.is_finite() or payload_total != total:
                return None
            amounts.add(fable_total_usd(payload_total, payload.get("models")))
        except (ValueError, InvalidOperation):
            return None
    return amounts.pop() if len(amounts) == 1 else None


def _invalid_evidence_at(segment):
    # A deployment timestamp would discard valid samples already waiting to be applied.
    if segment["latest_sampled_at"] is None and segment["confirmed_used_usd"] is None:
        return None
    return segment["latest_sampled_at"] or segment["baseline_captured_at"]


def _repair_segment(bind, segments, snapshots, segment, association):
    before = segment["confirmed_used_usd"]
    baseline = _endpoint_fable(bind, snapshots, association, segment["baseline_total_usd"], segment["baseline_captured_at"])
    latest = _endpoint_fable(bind, snapshots, association, segment["latest_total_usd"], segment["latest_sampled_at"])
    values = {"baseline_fable_usd": baseline, "latest_fable_usd": latest, "billing_valid": False}
    quality = segment["quality"]
    amount = before
    if baseline is not None and latest is not None:
        raw_delta = segment["latest_total_usd"] - segment["baseline_total_usd"]
        fable_delta = latest - baseline
        candidate = (segment["imported_used_usd"] or _ZERO) + raw_delta + fable_delta
        if raw_delta < 0 or fable_delta < 0 or candidate >= _MONEY_LIMIT or (before is not None and candidate < before):
            quality = "NEEDS_REVIEW"
        else:
            amount = candidate
            values["billing_valid"] = True
    elif before is not None or segment["latest_total_usd"] is not None:
        quality = "NEEDS_REVIEW"
    values["billing_invalid_at"] = _invalid_evidence_at(segment) if not values["billing_valid"] else None
    values.update(confirmed_used_usd=amount, quality=quality)
    bind.execute(segments.update().where(segments.c.id == segment["id"]).values(**values))
    return amount, quality


def _repair_current_rounds(bind, tables, now):
    cycles = tables["device_quota_cycles"]
    ledgers = tables["device_cycle_ledgers"]
    segments = tables["device_usage_segments"]
    associations = tables["device_associations"]
    snapshots = tables["device_usage_snapshots"]
    audits = tables["audit_logs"]
    current_ledgers = bind.execute(sa.select(ledgers).join(cycles, cycles.c.id == ledgers.c.cycle_id).where(
        cycles.c.status != "EXPIRED", cycles.c.started_at <= now, cycles.c.reset_at > now,
    )).mappings().all()
    for ledger in current_ledgers:
        rows = bind.execute(sa.select(segments).where(segments.c.ledger_id == ledger["id"]).order_by(segments.c.id)).mappings().all()
        amounts = []
        review_ids = []
        unknown = False
        for segment in rows:
            association = bind.execute(sa.select(associations).where(associations.c.id == segment["association_id"])).mappings().one()
            amount, quality = _repair_segment(bind, segments, snapshots, segment, association)
            if amount is not None:
                amounts.append(amount)
            else:
                unknown = True
            if quality == "NEEDS_REVIEW":
                review_ids.append(segment["id"])
            elif quality != "VERIFIED":
                unknown = True

        amount = sum(amounts, _ZERO) if amounts else None
        old_amount = ledger["confirmed_used_usd"]
        review = bool(review_ids) or ledger["quality"] == "NEEDS_REVIEW" or (unknown and bool(amounts))
        invalid_total = False
        if amount is not None and amount >= _MONEY_LIMIT:
            amount = old_amount
            review = True
            invalid_total = True
        if old_amount is not None and (amount is None or amount < old_amount):
            amount = old_amount
            review = True
            invalid_total = True
        if invalid_total:
            for segment in rows:
                bind.execute(segments.update().where(segments.c.id == segment["id"]).values(
                    billing_valid=False, billing_invalid_at=_invalid_evidence_at(segment),
                ))
            review_ids = [segment["id"] for segment in rows]
        quality = "NEEDS_REVIEW" if review else "VERIFIED" if rows and not unknown and amount is not None else "UNKNOWN"
        bind.execute(ledgers.update().where(ledgers.c.id == ledger["id"]).values(
            confirmed_used_usd=amount, quality=quality, updated_at=now,
        ))
        bind.execute(audits.insert().values(
            actor_type="SYSTEM", action="DEVICE_FABLE_BILLING_MIGRATED", target_type="DEVICE_CYCLE_LEDGER",
            target_id=str(ledger["id"]), result=quality, created_at=now,
            parameters_summary={
                "cycle_id": ledger["cycle_id"],
                "old_confirmed_used_usd": str(old_amount) if old_amount is not None else None,
                "new_confirmed_used_usd": str(amount) if amount is not None else None,
                "review_segment_ids": review_ids,
            },
        ))


def upgrade() -> None:
    with op.batch_alter_table("device_usage_segments") as batch:
        batch.add_column(sa.Column("billing_valid", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch.add_column(sa.Column("billing_invalid_at", sa.DateTime(timezone=True), nullable=True))
        for endpoint in ("baseline", "latest"):
            batch.add_column(sa.Column(f"{endpoint}_fable_usd", sa.Numeric(18, 10), nullable=True))
            batch.create_check_constraint(
                f"ck_device_usage_segments_{endpoint}_fable_range",
                f"{endpoint}_fable_usd IS NULL OR ({endpoint}_total_usd IS NOT NULL AND "
                f"{endpoint}_fable_usd >= 0 AND {endpoint}_fable_usd <= {endpoint}_total_usd)",
            )
    bind = op.get_bind()
    metadata = sa.MetaData()
    metadata.reflect(bind=bind, only=[
        "device_quota_cycles", "device_cycle_ledgers", "device_usage_segments", "device_associations", "device_usage_snapshots", "audit_logs",
    ])
    _repair_current_rounds(bind, metadata.tables, datetime.now(UTC))
    # Old estimates, including closed rounds, use API units and must not seed a new round.
    bind.execute(metadata.tables["device_quota_cycles"].update().values(**dict.fromkeys(_ESTIMATE_FIELDS)))


def downgrade() -> None:
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM device_usage_segments WHERE baseline_fable_usd IS NOT NULL OR latest_fable_usd IS NOT NULL LIMIT 1"
    )).first() is not None:
        raise RuntimeError("cannot downgrade 0019_fable_billing while subscription usage is recorded")
    with op.batch_alter_table("device_usage_segments") as batch:
        for endpoint in ("baseline", "latest"):
            batch.drop_constraint(f"ck_device_usage_segments_{endpoint}_fable_range", type_="check")
            batch.drop_column(f"{endpoint}_fable_usd")
        batch.drop_column("billing_valid")
        batch.drop_column("billing_invalid_at")
