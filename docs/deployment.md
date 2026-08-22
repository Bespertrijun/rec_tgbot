# Production Deployment

The bot runtime is PostgreSQL-only. `DATABASE_URL` must be an async SQLAlchemy URL with the
`postgresql+asyncpg://` scheme; startup rejects SQLite and synchronous PostgreSQL URLs. SQLite
remains available only for isolated test fixtures and migration checks.

The production host runs the published image with Docker Compose and keeps its
configuration and runtime data in the deployment directory. The GitHub deployment job
uploads only `docker-compose.yml`; it never uploads or overwrites the server `.env` or
the `data/` directory. Because the production file uses Compose's default filename,
commands on the server can use `docker compose pull` and `docker compose up -d` without
an explicit `-f` argument. Relative mounts are resolved beside that Compose file, so a
deployment at `/srv/reclaude-bot` stores PostgreSQL data in
`/srv/reclaude-bot/data/postgres`, the Cookie jar in
`/srv/reclaude-bot/data/cookies`, and rotated JSON logs in
`/srv/reclaude-bot/data/logs`.

## First-time server setup

1. Give the host a fixed public egress IP. Use that same IP for the dedicated Reclaude
   operator account when creating the session Cookie.
2. Create a private deployment directory and the server-only environment file:

   ```sh
   DEPLOY_USER=deploy
   sudo install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 700 /srv/reclaude-bot
   sudo install -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 600 /dev/null /srv/reclaude-bot/.env
   sudo -iu "$DEPLOY_USER"
   vi /srv/reclaude-bot/.env
   exit
   ```

   The directory and `.env` must be owned by `DEPLOY_USER`; do not leave them
   root-owned if the deployment account is expected to run Compose. Keep the file mode
   at `0600`: this protects the credentials from other non-root host users, but does not
   protect them from `root`, a Docker daemon running as root, or a container process with
   equivalent host privileges.
   `DEPLOY_USER` must also be able to run Docker and Compose: use rootless Docker for
   that user, or add it to the host's `docker` group according to your security policy.
   Verify this as the deployment user with `docker compose version` before configuring
   the GitHub job.

   Start from [.env.example](../.env.example). Set a strong, unique
   `POSTGRES_PASSWORD`, a matching `DATABASE_URL` using the Compose service name `db`,
   the Telegram settings, `RECLAUDE_LOGIN_EMAIL`, and `RECLAUDE_LOGIN_PASSWORD`. The
   account ID is discovered during `/account` (the legacy `/recovery_enable` alias is also
   accepted); no account email mask is configured
   in `.env`. `RECLAUDE_SESSION_COOKIE` remains an optional compatibility or initial
   fallback when a valid persistent cookie jar is not already present. Configure the login
   email and password together or leave both empty. MFA login responses fail closed because
   this deployment does not automate MFA.
   The Compose file fixes `RECLAUDE_COOKIE_JAR_PATH` to
   `/var/lib/reclaude-bot/cookies/cookies.json` and bind-mounts that directory from
   `./data/cookies`; do not add this variable to `.env`. It also fixes `LOG_FILE_PATH`
   to `/var/lib/reclaude-bot/logs/reclaude-bot.log` and bind-mounts `./data/logs`.
   Persistent file logging is rotated and bounded; stdout remains available through
   `docker logs`. The bot uses a normal Linux Chrome browser User-Agent by default,
   so `RECLAUDE_USER_AGENT` does not need to be configured.
3. From the fixed egress IP, use the dedicated account credentials in the server `.env`.
   The Bot sends `POST /api/auth/login` from this host only during an explicit
   `/account`; successful `Set-Cookie` values are persisted in the `0600` cookie
   jar and then verified with `/api/app/me`. Do not paste the password or Cookie into Git,
   tickets, chat, shell history, or CI logs. A credential or Cookie that has appeared in a
   log or message is compromised and must be rotated.
