from __future__ import annotations

from decimal import Decimal, InvalidOperation

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from reclaude_bot.application.admin import AdminService
from reclaude_bot.application.binding import BindingService
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.application.recovery import RecoveryService
from reclaude_bot.config import Settings
from reclaude_bot.domain.errors import DomainError


def build_router(settings: Settings) -> Router:
    router = Router(name="user")

    @router.message(Command("start"))
    async def start(message: Message) -> None:
        await message.answer("请使用 /bind 邮箱 或 /status。")

    @router.message(Command("bind"))
    async def bind(message: Message, command: CommandObject, binding: BindingService) -> None:
        try:
            if message.from_user is None or not command.args:
                raise ValueError
            user = await binding.bind(message.from_user.id, command.args.strip(), private_chat=message.chat.type == "private")
            await message.answer(f"绑定成功：{user.email}")
        except (DomainError, ValueError):
            await message.answer("绑定失败：请确认已完成首次成员同步、邮箱存在且未被占用。")

    @router.message(Command("status"))
    async def status(message: Message, quota: QuotaService) -> None:
        try:
            if message.from_user is None:
                return
            value = await quota.get_status(message.from_user.id)
            email = str(value["email"])
            local, _, domain = email.partition("@")
            await message.answer(
                f"邮箱：{(local[:1] or '*')}***@{domain}\n"
                f"本周期已用：${value['used_usd']:.2f}\n"
                f"当前额度：${value['limit_usd']:.2f}\n"
                f"剩余额度：${value['remaining_usd']:.2f}\n"
                f"刷新时间：{value['reset_at'].isoformat()}\n"
                f"最后24小时：{'是' if value['last_24h'] else '否'}\n"
                f"分配状态：{value['allocation_status']}"
            )
        except DomainError as exc:
            await message.answer(str(exc))

    return router


def build_admin_router(settings: Settings) -> Router:
    router = Router(name="admin")

    def is_admin(message: Message) -> bool:
        return message.from_user is not None and message.from_user.id in settings.telegram_admin_ids

    @router.message(Command("sync"))
    async def sync(message: Message, quota: QuotaService) -> None:
        if not is_admin(message):
            return
        try:
            await quota.ensure_cycle()
            count = await quota.sync_members()
            await message.answer(f"同步完成：{count} 个上游成员")
        except Exception:
            await message.answer("同步失败，已记录告警。")

    @router.message(Command("setquota"))
    async def setquota(message: Message, command: CommandObject, admin: AdminService) -> None:
        if not is_admin(message):
            return
        try:
            if not command.args:
                raise ValueError
            amount = Decimal(command.args.strip())
            value = await admin.set_quota(amount, message.from_user.id)  # type: ignore[union-attr]
            await message.answer(f"当前周期额度已设置为 ${value:.2f}")
        except (DomainError, InvalidOperation, ValueError) as exc:
            await message.answer(str(exc) or "用法：/setquota 金额")

    @router.message(Command("ban", "unban"))
    async def ban(message: Message, command: CommandObject, admin: AdminService) -> None:
        if not is_admin(message):
            return
        if not command.args or not command.args.strip().isdigit():
            await message.answer("用法：/ban 用户内部ID")
            return
        row = await admin.set_banned(int(command.args.strip()), message.from_user.id, (message.text or "").startswith("/ban"))  # type: ignore[union-attr]
        await message.answer(f"用户 {row.id} 状态：{row.status}")

    @router.message(Command("unbind"))
    async def unbind(message: Message, command: CommandObject, binding: BindingService) -> None:
        if not is_admin(message):
            return
        args = (command.args or "").split()
        if not args or not args[0].isdigit():
            await message.answer("用法：/unbind TelegramID [force]")
            return
        try:
            await binding.unbind(int(args[0]), operator_telegram_id=message.from_user.id, force_revoke=len(args) > 1 and args[1] == "force")  # type: ignore[union-attr]
            await message.answer("解绑完成")
        except DomainError as exc:
            await message.answer(str(exc))

    @router.message(Command("audit"))
    async def audit_view(message: Message, admin: AdminService) -> None:
        if not is_admin(message):
            return
        rows = await admin.recent_audit()
        await message.answer("\n".join(f"{row.created_at.isoformat()} {row.action} {row.result}" for row in rows) or "暂无审计记录")

    @router.message(Command("account", "recovery_enable"))
    async def recovery_enable(message: Message, recovery: RecoveryService) -> None:
        if not is_admin(message):
            return
        try:
            await recovery.health_sync_reconcile_enable(message.from_user.id)  # type: ignore[union-attr]
            await message.answer("账号、周期和成员健康检查完成，已启用上游写操作")
        except DomainError as exc:
            await message.answer(str(exc))
        except Exception:
            await message.answer("恢复失败，写操作仍已暂停，请检查 Reclaude 登录和账号状态。")

    return router
