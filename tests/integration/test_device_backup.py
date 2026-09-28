"""A real PostgreSQL dump/restore preserves device money, locks and pending work."""
import asyncio
import os
import shutil
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from tests.integration.test_device_migration import config_for, database_ddl, migration_url  # noqa: F401

pytestmark = pytest.mark.parametrize("migration_url", ["postgresql"], indirect=True)


async def seed_and_read(url, *, seed=False):
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            if seed:
                statements = [
                    "INSERT INTO users (id,telegram_user_id,email,email_normalized,binding_status,status,baseline_status,bound_at,updated_at) "
                    "VALUES (900,1900,'backup@example.invalid','backup@example.invalid','BOUND','ACTIVE','UNKNOWN',now(),now())",
                    "INSERT INTO device_task_scopes (task_id,org_id,scope_mode,created_at,updated_at) VALUES (1,178,'ALL',now(),now())",
                    "INSERT INTO devices (org_id,device_id,name,first_synced_at,last_synced_at) VALUES (178,44500,'backup',now(),now())",
                    "INSERT INTO device_associations (id,user_id,task_id,org_id,device_id,state,started_at,updated_at) "
                    "VALUES (900,900,1,178,44500,'UNKNOWN',now(),now())",
                    "INSERT INTO device_actions (association_id,kind,status,idempotency_key,target_device_id,attempt_count,created_at,updated_at) "
                    "VALUES (900,'REVOKE','UNKNOWN','backup-revoke',44500,1,now(),now())",
                    "INSERT INTO device_quota_cycles (id,task_id,started_at,reset_at,created_at,status,account_id) "
                    "VALUES (900,1,now()-interval '1 day',now()+interval '6 day',now(),'VERIFIED','7022')",
                    "INSERT INTO device_cycle_ledgers (id,user_id,cycle_id,task_id,confirmed_used_usd,quality,quota_locked_at,created_at,updated_at) "
                    "VALUES (900,900,900,1,700.1234567890,'VERIFIED',now(),now(),now())",
                    "INSERT INTO device_usage_snapshots (org_id,device_id,sample_key,range,total_usd,sampled_at,payload) "
                    "VALUES (178,44500,'backup-sample','all',700.1234567890,now(),'{\"overview\":{\"total_usd\":\"700.1234567890\"}}')",
                    "INSERT INTO device_usage_segments (association_id,user_id,task_id,ledger_id,started_at,baseline_total_usd,baseline_captured_at,"
                    "latest_total_usd,latest_sampled_at,confirmed_used_usd,imported_used_usd,quality) "
                    "VALUES (900,900,1,900,now(),200,now(),700.1234567890,now(),700.1234567890,200,'VERIFIED')",
                    "INSERT INTO device_quota_adjustments (ledger_id,amount_usd,reason,operator_telegram_id,operation_key,created_at) "
                    "VALUES (900,12.3456789012,'backup test',999,'backup-adjustment',now())",
                    "INSERT INTO device_notifications (ledger_id,event,status,attempt_count,created_at,updated_at,payload) "
                    "VALUES (900,'THRESHOLD_100','PENDING',1,now(),now(),'{\"used\":\"700.1234567890\"}')",
                    "INSERT INTO device_resample_jobs (association_id,sequence,run_after,status,attempt_count,created_at,updated_at) "
                    "VALUES (900,0,now(),'PENDING',1,now(),now())",
                ]
                for statement in statements:
                    await conn.execute(text(statement))
            result = {}
            for table in ("users", "device_task_scopes", "devices", "device_associations", "device_actions",
                          "device_quota_cycles", "device_cycle_ledgers", "device_usage_snapshots", "device_usage_segments", "device_quota_adjustments",
                          "device_notifications", "device_resample_jobs", "alembic_version"):
                result[table] = [dict(row) for row in (await conn.execute(text(f'SELECT * FROM "{table}"'))).mappings()]
            return result
    finally:
        await engine.dispose()


def test_dump_restore_preserves_device_ledger_and_pending_operations(migration_url, tmp_path):  # noqa: F811
    dump_bin, restore_bin = shutil.which("pg_dump"), shutil.which("pg_restore")
    if dump_bin is None or restore_bin is None:
        pytest.skip("PostgreSQL client tools required for backup rehearsal")
    command.upgrade(config_for(migration_url), "head")
    before = asyncio.run(seed_and_read(migration_url, seed=True))
    url = make_url(migration_url)
    env = os.environ.copy()
    env.update(PGHOST=url.query.get("host", url.host or "localhost"),
               PGPORT=str(url.query.get("port", url.port or 5432)), PGUSER=url.username or "postgres")
    if url.password:
        env["PGPASSWORD"] = url.password
    backup = tmp_path / "device-backup.dump"
    subprocess.run([dump_bin, "--format=custom", "--no-owner", "--file", str(backup), "--dbname", url.database],
                   env=env, check=True, capture_output=True, timeout=30)
    restored_name = "rec_restore_" + uuid4().hex + "_test"
    asyncio.run(database_ddl(url, f'CREATE DATABASE "{restored_name}"'))
    try:
        subprocess.run([restore_bin, "--no-owner", "--exit-on-error", "--dbname", restored_name, str(backup)],
                       env=env, check=True, capture_output=True, timeout=30)
        after = asyncio.run(seed_and_read(url.set(database=restored_name)))
        assert after == before
    finally:
        asyncio.run(database_ddl(url, f'DROP DATABASE "{restored_name}"'))