4. The published GHCR image is public and can be pulled anonymously. The production
   server does not need a registry token or `docker login`; `docker compose pull` uses
   the default public image directly.
5. Initialize the host directories before the first start. Run these commands from
   the directory containing `docker-compose.yml` (for example, `/srv/reclaude-bot`):

   ```sh
   install -d -m 700 data data/postgres data/cookies data/logs
   chmod 700 data data/postgres data/cookies data/logs
   ```

   The published bot image runs as UID `10001` (`bot`). The numeric UID/GID of
   `postgres:16-alpine` is image-specific, so read it from the exact image before
   assigning the bind directory. With the usual rootful Docker daemon:

   ```sh
   POSTGRES_UID="$(docker compose run --rm --no-deps --user 0 --entrypoint id db -u postgres)"
   POSTGRES_GID="$(docker compose run --rm --no-deps --user 0 --entrypoint id db -g postgres)"
   sudo chown "$POSTGRES_UID:$POSTGRES_GID" data/postgres
   sudo chown -R 10001:10001 data/cookies data/logs
   ```

   Do not change the bot to run as root. On rootless Docker, host numeric ownership is
   user-namespace mapped and the `sudo chown` values above are not the right host IDs.
   Let the same Docker daemon perform the ownership change instead (after `docker
   compose pull`):

   ```sh
   docker compose run --rm --no-deps --user 0 --entrypoint chown bot \
     -R 10001:10001 /var/lib/reclaude-bot/cookies /var/lib/reclaude-bot/logs
   POSTGRES_UID="$(docker compose run --rm --no-deps --user 0 --entrypoint id db -u postgres)"
   POSTGRES_GID="$(docker compose run --rm --no-deps --user 0 --entrypoint id db -g postgres)"
   docker compose run --rm --no-deps --user 0 --entrypoint chown db \
     "$POSTGRES_UID:$POSTGRES_GID" /var/lib/postgresql/data
   ```

   These commands work with rootless Docker because UID `0` is confined to the
   daemon's user namespace; the host sees the corresponding subordinate IDs. If a
   distribution uses a different Postgres image or remaps users, the same `id` commands
   use the actual image values. The recursive bot ownership command also repairs files
   left root-owned by an earlier container; the bot preserves the Cookie jar's `0600`
   mode when it writes it. Keep the parent `data` directory accessible to the deployment
   user so Compose can mount it; keep `data/postgres`, `data/cookies`, and `data/logs`
   mode `0700`.

## GitHub settings

Create a `production` environment and require its approval when appropriate. Add these
repository/environment secrets exactly as named:

| Secret | Value |
| --- | --- |
| `DEPLOY_HOST` | Fixed-IP deployment host name or address |
| `DEPLOY_PORT` | SSH port, normally `22` |
| `DEPLOY_USER` | Restricted deployment user |
| `DEPLOY_SSH_KEY` | Private SSH key for that user |
| `DEPLOY_KNOWN_HOSTS` | Pinned `known_hosts` line(s) for the host |

Add these variables:

| Variable | Value |
| --- | --- |
| `DEPLOY_ENABLED` | `true` to enable the production job; anything else disables it |
| `DEPLOY_PATH` | Existing absolute directory, for example `/srv/reclaude-bot` |

The image publish job uses the workflow `GITHUB_TOKEN` with `packages:write`. This token
is used only by GitHub Actions to publish the image; it is never copied to the host, and
the production server does not need a registry token.

Before saving `DEPLOY_KNOWN_HOSTS`, verify the server's SSH host key fingerprint through
an independent trusted channel. For example, compare the output of
`ssh-keygen -lf <(ssh-keyscan -t ed25519 "$DEPLOY_HOST" 2>/dev/null)` with the provider's
console or an out-of-band administrator record. Store only the verified `known_hosts`
line(s); do not accept an unexpected first-connection fingerprint.

## First deployment

The target directory must already contain the server `.env`. From a checked-out copy of
the repository, an operator can perform the first deployment with:

