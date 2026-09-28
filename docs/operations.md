# Operations

## Backups

Use `scripts/backup.sh` daily and retain encrypted PostgreSQL dumps for at least 30 days.
Cookie jars are deliberately excluded from database backups. Target RPO is 24 hours and
RTO is 4 hours. Run `scripts/restore.sh` monthly in an isolated database, then verify the
selected Reclaude account, device-cycle evidence, and unresolved REVOKE actions before
enabling device quota tasks. Do not recreate device balances from the legacy upstream
member snapshot; see the [device-ledger rollout gate](device-ledger-rollout.md).

## Account and recovery

`/account` reads the current login and live account inventory. `/use <account_id>` selects
the initial healthy bound account and leaves tasks STOPPED with the write latch closed.
Using `/use` for the already selected account revalidates it. Selecting a different
account is refused; the command does not reset balances, sync legacy members, or resume
tasks. Replacing an account requires a separately reviewed cutover for existing device
associations and unresolved REVOKE actions.

A `401` forces all quota tasks to STOPPED and closes the write latch. After repairing the
dedicated session, run `/account`, verify the persisted account, and explicitly start only
the tasks that should resume. `/recovery_enable` is a compatibility alias that validates
the selected account and leaves all tasks STOPPED. Normal API calls do not silently retry
password login.

An UNKNOWN AUTH remains reserved. Device-list queries never infer that an UNKNOWN AUTH
succeeded, and users must not resend its authorization link. For manual or automatic
REVOKE timeouts, the background reconciliation loop lists the full organization device
inventory and checks every armed unresolved revoke, including user/admin deauth and older
cycles. It never repeats the POST.

## Device tasks and quota

Each Reclaude organization has one device-scoped task. Create it with `/newtask <name>
[limit]`; use `/member` to find local numeric user IDs for `/addtaskmember` and
`/deletetaskmember`. The default scope is `ALL`; adding members changes it to `ALLOWLIST`,
deleting from `ALL` changes it to `EXCLUDE`, and `/addtaskmember <name> all` restores
`ALL`. `/task` reports task state and the latest device-cycle evidence. `/taskusers` shows
local users, their device association, and device-ledger usage.

`/starttask` opens automatic quota actions for the configured device task. `/stoptask`
stops those actions while device sampling continues. `/stopstats` pauses the shared cycle
and usage sampling loop; `/startstats` resumes it. With statistics stopped, old snapshots
are not treated as fresh evidence. Sampling failures stay pending and are retried without
an age cutoff or conversion to zero.

The task's write state and `RecoveryGate.write_enabled` both guard automatic quota revoke.
Manual `/deauth` and `/deauthuser` do not depend on the automatic write latch. Manual
`/auth` still requires current task scope and verified quota evidence. Admin
`/authuser <email> <device_id>` resolves an already-bound local user; it never creates one.
For the initial migration only, `/authuser <email> <device_id> [task] --used 500`
records $500 of confirmed cycle consumption and captures the device's current `all`
total as the baseline for subsequent increments. It does not add or subtract quota.
The user must have no prior device association and no current-cycle ledger. Repeating
the same active association and original amount is idempotent; a different amount is
rejected. A failed baseline query leaves no association or partial import. Ordinary
commands without `--used` retain their existing behavior. Imported consumption remains
part of the user's cycle total after device changes, and follows normal quota enforcement.
Admin `/deauthuser <email>` uses the same identity lookup. Ban only disables a local user
and does not revoke a device. `/unbind` refuses while an association is open; complete
`/deauthuser` and wait for confirmed revocation first.

Quota usage is cumulative across device changes. Transfers and limit increases do not
unlock a quota lock. A fresh, verified account/cycle reading with weekly use below 100%
allows final-24-hour authorization and exempts automatic quota deauth. The bot sends a
durable `/auth` notice only while the user remains bound, active, and in the current task
scope.

## Incident records

stdout contains JSON structlog events with request/job IDs and no Cookies, full emails,
response bodies, or authorization headers. Repeated device-usage GET failure logs include
only association/job IDs, the attempt count, and a fixed safe error code. Mutating actions
are also immutable rows in `audit_logs`.
