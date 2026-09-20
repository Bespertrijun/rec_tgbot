#!/bin/sh
set -eu

bot_uid=10001
bot_gid=10001

if [ "$(id -u)" -ne 0 ]; then
    printf '%s\n' 'reclaude-bot: startup must run as root to repair runtime directory ownership' >&2
    exit 1
fi

for runtime_dir in /var/lib/reclaude-bot/cookies /var/lib/reclaude-bot/logs /var/lib/reclaude-bot/update; do
    if ! mkdir -p "$runtime_dir"; then
        printf 'reclaude-bot: cannot create runtime directory %s\n' "$runtime_dir" >&2
        exit 1
    fi
    if ! chown -R "$bot_uid:$bot_gid" "$runtime_dir"; then
        printf 'reclaude-bot: cannot assign ownership for %s\n' "$runtime_dir" >&2
        exit 1
    fi
    if ! find "$runtime_dir" -type d -exec chmod 700 {} +; then
        printf 'reclaude-bot: cannot secure directories under %s\n' "$runtime_dir" >&2
        exit 1
    fi
    if ! find "$runtime_dir" -type f -exec chmod 600 {} +; then
        printf 'reclaude-bot: cannot secure files under %s\n' "$runtime_dir" >&2
        exit 1
    fi
done

if ! command -v gosu >/dev/null 2>&1; then
    printf '%s\n' 'reclaude-bot: gosu is required to drop root privileges' >&2
    exit 1
fi

# Grant the unprivileged bot user access to the host Docker socket (used by the
# admin-only /update self-update command). The socket's owning group varies per
# host, so map whatever GID the socket has into the container and add bot to it.
docker_sock=/var/run/docker.sock
if [ -S "$docker_sock" ]; then
    sock_gid="$(stat -c %g "$docker_sock")"
    if ! getent group "$sock_gid" >/dev/null 2>&1; then
        groupadd -g "$sock_gid" docker-host
    fi
    usermod -aG "$(getent group "$sock_gid" | cut -d: -f1)" bot
fi

exec gosu bot /bin/sh -c 'alembic upgrade head && exec reclaude-bot'
