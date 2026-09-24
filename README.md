# Reclaude Quota Bot

Single-process Telegram bot for binding Reclaude members and enforcing a configurable
cycle quota from the upstream members snapshot. It uses PostgreSQL exclusively at runtime
through the required `postgresql+asyncpg://` URL and a
dedicated, persistent Reclaude session. No SMTP, email verification, API key, or port 25
is required.

```bash
cp .env.example .env
docker compose -f docker-compose.dev.yml up --build
```

Before enabling writes in production, configure the dedicated Reclaude login credentials
(or an optional compatibility Cookie), then run the read-only health check. See
`docs/deployment.md`, `docs/cookie-runbook.md`, and `docs/operations.md`.

Production deployment, GHCR access, server `.env` handling, upgrades, rollbacks, and
backups are documented in [`docs/deployment.md`](docs/deployment.md). Never use a Cookie
that has been pasted into chat, a ticket, source control, or CI output.

The normal loop calls `/members` once per minute and keeps the latest member assignment
and cumulative usage locally. Users only need `/bind email` and `/status`, and can
transfer part of their own cycle quota to another bound user with `/send @user amount`
inside a managed group; administrators
group members into named quota tasks with per-task limits (`/newtask`, `/settaskquota`) and
inspect one task's usage with `/taskusers`. Account assignment itself remains an
operator action in the Reclaude console.