```sh
scp -P "$DEPLOY_PORT" docker-compose.yml \
  "$DEPLOY_USER@$DEPLOY_HOST:$DEPLOY_PATH/docker-compose.yml.new"
ssh -p "$DEPLOY_PORT" "$DEPLOY_USER@$DEPLOY_HOST" \
  "cd '$DEPLOY_PATH' && mv -f -- docker-compose.yml.new docker-compose.yml && docker compose pull bot && docker compose up -d"
```

Before that first `docker compose up -d`, complete the `data/` directory initialization
and ownership commands in step 5 above. An upgrade replaces only the Compose file and
reuses the same bind directories; rerun `docker compose config --quiet`, check their
ownership/mode, then run `docker compose pull bot && docker compose up -d
--remove-orphans`. This change does not copy data from the old named volumes. If the
currently running stack still contains important data in a named volume, stop and
perform a separately reviewed backup/migration before switching mounts; do not assume
the new empty bind directory contains that data.

The automated job follows the same sequence and pins `BOT_IMAGE` to the commit SHA. It
only transfers the Compose file and atomically replaces the previous copy. Verify `docker compose ps`
shows both `db` and `bot` running. On every initial start, restart, upgrade, rollback, or
restore, complete the read-only checks and send `/account` from an administrator
account before allowing quota writes.

## Upgrade and rollback

Every successful `main` build publishes both `ghcr.io/bespertrijun/rec_tgbot:latest` and
`ghcr.io/bespertrijun/rec_tgbot:sha-<commit>`. The deployment job uses the immutable SHA
tag. To manually select a version without changing `.env`:

```sh
export BOT_IMAGE=ghcr.io/bespertrijun/rec_tgbot:sha-COMMIT_SHA
docker compose pull bot
docker compose up -d --no-deps bot
docker compose ps
```

Use the previous known-good SHA for a rollback. Re-run the health/reconcile checks and
`/account` after either operation. Never roll back by copying a Cookie or `.env`
from another host.

## Inspecting and retiring old named volumes

The previous Compose file created project-prefixed named volumes. List the exact names
before touching them:

```sh
docker volume ls --format '{{.Name}}' | grep -E '(^|_)reclaude-cookies$' || true
docker volume ls --format '{{.Name}}' | grep -E '(^|_)postgres-data$' || true
```

For a candidate Cookie volume, inspect its metadata and contents read-only (replace the
placeholder with the exact name printed above):

```sh
OLD_COOKIE_VOLUME='project_reclaude-cookies'
docker volume inspect "$OLD_COOKIE_VOLUME"
docker run --rm --mount "source=$OLD_COOKIE_VOLUME,target=/volume,readonly" alpine:3.20 \
  sh -c 'find /volume -mindepth 1 -maxdepth 2 -print'
```

Only after confirming that the old Cookie volume contains no data you still need may it
be removed:

```sh
docker volume rm "$OLD_COOKIE_VOLUME"
```

Do not remove any `postgres-data` volume as part of this change. Its contents are the
database and must be retained until a verified backup and an explicit migration or
retirement decision exists. `docker compose down -v` is also unsafe here because it can
remove named database and Cookie volumes.

## Backups and recovery

Take an encrypted PostgreSQL dump before upgrades and retain at least 30 days of dumps.
For a host with the Compose stack running, a dump can be created without exposing the
database port:

```sh
mkdir -p backups
docker compose exec -T db \
  pg_dump -U bot -d reclaude --format=custom > "backups/reclaude-$(date -u +%Y%m%dT%H%M%SZ).dump"
```

Keep dumps, `data/cookies`, and `data/logs` access-restricted. After restoring a dump, the service
must remain write-disabled while operators verify the account, perform a full members
snapshot and cycle-baseline reconcile, and then explicitly send `/account`.
See [the Cookie recovery runbook](cookie-runbook.md) and [operations notes](operations.md)
for the failure gates and restore procedure.
