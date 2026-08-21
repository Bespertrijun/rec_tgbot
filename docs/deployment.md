# Production Deployment

The production host runs the published image with Docker Compose and keeps its
configuration and Cookie outside the repository. The GitHub deployment job uploads only
`docker-compose.yml`; it never uploads or overwrites the server `.env` or the Cookie
volume. Because the production file uses Compose's default filename, commands on the
server can use `docker compose pull` and `docker compose up -d` without an explicit
`-f` argument.

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
   `/var/lib/reclaude-bot/cookies/cookies.json` and mounts that directory on the persistent
   `reclaude-cookies` named volume; do not add this variable to `.env`. The bot uses a
   normal Linux Chrome browser User-Agent by default, so `RECLAUDE_USER_AGENT` does not
   need to be configured.
3. From the fixed egress IP, use the dedicated account credentials in the server `.env`.
   The Bot sends `POST /api/auth/login` from this host only during an explicit
   `/account`; successful `Set-Cookie` values are persisted in the `0600` cookie
   jar and then verified with `/api/app/me`. Do not paste the password or Cookie into Git,
   tickets, chat, shell history, or CI logs. A credential or Cookie that has appeared in a
   log or message is compromised and must be rotated.
4. The published GHCR image is public and can be pulled anonymously. The production
   server does not need a registry token or `docker login`; `docker compose pull` uses
   the default public image directly.

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

## Backups and recovery

Take an encrypted PostgreSQL dump before upgrades and retain at least 30 days of dumps.
For a host with the Compose stack running, a dump can be created without exposing the
database port:

```sh
mkdir -p backups
docker compose exec -T db \
  pg_dump -U bot -d reclaude --format=custom > "backups/reclaude-$(date -u +%Y%m%dT%H%M%SZ).dump"
```

Keep dumps and the Cookie volume access-restricted. After restoring a dump, the service
must remain write-disabled while operators verify the account, perform a full members
snapshot and cycle-baseline reconcile, and then explicitly send `/account`.
See [the Cookie recovery runbook](cookie-runbook.md) and [operations notes](operations.md)
for the failure gates and restore procedure.
