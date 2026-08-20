# Operations

## Backups

Use `scripts/backup.sh` daily and retain encrypted PostgreSQL dumps for at least 30 days.
Cookie jars are deliberately not included in database backups. Target RPO is 24 hours and RTO
is 4 hours. Run `scripts/restore.sh` monthly in an isolated database, then perform a full
members snapshot and cycle-baseline reconcile before enabling quota writes.

## Health gates

The service fails closed when the current account status is not `bound`, the `/accounts` response
does not contain exactly one bound record whose health is non-empty and not `banned`, its
`account_id` is missing or invalid, or a session request returns `401`. Account IDs are discovered
afresh by `/account`; the
record's separate database `id` is never used. No retry is attempted after the first `401` until
the recovery runbook is completed.

Quota writes also require a fresh `/members` snapshot. The default maximum age is 90 seconds;
future-dated or older snapshots are ignored until the next normal members sync. Override this
with `MEMBER_SNAPSHOT_MAX_AGE_SECONDS` when the polling interval requires a different bound.

## Incident records

stdout contains JSON structlog events with request/job IDs and no Cookies, full emails, response
bodies, or authorization headers. Mutating actions are also immutable rows in `audit_logs`.
