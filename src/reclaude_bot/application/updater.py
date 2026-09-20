"""Docker self-update engine for the admin-only /update command.

The bot container mounts the host Docker socket. This module talks to the host
daemon through the Docker Python SDK (no docker CLI in the image): it inspects
the running container, pulls the image it was created from, and when the tag
moved to a new image it hands the container swap to a throwaway helper
container based on the public ``docker:cli`` image. The helper stops the old
container, starts the pre-created replacement, watches it for a grace period,
writes ``result.json`` into the update state directory, and rolls back to the
old container when the new one fails to stay up. On the next process start,
:func:`consume_restart_notification` reports that result to the admin chat.
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from reclaude_bot.config import Settings

if TYPE_CHECKING:
    from aiogram import Bot

log = structlog.get_logger(__name__)

_DOCKER_SOCKET = Path("/var/run/docker.sock")
_HELPER_IMAGE = "docker:cli"
_UPDATING_SUFFIX = "-updating"
_RESULT_FILE_NAME = "result.json"
# The helper waits 30s before judging the new container; give it ample margin.
_RESULT_MAX_WAIT_SECONDS = 75.0
_RESULT_POLL_INTERVAL = 3.0
_NOTIFICATION_MAX_AGE_SECONDS = 600

# Runs inside the helper container (busybox sh from docker:cli). Args:
# $1 final container name, $2 update state dir (mounted), $3 original restart policy.
_SWAP_SCRIPT = r"""#!/bin/sh
NEW_CONTAINER="$1"
STATE_DIR="$2"
RESTART_POLICY="${3:-unless-stopped}"
OLD_CONTAINER="${NEW_CONTAINER}-updating"
GRACE_SECONDS=30

docker update --restart=no "$OLD_CONTAINER"
docker stop "$OLD_CONTAINER" --time 30
docker start "$NEW_CONTAINER"
sleep "$GRACE_SECONDS"

state="$(docker inspect -f '{{.State.Running}} {{.RestartCount}}' "$NEW_CONTAINER" 2>/dev/null || printf 'unknown 0')"
if [ "$state" = "true 0" ]; then
    printf '{"ok":true}\n' > "$STATE_DIR/result.json"
    docker rm "$OLD_CONTAINER"
    docker image prune -f >/dev/null 2>&1 || true
    exit 0
fi

