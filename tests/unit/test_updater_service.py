import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from docker.errors import ImageNotFound

import reclaude_bot.application.updater as updater_module
from reclaude_bot.application.updater import (
    UpdateCheck,
    UpdateError,
    UpdateService,
    cleanup_stale_updating_container,
    consume_restart_notification,
)
from reclaude_bot.config import Settings

SPEC = "ghcr.io/example/bot:latest"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        DATABASE_URL="postgresql+asyncpg://test:test@localhost/test",
        UPDATE_STATE_FILE=tmp_path / "update" / "pending.json",
    )


def _client(tmp_path: Path, *, current_id: str = "sha256:old", pulled_id: str = "sha256:new", with_state_mount: bool = True) -> tuple[MagicMock, MagicMock]:
    container = MagicMock()
    binds = ["/var/run/docker.sock:/var/run/docker.sock"]
    if with_state_mount:
        binds.append(f"/srv/host-update:{tmp_path / 'update'}")
    container.attrs = {
        "Name": "/tgbot-bot-1",
        "Image": current_id,
        "Config": {"Image": SPEC, "Env": ["A=1"], "Labels": {"com.docker.compose.service": "bot"}},
        "HostConfig": {
            "Binds": binds,
            "PortBindings": {},
            "RestartPolicy": {"Name": "unless-stopped"},
            "NetworkMode": "tgbot_default",
        },
        "NetworkSettings": {"Networks": {"tgbot_default": {}}},
    }
    client = MagicMock()
    client.containers.get.return_value = container
    client.api.pull.return_value = iter([])
    pulled = MagicMock()
    pulled.id = pulled_id
    client.images.get.return_value = pulled
    return client, container


def _check() -> UpdateCheck:
    return UpdateCheck(changed=True, image_spec=SPEC, current_image_id="sha256:old", new_image_id="sha256:new")


@pytest.mark.asyncio
async def test_check_for_update_detects_new_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client(tmp_path)
    monkeypatch.setattr(updater_module, "_new_client", lambda: client)

    check = await UpdateService(_settings(tmp_path)).check_for_update()

    assert check.changed is True
    assert check.image_spec == SPEC
    assert check.current_image_id == "sha256:old"
    assert check.new_image_id == "sha256:new"
    # The pull target is the image the container actually runs, not a hardcoded default.
    assert client.api.pull.call_args.args[0] == SPEC


@pytest.mark.asyncio
async def test_check_for_update_reports_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client(tmp_path, pulled_id="sha256:old")
    monkeypatch.setattr(updater_module, "_new_client", lambda: client)

    check = await UpdateService(_settings(tmp_path)).check_for_update()

    assert check.changed is False


@pytest.mark.asyncio
async def test_check_for_update_wraps_pull_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client(tmp_path)
    client.api.pull.return_value = iter([{"error": "registry unreachable"}])
    monkeypatch.setattr(updater_module, "_new_client", lambda: client)

    with pytest.raises(UpdateError, match="拉取镜像失败"):
        await UpdateService(_settings(tmp_path)).check_for_update()


@pytest.mark.asyncio
async def test_apply_update_swaps_container_via_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, container = _client(tmp_path)
    monkeypatch.setattr(updater_module, "_new_client", lambda: client)
    settings = _settings(tmp_path)

    await UpdateService(settings).apply_update(_check(), chat_id=555)

    container.update.assert_any_call(restart_policy={"Name": "no"})
    container.rename.assert_called_once_with("tgbot-bot-1-updating")
    create_kwargs = client.containers.create.call_args.kwargs
    assert create_kwargs["image"] == SPEC
    assert create_kwargs["name"] == "tgbot-bot-1"
    assert create_kwargs["environment"] == ["A=1"]
    assert create_kwargs["labels"] == {"com.docker.compose.service": "bot"}
    assert create_kwargs["volumes"] == container.attrs["HostConfig"]["Binds"]
    assert create_kwargs["restart_policy"] == {"Name": "unless-stopped"}
    assert create_kwargs["network_mode"] == "tgbot_default"
    run_kwargs = client.containers.run.call_args.kwargs
    assert run_kwargs["image"] == "docker:cli"
    assert run_kwargs["detach"] is True
    assert run_kwargs["remove"] is True
    assert run_kwargs["command"][:3] == ["sh", "-c", updater_module._SWAP_SCRIPT]
    assert run_kwargs["command"][3:] == ["update-helper", "tgbot-bot-1", str(tmp_path / "update"), "unless-stopped"]
    assert run_kwargs["volumes"]["/var/run/docker.sock"] == {"bind": "/var/run/docker.sock", "mode": "rw"}
    assert run_kwargs["volumes"]["/srv/host-update"] == {"bind": str(tmp_path / "update"), "mode": "rw"}

    pending = json.loads(settings.update_state_file.read_text(encoding="utf-8"))
    assert pending["chat_id"] == 555
    assert pending["image_spec"] == SPEC
    assert pending["new_image_id"] == "sha256:new"


@pytest.mark.asyncio
async def test_apply_update_pulls_helper_image_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client(tmp_path)
    client.images.get.side_effect = ImageNotFound("missing")
    monkeypatch.setattr(updater_module, "_new_client", lambda: client)

    await UpdateService(_settings(tmp_path)).apply_update(_check(), chat_id=555)

    client.images.pull.assert_called_once_with("docker:cli")


