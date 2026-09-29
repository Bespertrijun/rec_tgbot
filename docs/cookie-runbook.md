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
   read-only and still displays a banned `current_account`. Device mode discovers the
   single healthy bound record automatically from its `account_id`; no account ID or email
   mask is configured in `.env`. The first cycle records that identity without clearing
   existing history. When the bound ID changes, background reconciliation closes quota
   writes, captures device baselines, and starts a new cycle before restoring only tasks
   that were RUNNING. An ambiguous or failed inventory remains paused for retry.
5. After a `401`, fix the dedicated session, run `/account`, verify the persisted account,
   and explicitly run `/starttask` for each task that should resume. The first `401` forces
   tasks to `STOPPED`; normal request paths never retry password login automatically. A
   timed-out REVOKE is checked against the organization's device list by the background
   reconciliation loop. This covers user, administrator, and quota revocations across
   cycle boundaries. UNKNOWN AUTH remains reserved and is never inferred from a device
   list; do not resend its authorization link. `/stoptask` pauses automatic quota actions
   while device sampling continues. `/stopstats` pauses the usage and cycle sampling loop.
