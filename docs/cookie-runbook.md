# Dedicated Cookie recovery runbook

1. From the production fixed egress IP, sign in with a dedicated Reclaude operator account.
2. Export only the session Cookie into a root-owned secret file. Do not reuse a browser session,
   any Cookie copied from chat, or an `rck_` API key.
3. Stop the Bot, replace `RECLAUDE_SESSION_COOKIE` or the external Cookie jar, and ensure the jar
   is outside the repository with mode `0600` and readable only by the Bot user.
4. Start the container in read-only recovery mode. Run `GET /api/app/me`, `/accounts`, and
   `/members`; verify account status, the masked email, exactly one account, the unique
   `weekly_all` cycle, and member assignment state. The recovery command discovers that
   account's ID from `/accounts`; no account ID is configured in `.env`.
5. Run a full members snapshot and cycle-baseline reconcile. Inspect pending quota revocations.
6. An administrator may resume quota enforcement writes only after the checks pass. The first `401`
   opens the circuit again and requires this procedure.
