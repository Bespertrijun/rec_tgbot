# Dedicated Reclaude session recovery runbook

1. Prefer the dedicated `RECLAUDE_LOGIN_EMAIL` and `RECLAUDE_LOGIN_PASSWORD` in the server
   `.env`; configure both or neither. The Bot sends the login request from the production
   fixed egress IP only when an administrator invokes `/account` (the legacy
   `/recovery_enable` alias is also accepted). MFA responses fail
   closed and require an operator-managed recovery path.
2. `RECLAUDE_SESSION_COOKIE` remains an optional compatibility or initial fallback. Do not
   reuse a browser session, any Cookie copied from chat, or an `rck_` API key. A valid existing
   cookie jar is reused before attempting password login.
3. Keep the external cookie jar outside the repository with mode `0600` and readable only by
   the Bot user. Host mode `0600` does not prevent `root`, a rootful Docker daemon, or a
   privileged container process from reading the `.env` or jar.
4. Run `/account`. It authenticates first, verifies `GET /api/app/me`, then reads `/accounts`
   and `/members`; it verifies a single bound account whose health is non-empty and not
   `banned`, the unique `weekly_all` cycle, and member assignment state. The recovery command discovers the
   account's `account_id` from `/accounts` (the record's separate `id` is not used); no
   account ID or email mask is configured in `.env`.
5. Inspect pending quota revocations. An administrator may resume quota enforcement writes only
   after the checks pass. The first `401` opens the circuit again and requires this procedure;
   normal polling and write requests never retry the password login automatically.
