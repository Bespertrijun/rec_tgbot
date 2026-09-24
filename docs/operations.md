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
validates one selected record, reconciles it, zeroes current-cycle usage baselines, and carries
still-active revocations into the current cycle so removed members are restored automatically once
enforcement resumes. Tasks that were RUNNING before the switch resume automatically (re-opening
writes); when no task was running, writes stay disabled until `/starttask`.
Use `/starttask <name>` as the operator write switch per task, and `/stoptask <name>` to stop one;
the write latch stays open while any task remains `RUNNING`, and the shared quota loop keeps
syncing usage data even after every task has stopped. `/startstats` and `/stopstats` control the
usage-sync loop itself; the stopped state persists across restarts, and while sync is off,
enforcement blocks itself on stale member snapshots. `/starttask` while sync is off still marks
the task `RUNNING` (with a bot warning) but starts no loop until `/startstats`. The legacy
`/recovery_enable` command retains the stricter single-bound-account discovery flow but does not
independently start any task. The account record's separate database `id` is never used. No retry
is attempted after the first `401` until the recovery runbook is completed.

Quota enforcement is organized as multiple named tasks (`quota_tasks`), each with its own per-user
limit and member scope. All tasks run against the single selected Reclaude account. Create one with
`/newtask <name> [limit]` (the limit defaults to the global `/setquota` value) and remove it with
`/deltatask <name>`. Tasks start stopped on a new database; the migration carries the previous
single-task switch and allowlist into a `default` task. A persisted `RUNNING` state survives a
restart and resumes only after startup validates the selected account. The internal `RecoveryGate`
remains a fail-safe for startup validation, invalid accounts, and 401 recovery, and stops all
running tasks when it trips. `/task` lists every task, `/task <name>` reports one task's state and
best-effort tick health. `/member` lists all cached upstream member emails and Reclaude user IDs for
use with `/addtaskmember`. Member scope defaults to `ALL`; use
`/addtaskmember <name> <reclaude_user_id> ...` to create an `ALLOWLIST`, `/deletetaskmember <name> ...`
to remove IDs, and `/addtaskmember <name> all` to clear the member list and return to `ALL`. Deleting
from an `ALL` (or `EXCLUDE`) task switches it to `EXCLUDE`: the listed IDs are excluded while every
other current and future upstream member stays covered; `/addtaskmember <name> <id>` on an `EXCLUDE`
task re-includes the ID. When only
one task exists its name may be omitted. Change a task's limit with `/settaskquota <name> <amount>`;
the new limit reconciles immediately from the local cache. `/taskusers <name>` works in admin
private chats only and lists every scoped member's current-cycle usage against the task limit. It
also reads `/api/app/me` live to show the account's 5-hour and 7-day window utilization with reset
times, plus an estimated 7-day window total (the locally cached cycle spend divided by the reported
utilization); that section degrades to a notice when the upstream read fails, while
the member list stays cache-only.
A member covered by several RUNNING tasks is enforced at the strictest (smallest) limit, because
upstream revocation is account-level. Group onboarding remains independent of the quota tasks.

Quota writes also require a fresh `/members` snapshot. The default maximum age is 90 seconds;
future-dated or older snapshots are ignored until the next normal members sync. Override this
with `MEMBER_SNAPSHOT_MAX_AGE_SECONDS` when the polling interval requires a different bound.

## Incident records

stdout contains JSON structlog events with request/job IDs and no Cookies, full emails, response
bodies, or authorization headers. Mutating actions are also immutable rows in `audit_logs`.
