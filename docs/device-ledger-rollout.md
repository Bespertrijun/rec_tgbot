# Device Ledger Rollout Gate

The device-ledger runtime uses local `User.id` values, device associations, and cumulative
device usage snapshots. It is not a drop-in continuation of the legacy upstream-member
balance model. No legacy balance import or account-switch migration is implemented.

On 2026-09-28 the operator confirmed that legacy remaining balances are retired at
cutover. Preserve the old database as a backup; do not import its balances into device
ledgers. See [the Chinese cutover checklist](device-cutover-zh.md).

Do not deploy this runtime over a database whose existing quota balances must remain
spendable until a reviewed conversion or retirement plan exists. Starting the new image
runs forward-only schema migrations; it does not populate device ledgers from
`CycleBaseline`, `QuotaAdjustment`, or legacy allocation state. Old quota tasks without a
`DeviceTaskScope` are hidden from device-mode task lists and cannot be started, edited, or
deleted through the device task service.

The device Compose channel has a distinct project name, deployment directory, and
`data-device/` storage. It does not automatically stop or take over a legacy bot process.
Before starting the device bot with the same Telegram token, stop the old bot container or
process completely. `/stoptask` and `/stopstats` do not stop Telegram polling; the same token
must not be polled by both versions at once. This workspace change does not stop or inspect
any real process.

Before enabling a device-mode release:

1. Take and verify a PostgreSQL backup, then test restoration in an isolated environment.
2. Apply the confirmed retirement policy: legacy remaining balances are not spendable
   in the new runtime. Keep the old database backup; no destructive balance deletion or
   conversion is needed. Do not treat a device snapshot as an old upstream total.
3. Stop the old bot process completely before starting the device bot. Stop legacy quota
   writes and statistics before the runtime cutover. Review externally associated devices
   and reconcile their ownership before users receive new auth links.
4. Apply the migration chain only in the approved target environment after the backup and
   balance decision are complete. This workspace change has not run migrations against a
   production database and has not been deployed.
5. In device mode, select the initial healthy account with `/use <account_id>`, create a
   scoped task with `/newtask`, and use `/member` to obtain local numeric IDs for
   `/addtaskmember` and `/deletetaskmember`. Verify `/sync` and `/taskusers` before starting
   the task with `/starttask`.

`/use` permits initial selection and revalidation of the selected account. Selecting a
different account is rejected; it does not reset current usage, carry balances, or resume
tasks. Account replacement requires a separate reviewed cutover that accounts for every
device association and unresolved REVOKE. UNKNOWN AUTH is intentionally not inferred from
the device inventory and must not be retried with the same authorization link.

`/ban` disables a local user but does not revoke that user's device. `/unbind` refuses while
an open device association exists; use `/deauthuser <email>` and wait for the
confirmed revoke before unbinding. A timed-out manual revoke is checked from the complete
organization device list by the background reconciliation loop. Failed usage samples
remain pending for later sampling; no age-based cutoff converts them to zero usage.