printf '{"ok":false}\n' > "$STATE_DIR/result.json"
docker stop "$NEW_CONTAINER" --time 10 2>/dev/null || true
docker rm "$NEW_CONTAINER" 2>/dev/null || true
docker rename "$OLD_CONTAINER" "$NEW_CONTAINER"
docker update --restart="$RESTART_POLICY" "$NEW_CONTAINER"
docker start "$NEW_CONTAINER"
exit 1
"""


class UpdateError(Exception):
    """Self-update failure; the message is safe to show to admins."""


@dataclass(frozen=True)
class UpdateCheck:
    changed: bool
    image_spec: str
    current_image_id: str
    new_image_id: str


@dataclass(frozen=True)
class _ContainerConfig:
    name: str
    image: str
    env: list[str]
    labels: dict[str, str]
    binds: list[str]
    port_bindings: dict[str, Any]
    restart_policy: dict[str, Any]
    network_mode: str


def _new_client() -> Any:
    import docker

    return docker.from_env()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _short(image_id: str) -> str:
    digest = image_id.removeprefix("sha256:")
    return digest[:12] or "unknown"


class UpdateService:
    """Coordinates one self-update at a time through the host Docker daemon."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return _DOCKER_SOCKET.exists()

    @property
    def in_progress(self) -> bool:
        return self._lock.locked()

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        async with self._lock:
            yield

    async def check_for_update(self) -> UpdateCheck:
        """Pull the image this container runs from and report whether the tag moved."""
        try:
            client = await asyncio.to_thread(_new_client)
            container = await asyncio.to_thread(client.containers.get, socket.gethostname())
            attrs = container.attrs
            image_spec = str(attrs.get("Config", {}).get("Image") or "").strip()
            current_id = str(attrs.get("Image") or "")
            if not image_spec or not current_id:
                raise UpdateError("无法识别当前容器的镜像信息。")
            await asyncio.to_thread(self._pull, client, image_spec)
            new_id = str((await asyncio.to_thread(client.images.get, image_spec)).id)
        except UpdateError:
            raise
        except Exception as exc:
            raise UpdateError(f"检查更新失败（{type(exc).__name__}）。") from exc
        log.info(
            "self_update_check",
            image=image_spec,
            current=_short(current_id),
            latest=_short(new_id),
            changed=new_id != current_id,
        )
        return UpdateCheck(changed=new_id != current_id, image_spec=image_spec, current_image_id=current_id, new_image_id=new_id)

    async def apply_update(self, check: UpdateCheck, chat_id: int) -> None:
        """Swap this container for one built from the freshly pulled image.

        Returns once the helper container has been launched; the helper stops this
        container moments later, so callers must notify the admin beforehand.
        """
        self._write_pending(chat_id, check)
        try:
            await asyncio.to_thread(self._apply_update_sync, check)
        except UpdateError:
            self._delete_pending()
            raise
        except Exception as exc:
            self._delete_pending()
            raise UpdateError(f"启动更新流程失败（{type(exc).__name__}）。") from exc

    def _pull(self, client: Any, image_spec: str) -> None:
        log.info("self_update_pull_started", image=image_spec)
        try:
            for line in client.api.pull(image_spec, stream=True, decode=True):
                if line.get("error"):
                    raise UpdateError(f"拉取镜像失败：{line['error']}")
        except UpdateError:
            raise
        except Exception as exc:
            raise UpdateError(f"拉取镜像失败（{type(exc).__name__}）。") from exc
        log.info("self_update_pull_finished", image=image_spec)

    def _apply_update_sync(self, check: UpdateCheck) -> None:
        client = _new_client()
        old = client.containers.get(socket.gethostname())
        config = self._inspect(old)
        state_host_dir = self._find_host_state_dir(config.binds)
        if state_host_dir is None:
            raise UpdateError("容器缺少更新状态目录挂载，请先用新版 compose 重建一次 bot 容器。")
        self._ensure_helper_image(client)

        updating_name = f"{config.name}{_UPDATING_SUFFIX}"
        restart_policy = dict(config.restart_policy)
        state_container_dir = str(self._state_dir())
        new_container = None
        renamed = False
        try:
            old.update(restart_policy={"Name": "no"})
            old.rename(updating_name)
            renamed = True
            log.info("self_update_container_renamed", name=config.name, updating_name=updating_name)
            new_container = client.containers.create(
                image=check.image_spec,
                name=config.name,
                environment=list(config.env),
                labels=dict(config.labels),
                volumes=list(config.binds),
                ports=self._port_bindings(config),
                restart_policy=restart_policy,
                network_mode=config.network_mode,
            )
            client.containers.run(
                image=_HELPER_IMAGE,
                command=["sh", "-c", _SWAP_SCRIPT, "update-helper", config.name, state_container_dir, str(restart_policy.get("Name") or "unless-stopped")],
                detach=True,
                remove=True,
                volumes={
                    "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
                    state_host_dir: {"bind": state_container_dir, "mode": "rw"},
                },
            )
            log.info("self_update_helper_started", container=config.name, image=check.image_spec)
        except Exception as exc:
            log.error("self_update_swap_failed", error_type=type(exc).__name__)
            if new_container is not None:
                try:
                    new_container.remove(force=True)
                except Exception:
                    pass
            if renamed:
                try:
                    old.rename(config.name)
                    old.update(restart_policy=restart_policy)
                    log.info("self_update_compensation_done", name=config.name)
                except Exception as comp_exc:
                    log.error("self_update_compensation_failed", error_type=type(comp_exc).__name__)
            raise UpdateError(f"切换容器失败（{type(exc).__name__}），已回滚容器配置。") from exc

    def _inspect(self, container: Any) -> _ContainerConfig:
        attrs = container.attrs
        host_cfg = attrs.get("HostConfig", {})
        cfg = attrs.get("Config", {})
        return _ContainerConfig(
            name=str(attrs.get("Name", "")).lstrip("/"),
            image=str(cfg.get("Image", "")),
            env=list(cfg.get("Env") or []),
            labels=dict(cfg.get("Labels") or {}),
            binds=list(host_cfg.get("Binds") or []),
            port_bindings=dict(host_cfg.get("PortBindings") or {}),
            restart_policy=dict(host_cfg.get("RestartPolicy") or {"Name": "no"}),
            network_mode=str(host_cfg.get("NetworkMode", "bridge")),
        )

    def _state_dir(self) -> Path:
        return self._settings.update_state_file.parent

    def _find_host_state_dir(self, binds: list[str]) -> str | None:
        target = str(self._state_dir())
        for bind in binds:
            parts = bind.split(":")
            if len(parts) >= 2 and parts[1] == target:
                return parts[0]
        return None

    @staticmethod
    def _port_bindings(config: _ContainerConfig) -> dict[str, Any]:
        ports: dict[str, Any] = {}
        for container_port, host_bindings in config.port_bindings.items():
            if host_bindings:
                ports[container_port] = [(binding.get("HostIp", ""), binding.get("HostPort", "")) for binding in host_bindings]
        return ports

    def _ensure_helper_image(self, client: Any) -> None:
        import docker.errors

        try:
            client.images.get(_HELPER_IMAGE)
        except docker.errors.ImageNotFound:
            log.info("self_update_helper_image_pull", image=_HELPER_IMAGE)
            client.images.pull(_HELPER_IMAGE)

    def _write_pending(self, chat_id: int, check: UpdateCheck) -> None:
        path = self._settings.update_state_file
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "chat_id": chat_id,
            "ts": int(time.time()),
            "image_spec": check.image_spec,
            "new_image_id": check.new_image_id,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)

    def _delete_pending(self) -> None:
        try:
            self._settings.update_state_file.unlink(missing_ok=True)
        except OSError:
            pass


