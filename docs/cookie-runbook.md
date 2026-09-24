# Dedicated Reclaude session recovery runbook

1. Prefer the dedicated `RECLAUDE_LOGIN_EMAIL` and `RECLAUDE_LOGIN_PASSWORD` in the server
   `.env`; configure both or neither. The Bot sends the login request from the production
   fixed egress IP only when an administrator invokes `/account` (the legacy
   `/recovery_enable` alias is also accepted). MFA responses fail
   closed and require an operator-managed recovery path.
2. `RECLAUDE_SESSION_COOKIE` remains an optional compatibility or initial fallback. Do not
   reuse a browser session, any Cookie copied from chat, or an `rck_` API key. A valid existing
   cookie jar is reused before attempting password login.
   Existing flat `cookies.json` files require no manual conversion: the Bot scopes loaded
   values to the configured Reclaude hostname and `/` path. When Reclaude refreshes a
   recognized session cookie, the response value becomes authoritative and older domain/path
   variants are removed before the jar is persisted.
3. Keep the external cookie jar outside the repository with mode `0600` and readable only by
   the Bot user. Host mode `0600` does not prevent `root`, a rootful Docker daemon, or a
   privileged container process from reading the `.env` or jar.
4. Run `/account` to authenticate and inspect the live `/accounts` inventory. This is
   read-only and still displays a banned `current_account` so an operator can choose a
   healthy bound account. Run `/use <account_id>` for the selected record; it validates the
   lifecycle, health, and ID, then reconciles `GET /api/app/me`, the weekly cycle, and
   `/members` before persisting the selection. A successful switch also zeroes current-cycle
   usage baselines and carries still-active revocations into the current cycle, so the poll
   loop restores removed members automatically, and resumes any tasks that were RUNNING
   before the switch (re-opening writes); when no task was running, writes remain disabled
   until `/starttask`. The legacy
   `/recovery_enable` alias retains the single-bound-account discovery flow. Account IDs
   come from each record's `account_id` (the separate database `id` is never used); no
   account ID or email mask is configured in `.env`.
5. Inspect pending quota revocations. `/use` resumes previously RUNNING tasks and with them
   quota enforcement writes; `/starttask` remains the explicit per-task write switch when no
   task was running.
   `/stoptask` stops the task; writes close once no task remains
   `RUNNING`, while the quota loop keeps syncing usage data. The first `401` forces every durable task back
   to `STOPPED` and requires this procedure; normal polling and write requests never retry the
   password login automatically.
