from __future__ import annotations

import html
from decimal import Decimal, InvalidOperation

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import func, select

from reclaude_bot.application.admin import AdminService
from reclaude_bot.application.binding import BindingService
from reclaude_bot.application.onboarding import OnboardingService
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.application.recovery import RecoveryService
from reclaude_bot.application.task import ALLOWLIST, QuotaTaskService
from reclaude_bot.bot.middleware import GroupAccessMiddleware
from reclaude_bot.config import Settings
from reclaude_bot.domain.errors import DomainError
from reclaude_bot.infrastructure.db.models import UpstreamMember
from reclaude_bot.infrastructure.reclaude.models import AccountRecord
from reclaude_bot.jobs.scheduler import BackgroundJobs


def build_router(settings: Settings) -> Router:
    router = Router(name="user")
    router.message.middleware(GroupAccessMiddleware())

    @router.message(Command("start"))
    async def start(
        message: Message,
        command: CommandObject | None = None,
        onboarding: OnboardingService | None = None,
    ) -> None:
        payload = command.args if command is not None else None
        if payload and payload.startswith("verify_"):
            if onboarding is None or message.chat.type != "private" or message.from_user is None:
                await message.answer("验证链接只能在 Bot 私聊中使用。")
                return
            try:
                membership = await onboarding.verify_token(message.from_user.id, payload)
            except DomainError:
                membership = None
            if membership is None:
                await message.answer("验证链接无效或已过期，请从群组重新获取验证消息。")
                return
            if await onboarding.is_bound_active(message.from_user.id):
                await onboarding.queue_unmute_for_user(message.from_user.id)
                await message.answer("验证成功，群组权限正在恢复，请稍候。")
            else:
                await message.answer("验证成功，请在此私聊中发送 /bind <邮箱> 完成绑定。")
            return
        await message.answer("请使用 /bind 邮箱 或 /status。")

    @router.message(Command("bind"))
    async def bind(
        message: Message,
        command: CommandObject,
        binding: BindingService,
        onboarding: OnboardingService | None = None,
    ) -> None:
        try:
            if message.from_user is None or not command.args:
                raise ValueError
            user = await binding.bind(message.from_user.id, command.args.strip(), private_chat=message.chat.type == "private")
            pending = True
            if onboarding is not None:
                try:
                    pending = bool(await onboarding.queue_unmute_for_user(message.from_user.id))
                except Exception:
                    pending = True
            if pending:
                await message.answer(f"绑定成功：{html.escape(user.email)}\n群组权限正在恢复，完成后即可发言。")
            else:
                await message.answer(f"绑定成功：{html.escape(user.email)}")
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
    router.message.middleware(GroupAccessMiddleware())

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

    @router.message(Command("use"))
    async def use_account(message: Message, command: CommandObject, recovery: RecoveryService) -> None:
        if not is_admin(message):
            return
        account_id = (command.args or "").strip()
        if not account_id:
            await message.answer("用法：/use account_id（请先使用 /account 查看实时账号）")
            return
        try:
            account = await recovery.select_account(account_id, message.from_user.id)  # type: ignore[union-attr]
            await message.answer(f"已选择 Reclaude 账号 {account.account_id}，周期和成员同步完成；限额任务仍为 STOPPED，请使用 /starttask 显式启动")
        except DomainError as exc:
            await message.answer(f"账号选择失败：{html.escape(str(exc))}")
        except Exception:
            await message.answer("账号选择失败，写操作仍已暂停，请检查 Reclaude 登录、账号状态和成员同步。")

    @router.message(Command("task"))
    async def task_status(message: Message, task: QuotaTaskService, jobs: BackgroundJobs, quota: QuotaService) -> None:
        if not is_admin(message):
            return
        try:
            snapshot = await task.snapshot()
            runtime = jobs.status()
            lines = [f"限额任务：{'RUNNING' if snapshot.enabled else 'STOPPED'}"]
            lines.append(f"调度循环：{'运行中' if runtime['loop_running'] else '未运行'}")
            lines.append(f"最近启动：{_format_datetime(runtime['started_at'])}")
            lines.append(f"最近 tick 开始：{_format_datetime(runtime['last_tick_started'])}")
            lines.append(f"最近 tick 完成：{_format_datetime(runtime['last_tick_finished'])}")
            lines.append(f"最近 tick 错误：{html.escape(str(runtime['last_tick_error'] or '无'))}")
            lines.append(f"最近结果数：{runtime['last_result_count'] if runtime['last_result_count'] is not None else 'unknown'}")
            if snapshot.scope_mode == ALLOWLIST:
                scope = ", ".join(snapshot.member_ids) if snapshot.member_ids else "(empty)"
                lines.append(f"成员范围：ALLOWLIST {html.escape(scope)}")
                if snapshot.missing_member_ids:
                    lines.append(f"已消失成员：{html.escape(', '.join(snapshot.missing_member_ids))}")
            else:
                lines.append("成员范围：ALL")
            lines.append(f"Reclaude 账号：{html.escape(snapshot.selected_account_id or '未选择')}")
            lines.append(f"启动阻断原因：{html.escape(snapshot.reason or 'unknown')}")
            cycle = await quota.current_cycle_from_now()
            lines.append(f"当前周期：{_format_datetime(cycle.reset_at if cycle is not None else None)}")
            async with task.session_factory() as session:
                last_sync = await session.scalar(select(func.max(UpstreamMember.sampled_at)))
            lines.append(f"最近成员同步：{_format_datetime(last_sync)}")
            await message.answer("\n".join(lines))
        except Exception:
            await message.answer("任务状态暂时不可用。")

    @router.message(Command("starttask"))
    async def start_task(message: Message, recovery: RecoveryService, jobs: BackgroundJobs) -> None:
        if not is_admin(message):
            return
        try:
            account = await recovery.validate_selected_account()
            await jobs.start_quota_task(message.from_user.id)  # type: ignore[union-attr]
            await message.answer(f"限额任务已启动，写操作已开启（账号 {account.account_id}）。")
        except DomainError as exc:
            await message.answer(f"启动失败：{html.escape(str(exc))}")
        except Exception:
            await message.answer("启动失败，任务仍为 STOPPED，请检查 Reclaude 登录、账号状态和成员同步。")

    @router.message(Command("stoptask"))
    async def stop_task(message: Message, jobs: BackgroundJobs) -> None:
        if not is_admin(message):
            return
        try:
            await jobs.stop_quota_task(message.from_user.id)  # type: ignore[union-attr]
            await message.answer("限额任务已停止，写操作和额度循环均已关闭。")
        except Exception:
            await message.answer("停止限额任务失败，请检查服务日志。")

    @router.message(Command("addtaskmember"))
    async def add_task_member(message: Message, command: CommandObject, task: QuotaTaskService) -> None:
        if not is_admin(message):
            return
        try:
            values = (command.args or "").split()
            result = await task.add_members(values, message.from_user.id)  # type: ignore[union-attr]
            if values and values[0].casefold() == "all":
                await message.answer("限额任务成员范围已切回 ALL，旧白名单已清理。")
            else:
                await message.answer(f"已加入限额任务成员：{html.escape(', '.join(result))}（范围为 ALLOWLIST）")
        except DomainError as exc:
            await message.answer(f"成员范围更新失败：{html.escape(str(exc))}")

    @router.message(Command("deletetaskmember"))
    async def delete_task_member(message: Message, command: CommandObject, task: QuotaTaskService) -> None:
        if not is_admin(message):
            return
        try:
            values = (command.args or "").split()
            result = await task.delete_members(values, message.from_user.id)  # type: ignore[union-attr]
            await message.answer(f"已移除限额任务成员：{html.escape(', '.join(result))}；剩余为空时不会执行任何配额动作。")
        except DomainError as exc:
            await message.answer(f"成员范围更新失败：{html.escape(str(exc))}")

    @router.message(Command("account", "recovery_enable"))
    async def recovery_enable(message: Message, command: CommandObject, recovery: RecoveryService) -> None:
        if not is_admin(message):
            return
        if command.command.casefold() == "account":
            try:
                listing = await recovery.list_accounts()
                selected = listing.selected_account_id
                lines = [f"当前账号状态：{html.escape(listing.me.current_account.status)}"]
                if selected:
                    lines.append(f"已选择账号：{html.escape(selected)}")
                else:
                    lines.append("已选择账号：未选择")
                if not listing.accounts.items:
                    lines.append("实时账号：暂无")
                else:
                    lines.append("实时账号：")
                    for account in listing.accounts.items:
                        if not isinstance(account, AccountRecord):
                            continue
                        account_id = html.escape(str(account.account_id)) if account.account_id is not None else "无效"
                        email = html.escape(account.account_email or "-")
                        lifecycle = html.escape(account.lifecycle or "unknown")
                        health = html.escape(account.health or "unknown")
                        marker = " [当前]" if selected is not None and str(account.account_id).strip() == selected.strip() else ""
                        lines.append(f"- {account_id}{marker} | {email} | lifecycle={lifecycle} | health={health}")
                await message.answer("\n".join(lines))
            except DomainError as exc:
                await message.answer(f"账号查询失败：{exc}")
            except Exception:
                await message.answer("账号查询失败，请检查 Reclaude 登录和会话状态。")
            return
        try:
            await recovery.health_sync_reconcile_enable(message.from_user.id)  # type: ignore[union-attr]
            await message.answer("账号、周期和成员健康检查完成；限额任务仍为 STOPPED，请使用 /starttask 显式启动。")
        except DomainError as exc:
            await message.answer(str(exc))
        except Exception:
            await message.answer("恢复失败，写操作仍已暂停，请检查 Reclaude 登录和账号状态。")

    return router


def _format_datetime(value: object) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else "unknown"