async def consume_restart_notification(bot: Bot, settings: Settings) -> None:
    """Report the outcome of the previous /update swap, if any, to the admin chat.

    The helper container writes ``result.json`` next to the pending marker. On the
    success path the new container starts before the helper finishes its grace
    period, so a fresh marker without a result is polled briefly before being
    declared an interrupted update.
    """
    pending_path = settings.update_state_file
    result_path = pending_path.parent / _RESULT_FILE_NAME
    try:
        pending = _read_json(pending_path)
        if pending is None:
            return
        chat_id = int(pending.get("chat_id") or 0)
        ts = int(pending.get("ts") or 0)
        if chat_id <= 0:
            return
        if time.time() - ts > _NOTIFICATION_MAX_AGE_SECONDS:
            log.info("self_update_notification_stale")
            return
        result = _read_json(result_path)
        deadline = time.monotonic() + _RESULT_MAX_WAIT_SECONDS
        while result is None and time.monotonic() < deadline:
            await asyncio.sleep(_RESULT_POLL_INTERVAL)
            result = _read_json(result_path)
        if result is None:
            text = "更新中断：未收到更新结果回报，请人工检查容器状态。"
        elif result.get("ok") is True:
            spec = str(pending.get("image_spec") or "")
            text = f"更新完成：当前运行 {spec}（镜像 {_short(str(pending.get('new_image_id') or ''))}）。"
        else:
            text = "更新失败：新版本未通过启动检查，已自动回滚到旧版本。"
        await bot.send_message(chat_id, text)
    except Exception as exc:
        log.warning("self_update_notification_failed", error_type=type(exc).__name__)
    finally:
        for path in (pending_path, result_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


async def cleanup_stale_updating_container() -> None:
    """Remove a leftover ``<name>-updating`` container from an interrupted swap."""
    if not _DOCKER_SOCKET.exists():
        return

    def _cleanup() -> None:
        import docker.errors

        client = _new_client()
        name = client.containers.get(socket.gethostname()).name
        try:
            stale = client.containers.get(f"{name}{_UPDATING_SUFFIX}")
        except docker.errors.NotFound:
            return
        if stale.status in ("exited", "dead", "created"):
            stale.remove()
            log.info("self_update_stale_container_removed", name=stale.name)
        else:
            log.warning("self_update_stale_container_kept", name=stale.name, status=stale.status)

    try:
        await asyncio.to_thread(_cleanup)
    except Exception as exc:
        log.debug("self_update_cleanup_skipped", error_type=type(exc).__name__)