@pytest.mark.asyncio
async def test_apply_update_requires_state_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, _ = _client(tmp_path, with_state_mount=False)
    monkeypatch.setattr(updater_module, "_new_client", lambda: client)
    settings = _settings(tmp_path)

    with pytest.raises(UpdateError, match="缺少更新状态目录挂载"):
        await UpdateService(settings).apply_update(_check(), chat_id=555)

    assert not settings.update_state_file.exists()
    client.containers.create.assert_not_called()


@pytest.mark.asyncio
async def test_apply_update_compensates_when_create_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, container = _client(tmp_path)
    client.containers.create.side_effect = RuntimeError("boom")
    monkeypatch.setattr(updater_module, "_new_client", lambda: client)
    settings = _settings(tmp_path)

    with pytest.raises(UpdateError, match="切换容器失败"):
        await UpdateService(settings).apply_update(_check(), chat_id=555)

    rename_calls = [call.args[0] for call in container.rename.call_args_list]
    assert rename_calls == ["tgbot-bot-1-updating", "tgbot-bot-1"]
    update_calls = [call.kwargs["restart_policy"] for call in container.update.call_args_list]
    assert update_calls == [{"Name": "no"}, {"Name": "unless-stopped"}]
    client.containers.run.assert_not_called()
    assert not settings.update_state_file.exists()


@pytest.mark.asyncio
async def test_consume_notification_reports_success(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.update_state_file.parent.mkdir(parents=True)
    settings.update_state_file.write_text(json.dumps({"chat_id": 555, "ts": int(time.time()), "image_spec": SPEC, "new_image_id": "sha256:abcdef0123456789"}), encoding="utf-8")
    (settings.update_state_file.parent / "result.json").write_text('{"ok":true}\n', encoding="utf-8")
    bot = AsyncMock()

    await consume_restart_notification(bot, settings)

    bot.send_message.assert_awaited_once_with(555, f"更新完成：当前运行 {SPEC}（镜像 abcdef012345）。")
    assert not settings.update_state_file.exists()
    assert not (settings.update_state_file.parent / "result.json").exists()


@pytest.mark.asyncio
async def test_consume_notification_reports_rollback(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.update_state_file.parent.mkdir(parents=True)
    settings.update_state_file.write_text(json.dumps({"chat_id": 555, "ts": int(time.time()), "image_spec": SPEC, "new_image_id": "sha256:new"}), encoding="utf-8")
    (settings.update_state_file.parent / "result.json").write_text('{"ok":false}\n', encoding="utf-8")
    bot = AsyncMock()

    await consume_restart_notification(bot, settings)

    bot.send_message.assert_awaited_once_with(555, "更新失败：新版本未通过启动检查，已自动回滚到旧版本。")


@pytest.mark.asyncio
async def test_consume_notification_warns_when_result_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater_module, "_RESULT_MAX_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(updater_module, "_RESULT_POLL_INTERVAL", 0.001)
    settings = _settings(tmp_path)
    settings.update_state_file.parent.mkdir(parents=True)
    settings.update_state_file.write_text(json.dumps({"chat_id": 555, "ts": int(time.time()), "image_spec": SPEC, "new_image_id": "sha256:new"}), encoding="utf-8")
    bot = AsyncMock()

    await consume_restart_notification(bot, settings)

    bot.send_message.assert_awaited_once_with(555, "更新中断：未收到更新结果回报，请人工检查容器状态。")
    assert not settings.update_state_file.exists()


@pytest.mark.asyncio
async def test_consume_notification_drops_stale_marker(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.update_state_file.parent.mkdir(parents=True)
    settings.update_state_file.write_text(json.dumps({"chat_id": 555, "ts": int(time.time()) - 3600, "image_spec": SPEC, "new_image_id": "sha256:new"}), encoding="utf-8")
    bot = AsyncMock()

    await consume_restart_notification(bot, settings)

    bot.send_message.assert_not_awaited()
    assert not settings.update_state_file.exists()


@pytest.mark.asyncio
async def test_consume_notification_noop_without_marker(tmp_path: Path) -> None:
    bot = AsyncMock()

    await consume_restart_notification(bot, _settings(tmp_path))

    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_removes_stale_updating_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater_module, "_DOCKER_SOCKET", tmp_path / "docker.sock")
    (tmp_path / "docker.sock").touch()
    me = MagicMock()
    me.name = "tgbot-bot-1"
    stale = MagicMock()
    stale.status = "exited"
    client = MagicMock()
    client.containers.get.side_effect = [me, stale]
    monkeypatch.setattr(updater_module, "_new_client", lambda: client)

    await cleanup_stale_updating_container()

    stale.remove.assert_called_once()


@pytest.mark.asyncio
async def test_cleanup_skips_without_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater_module, "_DOCKER_SOCKET", tmp_path / "missing.sock")
    factory = MagicMock()
    monkeypatch.setattr(updater_module, "_new_client", factory)

    await cleanup_stale_updating_container()

    factory.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_swallows_docker_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updater_module, "_DOCKER_SOCKET", tmp_path / "docker.sock")
    (tmp_path / "docker.sock").touch()

    def _boom() -> None:
        raise RuntimeError("docker down")

    monkeypatch.setattr(updater_module, "_new_client", _boom)

    await cleanup_stale_updating_container()
