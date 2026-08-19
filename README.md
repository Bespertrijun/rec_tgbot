# Reclaude Quota Bot

Single-process Telegram bot for binding Reclaude members and enforcing a configurable
cycle quota from the upstream members snapshot. It uses a PostgreSQL database and a
dedicated, persistent Reclaude Cookie session. No SMTP, email verification, API key, or
port 25 is required.

```bash
cp .env.example .env
docker compose up --build
```

Before enabling writes in production, establish a dedicated Cookie session from the
fixed production egress IP and run the read-only health check. See
`docs/deployment.md`, `docs/cookie-runbook.md`, and `docs/operations.md`.

Production deployment, GHCR access, server `.env` handling, upgrades, rollbacks, and
backups are documented in [`docs/deployment.md`](docs/deployment.md). Never use a Cookie
that has been pasted into chat, a ticket, source control, or CI output.

The normal loop calls `/members` once per minute and keeps the latest member assignment
and cumulative usage locally. Users only need `/bind email` and `/status`; administrators
can change the live limit with `/setquota amount`. Account assignment itself remains an
operator action in the Reclaude console.
