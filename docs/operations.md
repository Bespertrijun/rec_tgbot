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

The device loop waits five minutes between rounds. A normal round fetches `/me` once
and usage once per active device (four devices means about five requests per five
minutes, excluding retries and manual commands). Authorization still checks `/me`
immediately. Initial authorization and pre-revoke samples remain immediate; confirmed
revocation schedules one follow-up at one hour, executed by the first due polling round.
Pending older staged follow-ups are consolidated into that schedule; completed history
is retained. Failed requests retain the normal retry policy. Quota enforcement also
runs on this five-minute loop, so it is not an instantaneous spending cutoff.

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


## Reset a device task's local quota period

In an administrator private chat, send `/reset <task-name>` (for example,
`/reset device`). If REC itself needs a quota reset, perform it there first. This
command only changes the bot's accounting; it does not request a REC quota reset,
approve devices, or revoke devices.

The bot reads the current account period and every still-active associated device's
`range=all` total before committing. It closes the old local period and creates a
new one ending at REC's reported weekly reset time. New usage is zero and each
user's effective allowance returns to the task's configured amount; old adjustments
and transfers remain in history and do not carry over. Active devices use the newly
collected totals as their baselines, so subsequent usage alone counts. Bindings,
member scope, task running/stopped state, and the write latch are preserved.

A failed required query or changed device/account state aborts the reset without
partially clearing users' balances. Resolve pending/unknown authorization or revoke
operations before resetting. A repeated delivery of the same Telegram command is
idempotent; sending a new `/reset` message intentionally starts another local period,
even within the same REC week. Historical ledgers remain available. Eligible users
previously revoked for quota receive the existing reauthorization notification on
the next statistics round; they must manually `/auth` again. If statistics are
paused, notifications wait until `/startstats` resumes the loop.
