# Reclaude Quota Bot

Single-process Telegram bot for binding local users, associating Reclaude devices, and
enforcing a per-user cycle quota from device usage snapshots. It uses PostgreSQL exclusively at runtime
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

The device runtime samples associated devices, preserves cumulative usage across device
changes, and retries failed samples without treating missing data as zero. Users bind with
`/bind email`, authorize in private chat with `/auth`, revoke with `/deauth`, inspect their
device and quota state with `/status`, and transfer quota with `/send @user amount` in a
managed group. Administrators manage devices and local user scopes with `/device`,
`/authuser`, `/deauthuser`, `/member`, and task commands. See
[`docs/device-ledger-rollout.md`](docs/device-ledger-rollout.md) before deploying this
runtime over data created by the legacy member-allocation quota loop; legacy balances are
not automatically migrated or reset.

组织账号用量快照在查询时按需刷新：距离上次尝试满 3 小时，才先调用 REC 刷新接口再读取快照。
没有查询就不刷新；失败（包括 429）后，3 小时内的查询也不会重试刷新。
尝试时间保存在数据库中，所有用户共享限频，重启不会提前重试；后台轮询不触发刷新。

首次从旧版切换请参考[中文上线步骤](docs/device-cutover-zh.md)：旧余额按已确认规则作废，
完整迁移旧库并保留用户绑定及群配置，同时说明已有设备关联的计费起点与待核对限制。
