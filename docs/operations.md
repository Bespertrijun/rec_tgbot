# Operations

## Backups

Use `scripts/backup.sh` daily and retain encrypted PostgreSQL dumps for at least 30 days.
Cookie jars are deliberately not included in database backups. Target RPO is 24 hours and RTO
is 4 hours. Run `scripts/restore.sh` monthly in an isolated database, then perform a full
members snapshot and cycle-baseline reconcile before enabling quota writes.

## Health gates

The service fails closed when a selected account is not live, its lifecycle is not `bound`, its
health is empty or `banned`, its `account_id` is missing or invalid, or a session request returns
`401`. `/account` is a read-only live inventory (including a banned current account); `/use <account_id>`
validates one selected record, reconciles it, and persists the selection without enabling writes.
Use `/starttask` as the single operator write switch, and `/stoptask` to close writes and the quota
loop together. The legacy `/recovery_enable` command retains the stricter single-bound-account
discovery flow but does not independently start the task. The account record's separate database
`id` is never used. No retry is attempted after the first `401` until the recovery runbook is
completed.

The quota task starts stopped on a new database. Its `RUNNING`/`STOPPED` state survives a restart;
a persisted `RUNNING` state resumes only after startup validates the selected account. The internal
`RecoveryGate` remains a fail-safe for startup validation, invalid accounts, and 401 recovery.
`/task` reports the state and best-effort tick health. `/member` lists all cached upstream member
emails and Reclaude user IDs for use with `/addtaskmember`. Member scope defaults to `ALL`; use
`/addtaskmember <reclaude_user_id> ...` to create an `ALLOWLIST`, `/deletetaskmember ...` to remove
IDs, and `/addtaskmember all` to clear the allowlist and return to `ALL`. Group onboarding remains
independent of the quota task.

Quota writes also require a fresh `/members` snapshot. The default maximum age is 90 seconds;
future-dated or older snapshots are ignored until the next normal members sync. Override this
with `MEMBER_SNAPSHOT_MAX_AGE_SECONDS` when the polling interval requires a different bound.

## Incident records

stdout contains JSON structlog events with request/job IDs and no Cookies, full emails, response
bodies, or authorization headers. Mutating actions are also immutable rows in `audit_logs`.
