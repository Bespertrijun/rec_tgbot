# Operations

## Backups

Use `scripts/backup.sh` daily and retain encrypted PostgreSQL dumps for at least 30 days.
Cookie jars are deliberately excluded from database backups. Target RPO is 24 hours and
RTO is 4 hours. Run `scripts/restore.sh` monthly in an isolated database, then verify the
selected Reclaude account, device-cycle evidence, and unresolved REVOKE actions before
enabling device quota tasks. Do not recreate device balances from the legacy upstream
member snapshot; see the [device-ledger rollout gate](device-ledger-rollout.md).

## Account and recovery

In an administrator private chat, `/task [name]` reports task state and the live account
inventory, including account IDs, full emails, lifecycle, and health. `/account [name]`
remains a compatibility alias to the same handler and is no longer shown as a standalone
menu item. Account inventory failures do not hide task details. `/status` keeps each
user's personal device quota separate from the organization account summary; the latter
contains only the masked current account email, bound status, 5-hour and 7-day windows,
reset times, managed-device estimate, and snapshot time. The `/me` lookup is scoped to
the configured organization ID.

The configured organization is expected to have exactly one healthy bound account. The
device loop, task start, and authorization checks discover that account from the live
organization inventory; there is no manual account-selection command. When its
`account_id` changes, the write latch closes, active device totals are captured as the
new cycle baselines, and the task starts a new local accounting generation. Previous
cycles remain history, a task that was STOPPED stays stopped, and a task that was RUNNING
resumes only after the reset succeeds. An ambiguous or failed inventory leaves the old
identity and history intact and retries with writes closed.

A `401` forces all quota tasks to STOPPED and closes the write latch. After repairing the
dedicated session, run `/task` (or the compatibility alias `/account`), verify the
persisted account, and explicitly start only the tasks that should resume.
`/recovery_enable` rechecks the live bound account and leaves all tasks STOPPED. Normal
API calls do not silently retry password login.

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

`/taskusers` also shows the selected account's 5-hour and 7-day windows. Its optional
weekly estimate is `confirmed device-ledger spend in the current local period * 100 /
live 7-day utilization percent`. The numerator is summed once per user ledger for the
whole task and period, so it includes associations that ended during the period and
their confirmed post-revoke sample; it is independent of the current member scope and
does not sum device-history totals or quota transfers. The display labels the result as
an estimate based on managed device spend, not an upstream account limit.

Unknown or review-state users do not hide other known ledger amounts. Missing ledgers,
old samples, pending final samples, and imported spend are included whenever their
user ledger already has a finite confirmed amount; unknown amounts are skipped rather
than replaced with zero. The estimate is hidden only when there is no known spend,
spend is zero, utilization is zero or invalid, the live account or reset time does not
match the saved cycle, or the cycle changes across the request. Account-window lookup
failures only remove this summary; the member ledger rows remain available. An empty
current member scope still receives the account summary.

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
