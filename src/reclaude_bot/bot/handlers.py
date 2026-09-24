from __future__ import annotations

import html
import traceback
from decimal import Decimal, InvalidOperation

import structlog
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import func, select

from reclaude_bot.application.admin import AdminService
from reclaude_bot.application.binding import BindingService
from reclaude_bot.application.onboarding import OnboardingService
from reclaude_bot.application.quota import QuotaService
from reclaude_bot.application.recovery import RecoveryService
from reclaude_bot.application.task import ALLOWLIST, EXCLUDE, QuotaTaskService
from reclaude_bot.application.updater import UpdateError, UpdateService
from reclaude_bot.bot.middleware import GroupAccessMiddleware
from reclaude_bot.config import Settings
from reclaude_bot.domain.errors import DomainError
from reclaude_bot.infrastructure.db.models import UpstreamMember
from reclaude_bot.infrastructure.reclaude.models import AccountRecord
from reclaude_bot.jobs.scheduler import BackgroundJobs

log = structlog.get_logger(__name__)


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
                await message.answer("验证链接无效或已过期。请重新加入群组获取新的验证消息；若你已完成绑定，重新入群后权限会自动恢复。")
                return
            if await onboarding.is_bound_active(message.from_user.id):
                await onboarding.queue_unmute_for_user(message.from_user.id)
                await message.answer("验证成功，群组权限正在恢复，请稍候。")
            else:
                await message.answer("验证成功，请在此私聊中发送 /bind &lt;邮箱&gt; 完成绑定。")
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
            await message.answer(html.escape(str(exc)))

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

    @router.message(Command("member"))
    async def member_list(message: Message, quota: QuotaService) -> None:
        if not is_admin(message):
            return
        try:
            members = await quota.list_upstream_members()
            if not members:
                await message.answer("暂无上游成员，请先执行 /sync")
                return
            lines = [f"上游成员：{len(members)} 个 | 最近同步：{_format_datetime(max(member.sampled_at for member in members))}"]
            lines.extend(f"- {html.escape(member.email)} | {html.escape(member.reclaude_user_id)}" for member in members)
            current = ""
            for line in lines:
                candidate = f"{current}\n{line}" if current else line
                if current and len(candidate) > 4000:
                    await message.answer(current)
                    current = line
                else:
                    current = candidate
            if current:
                await message.answer(current)
        except Exception as exc:
            log.error(
                "upstream_member_listing_failed",
                error_type=type(exc).__name__,
                traceback="".join(traceback.format_tb(exc.__traceback__)),
            )
            await message.answer("成员列表暂时不可用。")

    @router.message(Command("setquota"))
    async def setquota(message: Message, command: CommandObject, admin: AdminService) -> None:
        if not is_admin(message):
            return
        try:
            if not command.args:
                raise ValueError
            amount = Decimal(command.args.strip())
            value = await admin.set_quota(amount, message.from_user.id)  # type: ignore[union-attr]
            await message.answer(f"全局默认额度已设置为 ${value:.2f}（新建任务的默认值；现有任务请用 /settaskquota）")
        except (DomainError, InvalidOperation, ValueError) as exc:
            await message.answer(html.escape(str(exc)) or "用法：/setquota 金额")

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
            await message.answer(html.escape(str(exc)))

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
            await message.answer(f"已选择 Reclaude 账号 {account.account_id}，周期和成员同步完成，本周期用量已清零；换号前运行的任务已自动恢复，被移除成员将随轮询自动加回。")
        except DomainError as exc:
            await message.answer(f"账号选择失败：{html.escape(str(exc))}")
        except Exception:
            await message.answer("账号选择失败，写操作仍已暂停，请检查 Reclaude 登录、账号状态和成员同步。")

    @router.message(Command("newtask"))
    async def new_task(message: Message, command: CommandObject, task: QuotaTaskService) -> None:
        if not is_admin(message):
            return
        values = (command.args or "").split()
        if not values or len(values) > 2:
            await message.answer("用法：/newtask 名称 [每用户额度]")
            return
        try:
            limit = Decimal(values[1]) if len(values) == 2 else None
        except InvalidOperation:
            await message.answer("用法：/newtask 名称 [每用户额度]")
            return
        try:
            snapshot = await task.create_task(values[0], limit, message.from_user.id)  # type: ignore[union-attr]
        except DomainError as exc:
            await message.answer(html.escape(str(exc)))
            return
        await message.answer(
            f"限额任务 {html.escape(snapshot.name)} 已创建：STOPPED | 范围 ALL | 每用户额度 ${snapshot.limit_usd:.2f}。\n"
            f"使用 /addtaskmember {html.escape(snapshot.name)} &lt;reclaude_user_id&gt; 限定成员，/starttask {html.escape(snapshot.name)} 启动。"
        )

    @router.message(Command("deltatask"))
    async def delete_task(message: Message, command: CommandObject, task: QuotaTaskService) -> None:
        if not is_admin(message):
            return
        try:
            name = await task.resolve((command.args or "").strip() or None)
            await task.delete_task(name, message.from_user.id)  # type: ignore[union-attr]
            await message.answer(f"限额任务 {html.escape(name)} 及其成员范围已删除。")
        except DomainError as exc:
            await message.answer(html.escape(str(exc)))

    @router.message(Command("task"))
    async def task_status(message: Message, command: CommandObject, task: QuotaTaskService, jobs: BackgroundJobs, quota: QuotaService, recovery: RecoveryService) -> None:
        if not is_admin(message):
            return
        try:
            name_arg = (command.args or "").strip() or None
            if name_arg is None:
                snapshots = await task.list_tasks()
                if not snapshots:
                    await message.answer("暂无限额任务，请先使用 /newtask 创建。")
                    return
                lines = [f"限额任务：{len(snapshots)} 个"]
                for item in snapshots:
                    if item.scope_mode == ALLOWLIST:
                        scope = f"范围 ALLOWLIST | 成员 {len(item.member_ids)} 个"
                    elif item.scope_mode == EXCLUDE:
                        scope = f"范围 EXCLUDE | 排除 {len(item.member_ids)} 个"
                    else:
                        scope = "范围 ALL"
                    lines.append(f"- {html.escape(item.name)} | {'RUNNING' if item.enabled else 'STOPPED'} | {scope} | 额度 ${item.limit_usd:.2f}")
                await message.answer("\n".join(lines))
                return
            name = await task.resolve(name_arg)
            snapshot = await task.snapshot(name)
            runtime = jobs.status()
            state = await recovery.gate.get_state()
            lines = [f"任务 {html.escape(snapshot.name)}：{'RUNNING' if snapshot.enabled else 'STOPPED'}"]
            lines.append(f"任务额度：${snapshot.limit_usd:.2f}")
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
            elif snapshot.scope_mode == EXCLUDE:
                excluded = ", ".join(snapshot.member_ids) if snapshot.member_ids else "(empty)"
                lines.append(f"成员范围：EXCLUDE（排除：{html.escape(excluded)}）")
                if snapshot.missing_member_ids:
                    lines.append(f"已消失成员：{html.escape(', '.join(snapshot.missing_member_ids))}")
            else:
                lines.append("成员范围：ALL")
            selected = state.selected_account_id if state is not None else None
            lines.append(f"Reclaude 账号：{html.escape(str(selected)) if selected else '未选择'}")
            lines.append(f"写闸门状态：{'开启' if state is not None and state.write_enabled else '关闭'}（{html.escape(str(state.reason)) if state is not None else 'unknown'}）")
            cycle = await quota.current_cycle_from_now()
            lines.append(f"当前周期：{_format_datetime(cycle.reset_at if cycle is not None else None)}")
            async with quota.session_factory() as session:
                last_sync = await session.scalar(select(func.max(UpstreamMember.sampled_at)))
            lines.append(f"最近成员同步：{_format_datetime(last_sync)}")
            await message.answer("\n".join(lines))
        except DomainError as exc:
            await message.answer(html.escape(str(exc)))
        except Exception:
            await message.answer("任务状态暂时不可用。")

    @router.message(Command("taskusers"))
    async def task_users(message: Message, command: CommandObject, task: QuotaTaskService, quota: QuotaService) -> None:
        if not is_admin(message) or message.chat.type != "private":
            return
        try:
            name = await task.resolve((command.args or "").strip() or None)
            snapshot = await task.snapshot(name)
            usage = await quota.list_task_usage(scope_mode=snapshot.scope_mode, member_ids=snapshot.member_ids, limit_usd=snapshot.limit_usd)
        except DomainError as exc:
            await message.answer(html.escape(str(exc)))
            return
        except Exception as exc:
            log.error(
                "task_usage_listing_failed",
                error_type=type(exc).__name__,
                traceback="".join(traceback.format_tb(exc.__traceback__)),
            )
            await message.answer("任务成员使用状况暂时不可用。")
            return
        entries = usage["members"]
        if not entries:
            if snapshot.scope_mode == ALLOWLIST:
                await message.answer(f"任务 {html.escape(snapshot.name)} 白名单为空，请使用 /addtaskmember 添加成员。")
            elif snapshot.scope_mode == EXCLUDE:
                await message.answer(f"任务 {html.escape(snapshot.name)} 范围内暂无成员：其余成员均被排除或尚未同步。")
            else:
                await message.answer("任务范围内暂无成员，请先执行 /sync")
            return
        lines = [
            f"任务：{html.escape(snapshot.name)} | {'RUNNING' if snapshot.enabled else 'STOPPED'} | 范围：{snapshot.scope_mode} | "
            f"成员：{len(entries)} 个 | 任务额度 ${usage['limit_usd']:.2f} | 周期刷新：{_format_datetime(usage['reset_at'])}"
        ]
        lines.extend(await _account_usage_lines(quota))
        for entry in entries:
            reclaude_user_id = html.escape(str(entry["reclaude_user_id"]))
            if entry["missing_upstream"]:
                lines.append(f"- {reclaude_user_id} | 成员已从上游消失")
                continue
            email = html.escape(str(entry["email"]))
            if entry["telegram_user_id"] is not None:
                identity = f"TG {entry['telegram_user_id']} | {html.escape(str(entry['user_status']))}"
            else:
                identity = "未绑定"
            if entry["used_usd"] is None:
                usage_text = "数据未同步"
            else:
                usage_text = f"已用 ${entry['used_usd']:.2f} | 剩余 ${entry['remaining_usd']:.2f}"
            lines.append(f"- {email} | {reclaude_user_id} | {identity} | {usage_text}")
        current = ""
        for line in lines:
            candidate = f"{current}\n{line}" if current else line
            if current and len(candidate) > 4000:
                await message.answer(current)
                current = line
            else:
                current = candidate
        if current:
            await message.answer(current)

    @router.message(Command("starttask"))
    async def start_task(message: Message, command: CommandObject, task: QuotaTaskService, recovery: RecoveryService, jobs: BackgroundJobs) -> None:
        if not is_admin(message):
            return
        try:
            name = await task.resolve((command.args or "").strip() or None)
        except DomainError as exc:
            await message.answer(f"启动失败：{html.escape(str(exc))}")
            return
        try:
            account = await recovery.validate_selected_account()
            await jobs.start_quota_task(name, message.from_user.id)  # type: ignore[union-attr]
            reply = f"限额任务 {html.escape(name)} 已启动，写操作已开启（账号 {account.account_id}）。"
            if not await task.sync_enabled():
                reply += "\n注意：数据统计当前已停止，配额动作不会自动执行；恢复统计请使用 /startstats。"
            await message.answer(reply)
        except DomainError as exc:
            await message.answer(f"启动失败：{html.escape(str(exc))}")
        except Exception:
            await message.answer(f"启动失败，任务 {html.escape(name)} 仍为 STOPPED，请检查 Reclaude 登录、账号状态和成员同步。")

    @router.message(Command("stoptask"))
    async def stop_task(message: Message, command: CommandObject, task: QuotaTaskService, jobs: BackgroundJobs) -> None:
        if not is_admin(message):
            return
        try:
            name = await task.resolve((command.args or "").strip() or None)
            await jobs.stop_quota_task(name, message.from_user.id)  # type: ignore[union-attr]
            if await task.any_enabled():
                await message.answer(f"限额任务 {html.escape(name)} 已停止；其他任务仍在运行。")
            else:
                await message.answer(f"限额任务 {html.escape(name)} 已停止，写操作已关闭；用量数据仍会继续同步统计。")
        except DomainError as exc:
            await message.answer(html.escape(str(exc)))
        except Exception:
            await message.answer("停止限额任务失败，请检查服务日志。")

    @router.message(Command("startstats"))
    async def start_stats(message: Message, jobs: BackgroundJobs) -> None:
        if not is_admin(message):
            return
        try:
            await jobs.start_usage_sync(message.from_user.id)  # type: ignore[union-attr]
            await message.answer("数据统计已启动，用量同步循环运行中。")
        except Exception:
            await message.answer("启动数据统计失败，请检查服务日志。")

    @router.message(Command("stopstats"))
    async def stop_stats(message: Message, jobs: BackgroundJobs) -> None:
        if not is_admin(message):
            return
        try:
            await jobs.stop_usage_sync(message.from_user.id)  # type: ignore[union-attr]
            await message.answer("数据统计已停止，用量同步循环已关闭；限额任务与写闸门状态保持不变。")
        except Exception:
            await message.answer("停止数据统计失败，请检查服务日志。")

    @router.message(Command("settaskquota"))
    async def set_task_quota(message: Message, command: CommandObject, admin: AdminService) -> None:
        if not is_admin(message):
            return
        values = (command.args or "").split()
        if len(values) == 2:
            name_arg: str | None = values[0]
            amount_arg = values[1]
        elif len(values) == 1:
            name_arg, amount_arg = None, values[0]
        else:
            await message.answer("用法：/settaskquota &lt;任务名&gt; 金额（仅一个任务时可省略任务名）")
            return
        try:
            amount = Decimal(amount_arg)
        except InvalidOperation:
            await message.answer("用法：/settaskquota &lt;任务名&gt; 金额")
            return
        try:
            name, value = await admin.set_task_quota(name_arg, amount, message.from_user.id)  # type: ignore[union-attr]
            await message.answer(f"任务 {html.escape(name)} 每用户额度已设置为 ${value:.2f}", skip_auto_delete=True)
        except DomainError as exc:
            await message.answer(html.escape(str(exc)))

    @router.message(Command("addtaskmember"))
    async def add_task_member(message: Message, command: CommandObject, task: QuotaTaskService) -> None:
        if not is_admin(message):
            return
        try:
            values = (command.args or "").split()
            name, ids = await task.resolve_members_args(values, usage="用法：/addtaskmember <任务名> <reclaude_user_id> ...（仅一个任务时可省略任务名）")
            result = await task.add_members(name, ids, message.from_user.id)  # type: ignore[union-attr]
            if len(ids) == 1 and ids[0].casefold() == "all":
                await message.answer(f"任务 {html.escape(name)} 成员范围已切回 ALL，旧成员名单已清理。")
            else:
                snapshot = await task.snapshot(name)
                if snapshot.scope_mode == EXCLUDE:
                    await message.answer(f"已将成员重新纳入任务 {html.escape(name)}：{html.escape(', '.join(result))}（范围为 EXCLUDE，剩余排除 {len(snapshot.member_ids)} 个）")
                else:
                    await message.answer(f"已加入任务 {html.escape(name)} 成员：{html.escape(', '.join(result))}（范围为 ALLOWLIST）")
        except DomainError as exc:
            await message.answer(f"成员范围更新失败：{html.escape(str(exc))}")

    @router.message(Command("deletetaskmember"))
    async def delete_task_member(message: Message, command: CommandObject, task: QuotaTaskService) -> None:
        if not is_admin(message):
            return
        try:
            values = (command.args or "").split()
            name, ids = await task.resolve_members_args(values, usage="用法：/deletetaskmember <任务名> <reclaude_user_id> ...（仅一个任务时可省略任务名）")
            result = await task.delete_members(name, ids, message.from_user.id)  # type: ignore[union-attr]
            snapshot = await task.snapshot(name)
            if snapshot.scope_mode == EXCLUDE:
                await message.answer(f"已将成员从任务 {html.escape(name)} 排除：{html.escape(', '.join(result))}（范围为 EXCLUDE，其余及新加入成员仍被覆盖）")
            else:
                await message.answer(f"已移除任务 {html.escape(name)} 成员：{html.escape(', '.join(result))}；剩余为空时不会执行任何配额动作。")
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
            except Exception as exc:
                log.error(
                    "reclaude_account_listing_failed",
                    error_type=type(exc).__name__,
                    traceback="".join(traceback.format_tb(exc.__traceback__)),
                )
                await message.answer("账号查询失败，请检查 Reclaude 登录和会话状态。")
            return
        try:
            await recovery.health_sync_reconcile_enable(message.from_user.id)  # type: ignore[union-attr]
            await message.answer("账号、周期和成员健康检查完成；限额任务仍为 STOPPED，请使用 /starttask 显式启动。")
        except DomainError as exc:
            await message.answer(html.escape(str(exc)))
        except Exception:
            await message.answer("恢复失败，写操作仍已暂停，请检查 Reclaude 登录和账号状态。")

    @router.message(Command("update"))
    async def update(message: Message, updater: UpdateService) -> None:
        if not is_admin(message):
            return
        if not updater.available:
            await message.answer("自动更新不可用：容器未挂载 Docker socket。")
            return
        if updater.in_progress:
            await message.answer("已有更新正在进行中。")
            return
        async with updater.run():
            await message.answer("正在检查并拉取最新镜像…")
            try:
                check = await updater.check_for_update()
            except UpdateError as exc:
                await message.answer(f"更新失败：{exc}")
                return
            if not check.changed:
                await message.answer(f"当前已是最新版本（{check.image_spec}）。")
                return
            await message.answer("发现新版本，正在更新，Bot 将短暂离线后自动恢复，完成后会通知你。")
            try:
                await updater.apply_update(check, message.chat.id)
            except UpdateError as exc:
                await message.answer(f"更新失败：{exc}")

    return router


def _format_datetime(value: object) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else "unknown"


async def _account_usage_lines(quota: QuotaService) -> list[str]:
    """Live account usage windows for /taskusers; degrades to a notice when /me fails."""

    try:
        account = await quota.get_account_usage()
    except Exception:
        return ["账号用量：暂时不可用（上游查询失败）"]
    five_hour_reset = _format_datetime(account.five_hour_resets_at) if account.five_hour_resets_at is not None else "未激活"
    return [
        f"账号：{html.escape(account.email_masked)}（快照 {_format_datetime(account.usage_updated_at)}）",
        f"5h 限额：已用 {_format_percent(account.five_hour_utilization)} | 重置：{five_hour_reset}",
        f"7天限额：已用 {_format_percent(account.seven_day_utilization)} | 重置：{_format_datetime(account.seven_day_resets_at)} | 预估总额度：{_format_estimated_total(account.seven_day_estimated_total)}",
    ]


def _format_percent(value: Decimal | None) -> str:
    return f"{value:.1f}%" if value is not None else "未知"


def _format_estimated_total(value: Decimal | None) -> str:
    return f"≈${value:.2f}" if value is not None else "—"
