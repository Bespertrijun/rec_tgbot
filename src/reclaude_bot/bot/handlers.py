from __future__ import annotations

import html
import traceback
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal, InvalidOperation

import structlog
from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, MessageEntity
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reclaude_bot.application.admin import AdminService
from reclaude_bot.application.audit import utcnow
from reclaude_bot.application.binding import BindingService, masked_email, normalize_email
from reclaude_bot.application.device import DeviceAuthorizationService
from reclaude_bot.application.device_admin import DeviceAdminService
from reclaude_bot.application.device_cycle import DeviceCycleService
from reclaude_bot.application.device_quota import DeviceQuotaService
from reclaude_bot.application.device_reset import DeviceTaskResetService
from reclaude_bot.application.device_revocation import DeviceRevocationService
from reclaude_bot.application.device_sampling import DeviceSamplingService
from reclaude_bot.application.device_task_members import DeviceTaskMemberService
from reclaude_bot.application.onboarding import OnboardingService
from reclaude_bot.application.recovery import RecoveryService
from reclaude_bot.application.task import ALLOWLIST, EXCLUDE, QuotaTaskService
from reclaude_bot.application.updater import UpdateError, UpdateService
from reclaude_bot.bot.middleware import GroupAccessMiddleware
from reclaude_bot.config import Settings
from reclaude_bot.domain.errors import DomainError, EligibilityError
from reclaude_bot.domain.timefmt import format_beijing
from reclaude_bot.infrastructure.db.models import User
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
            user = await binding.bind(
                message.from_user.id,
                command.args.strip(),
                private_chat=message.chat.type == "private",
                telegram_username=message.from_user.username,
            )
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
            await message.answer("绑定失败：请确认在私聊中操作、邮箱格式正确且未被占用；账号受限请联系管理员。")

    @router.message(Command("status"))
    async def status(message: Message, device_quota: DeviceQuotaService) -> None:
        try:
            if message.from_user is None:
                return
            await _record_username_safely_from_store(device_quota.session_factory, message.from_user.id, message.from_user.username)
            local_user = await _find_local_user(device_quota.session_factory, telegram_user_id=message.from_user.id)
            if local_user is None:
                await message.answer("你还没有绑定账号，请先私聊 Bot 使用 /bind 邮箱。")
                return
            value = await device_quota.status(local_user[0])
            email = local_user[1]
            local, _, domain = email.partition("@")
            if value.association_state == "UNKNOWN" and value.pending_action_kind == "AUTH":
                auth_state = "授权结果待核对（请勿重发链接）"
            elif value.association_state == "UNKNOWN" and value.pending_action_kind == "REVOKE":
                auth_state = "撤销结果待核对"
            elif value.association_state == "UNKNOWN":
                auth_state = "设备关联状态待核对"
            elif value.association_state == "PENDING_AUTH":
                auth_state = "授权处理中"
            elif value.association_state == "PENDING_REVOKE":
                auth_state = "撤销处理中"
            elif value.association_state == "ACTIVE":
                auth_state = "已授权"
            else:
                auth_state = "未关联"
            if value.association_state == "ACTIVE" and value.last_sampled_at is None:
                used = "首采待同步"
            else:
                used = f"${value.used_usd:.2f}" if value.used_usd is not None else "待同步"
            device = (
                str(value.device_id)
                if value.device_id is not None
                else "待核对"
                if value.association_state in {"PENDING_AUTH", "UNKNOWN"}
                else "无"
            )
            limit = f"${value.effective_limit_usd:.2f}" if value.effective_limit_usd is not None else "未知"
            remaining = f"${value.remaining_usd:.2f}" if value.remaining_usd is not None else "未知"
            await message.answer(
                f"邮箱：{(local[:1] or '*')}***@{domain}\n"
                f"授权状态：{auth_state}\n"
                f"设备：{device}\n"
                f"任务：{html.escape(value.task_name or '未配置')}\n"
                f"本周期已用：{used}\n"
                f"当前额度：{limit}\n"
                f"剩余额度：{remaining}\n"
                f"数据质量：{html.escape(value.quality)}\n"
                f"额度锁定：{'是' if value.quota_locked else '否'}\n"
                f"最近采样：{_format_datetime(value.last_sampled_at)}\n"
                f"周期刷新：{_format_datetime(value.reset_at)}"
            )
        except DomainError as exc:
            await message.answer(html.escape(str(exc)))

    @router.message(Command("auth"))
    async def auth_device(message: Message, command: CommandObject, device_auth: DeviceAuthorizationService, device_quota: DeviceQuotaService) -> None:
        if message.chat.type != "private" or message.from_user is None:
            await message.answer("请在 Bot 私聊中发送 /auth 授权链接。")
            return
        if not command.args or not command.args.strip():
            await message.answer("用法：/auth 授权链接")
            return
        try:
            local_user = await _find_local_user(device_quota.session_factory, telegram_user_id=message.from_user.id)
            if local_user is None:
                raise EligibilityError("你还没有绑定账号，请先私聊 Bot 使用 /bind 邮箱")
            result = await device_auth.auth(local_user[0], command.args.strip())
            if result.status == "SUCCEEDED":
                await message.answer(f"设备授权成功，设备 ID：{result.device_id}。")
            elif result.status == "FAILED":
                await message.answer("设备授权未成功，请重新获取新的授权链接后再试。")
            else:
                await message.answer("授权结果暂未确认，请等待后台核对或联系管理员；不要重发此链接。")
        except DomainError as exc:
            await message.answer(html.escape(str(exc)))
        except Exception:
            await message.answer("设备授权暂时无法完成，请稍后重试或联系管理员核对。")

    @router.message(Command("deauth"))
    async def deauth_device(message: Message, device_revocation: DeviceRevocationService, device_quota: DeviceQuotaService) -> None:
        if message.chat.type != "private" or message.from_user is None:
            await message.answer("请在 Bot 私聊中使用 /deauth。")
            return
        try:
            local_user = await _find_local_user(device_quota.session_factory, telegram_user_id=message.from_user.id)
            if local_user is None:
                raise EligibilityError("你还没有绑定账号")
            result = await device_revocation.deauth(local_user[0])
            if result is None:
                await message.answer("当前没有未结束的设备关联。")
            elif result.status == "SUCCEEDED":
                await message.answer("设备撤销已确认。")
            else:
                await message.answer("设备撤销结果暂未确认，后台会继续核对；请勿重复提交。")
        except DomainError as exc:
            await message.answer(html.escape(str(exc)))
        except Exception:
            await message.answer("设备撤销暂时无法完成，请稍后查询 /status 或联系管理员。")

    @router.message(Command("send"))
    async def send(message: Message, device_quota: DeviceQuotaService) -> None:
        if message.from_user is None:
            await message.answer("无法识别发送者：匿名管理员或频道身份不能使用 /send，请换回本人身份后重试。", skip_auto_delete=True)
            return
        if message.chat.type == "private":
            await message.answer("只能在群组中使用：请在群里 @对方 后转账。")
            return
        await _record_username_safely_from_store(device_quota.session_factory, message.from_user.id, message.from_user.username)
        entities = list(message.entities or [])
        mention = next((entity for entity in entities if entity.type in {"mention", "text_mention"}), None)
        if mention is None:
            await message.answer("用法：/send @对方 金额")
            return
        text = message.text or ""
        spans = [entity for entity in entities if entity.type == "bot_command"] + [mention]
        try:
            amount = Decimal(_text_without_entities(text, spans).strip())
        except InvalidOperation:
            await message.answer("用法：/send @对方 金额")
            return
        try:
            sender = await _find_local_user(device_quota.session_factory, telegram_user_id=message.from_user.id)
            if sender is None:
                raise EligibilityError("发送前请先私聊 Bot 使用 /bind 邮箱")
            if mention.type == "text_mention" and mention.user is not None:
                recipient = await _find_local_user(device_quota.session_factory, telegram_user_id=mention.user.id)
            else:
                recipient = await _find_local_user(
                    device_quota.session_factory,
                    username=mention.extract_from(text).lstrip("@"),
                )
            if recipient is None:
                raise EligibilityError("找不到已绑定的收款用户")
            current = await device_quota.status(sender[0])
            if current.cycle_id is None:
                raise EligibilityError("当前设备额度周期尚未核实，暂不能转账")
            result = await device_quota.transfer(
                sender[0],
                recipient[0],
                current.cycle_id,
                amount,
                operation_key=f"telegram:{message.chat.id}:{message.message_id}",
            )
        except DomainError as exc:
            log.info("quota_transfer_rejected", sender_telegram_id=message.from_user.id, error=str(exc))
            await message.answer(html.escape(str(exc)), skip_auto_delete=True)
            return
        except Exception as exc:
            log.error(
                "quota_transfer_failed",
                error_type=type(exc).__name__,
                traceback="".join(traceback.format_tb(exc.__traceback__)),
            )
            await message.answer("转账失败，请稍后重试。", skip_auto_delete=True)
            return
        await message.answer(
            f"已转账 ${result.amount_usd:.2f} 给 {html.escape(masked_email(recipient[1]))}，"
            f"你本周期剩余额度 ${result.sender_remaining_usd:.2f}。",
            skip_auto_delete=True,
        )

    return router


def build_admin_router(settings: Settings) -> Router:
    router = Router(name="admin")
    router.message.middleware(GroupAccessMiddleware())

    def is_admin(message: Message) -> bool:
        return message.from_user is not None and message.from_user.id in settings.telegram_admin_ids

    @router.message(Command("sync"))
    async def sync(message: Message, device_cycle: DeviceCycleService, device_sampling: DeviceSamplingService) -> None:
        if not is_admin(message):
            return
        try:
            cycle = await device_cycle.sync()
            samples = await device_sampling.tick()
            percent = f"{cycle.weekly_percent:.2f}%" if cycle.weekly_percent is not None else "未知"
            await message.answer(
                f"设备周期同步完成：{cycle.status} | 账号 {html.escape(str(cycle.account_id or '未知'))} | "
                f"周用量 {percent} | 补采任务处理 {len(samples)} 个"
            )
        except Exception:
            await message.answer("同步失败，已记录告警。")

    @router.message(Command("member"))
    async def member_list(message: Message, device_quota: DeviceQuotaService) -> None:
        if not is_admin(message):
            return
        try:
            async with device_quota.session_factory() as session:
                members = list((await session.scalars(select(User).order_by(User.id.asc()))).all())
            if not members:
                await message.answer("暂无本地用户")
                return
            lines = [f"本地用户：{len(members)} 个"]
            lines.extend(
                f"- 本地 ID {user.id} | {html.escape(user.email)} | {html.escape(user.binding_status)} | "
                f"{html.escape(user.status)} | TG {user.telegram_user_id}"
                for user in members
            )
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
                "local_member_listing_failed",
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
        if len(args) != 1 or not args[0].isdigit():
            await message.answer("用法：/unbind TelegramID；如果仍有关联设备，请先执行 /deauthuser 邮箱")
            return
        try:
            await binding.unbind(int(args[0]), operator_telegram_id=message.from_user.id, force_revoke=False)  # type: ignore[union-attr]
            await message.answer("解绑完成")
        except DomainError as exc:
            await message.answer(html.escape(str(exc)))

    @router.message(Command("device", "devices"))
    async def devices(message: Message, device_admin: DeviceAdminService) -> None:
        if not is_admin(message):
            return
        try:
            entries = await device_admin.list_devices()
            if not entries:
                await message.answer("当前组织暂无设备")
                return
            lines = [f"组织设备：{len(entries)} 个"]
            for item in entries:
                state = "已撤销" if item.revoked_at is not None else "可用"
                if item.owner_user_id is not None:
                    owner = f"本地用户 {item.owner_user_id} | {html.escape(item.owner_email or '')} | {item.association_state}"
                else:
                    owner = "未关联"
                lines.append(f"- {item.device_id} | {html.escape(item.name)} | {state} | {owner}")
            await _answer_lines(message, lines)
        except DomainError as exc:
            await message.answer(html.escape(str(exc)))
        except Exception:
            await message.answer("设备列表暂时不可用。")

    @router.message(Command("authuser"))
    async def auth_user_device(message: Message, command: CommandObject, device_admin: DeviceAdminService) -> None:
        if not is_admin(message):
            return
        raw_values = (command.args or "").split()
        used_usd: Decimal | None = None
        if "--used" in raw_values:
            if raw_values.count("--used") != 1:
                await message.answer("用法：/authuser 邮箱 设备ID [任务名] --used 金额")
                return
            used_index = raw_values.index("--used")
            if used_index != len(raw_values) - 2:
                await message.answer("用法：/authuser 邮箱 设备ID [任务名] --used 金额")
                return
            try:
                used_usd = Decimal(raw_values[-1])
            except InvalidOperation:
                await message.answer("--used 金额格式无效")
                return
            raw_values = raw_values[:used_index]
        values = raw_values
        if len(values) not in {2, 3} or not values[1].isdigit():
            await message.answer("用法：/authuser 邮箱 设备ID [任务名] [--used 金额]")
            return
        try:
            user_id = await _find_existing_user_by_email(device_admin.session_factory, values[0])
            operator_id = message.from_user.id  # type: ignore[union-attr]
            task_name = values[2] if len(values) == 3 else None
            if used_usd is None:
                result = await device_admin.authuser(
                    user_id,
                    int(values[1]),
                    operator_id,
                    task_name=task_name,
                )
            else:
                result = await device_admin.authuser(
                    user_id,
                    int(values[1]),
                    operator_id,
                    task_name=task_name,
                    used_usd=used_usd,
                )
            imported = f" | 当前周期已用导入 ${used_usd}" if used_usd is not None else ""
            await message.answer(
                f"设备关联已建立：{html.escape(masked_email(values[0]))} | 设备 {result.device_id} | "
                f"关联 {result.association_id}{imported}"
            )
        except DomainError as exc:
            await message.answer(html.escape(str(exc)))
        except Exception:
            await message.answer("管理员设备关联失败，请检查设备状态和本地额度。")

    @router.message(Command("deauthuser"))
    async def deauth_user_device(message: Message, command: CommandObject, device_revocation: DeviceRevocationService) -> None:
        if not is_admin(message):
            return
        values = (command.args or "").split()
        if len(values) != 1:
            await message.answer("用法：/deauthuser 邮箱")
            return
        try:
            user_id = await _find_existing_user_by_email(device_revocation.session_factory, values[0])
            result = await device_revocation.deauth(
                user_id,
                operator_id=message.from_user.id,  # type: ignore[union-attr]
            )
            if result is None:
                await message.answer("该用户当前没有未结束的设备关联。")
            elif result.status == "SUCCEEDED":
                await message.answer(f"设备撤销已确认：关联 {result.association_id}。")
            else:
                await message.answer(f"设备撤销结果待核对：关联 {result.association_id}，后台会继续核对。")
        except DomainError as exc:
            await message.answer(html.escape(str(exc)))
        except Exception:
            await message.answer("管理员设备撤销暂时无法完成，请检查服务日志。")

    @router.message(Command("audit"))
    async def audit_view(message: Message, admin: AdminService) -> None:
        if not is_admin(message):
            return
        rows = await admin.recent_audit()
        await message.answer("\n".join(f"{_format_datetime(row.created_at)} {row.action} {row.result}" for row in rows) or "暂无审计记录")

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
            await message.answer(
                f"Reclaude 账号 {account.account_id} 已校验并选择。任务保持 STOPPED，写闸未开启；"
                "设备周期和用量将由本地统计循环同步。旧余额不会在此操作中重置或迁移。"
            )
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
            f"使用 /addtaskmember {html.escape(snapshot.name)} &lt;本地用户ID&gt; 限定成员，/starttask {html.escape(snapshot.name)} 启动。"
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
    async def task_status(
        message: Message,
        command: CommandObject,
        task: QuotaTaskService,
        jobs: BackgroundJobs,
        device_cycle: DeviceCycleService,
        recovery: RecoveryService,
    ) -> None:
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
            cycle = await device_cycle.current(name)
            if cycle is None:
                lines.append("当前设备周期：尚未同步")
            else:
                weekly = f"{cycle.weekly_percent:.2f}%" if cycle.weekly_percent is not None else "未知"
                lines.append(f"设备周期：{cycle.status} | 周用量 {weekly} | 刷新 {_format_datetime(cycle.reset_at)}")
            await message.answer("\n".join(lines))
        except DomainError as exc:
            await message.answer(html.escape(str(exc)))
        except Exception:
            await message.answer("任务状态暂时不可用。")

    @router.message(Command("reset"))
    async def reset_task(message: Message, command: CommandObject, device_reset: DeviceTaskResetService) -> None:
        if not is_admin(message) or message.chat.type != "private":
            return
        values = (command.args or "").split()
        if len(values) != 1:
            await message.answer("用法：/reset 任务名")
            return
        try:
            result = await device_reset.reset(
                values[0],
                message.from_user.id,  # type: ignore[union-attr]
                operation_key=f"telegram:{message.chat.id}:{message.message_id}",
            )
        except DomainError as exc:
            await message.answer(html.escape(str(exc)))
            return
        except Exception:
            await message.answer("本地周期重置失败，任务和账本未重置，请稍后重试。")
            return
        await message.answer(
            f"任务 {html.escape(values[0])} 已重置为新本地周期：设备 {result.device_count} 个，"
            f"范围内用户 {result.user_count} 个，REC 周期刷新 {_format_datetime(result.reset_at)}。"
            "旧账本和审计历史已保留。"
        )

    @router.message(Command("taskusers"))
    async def task_users(
        message: Message,
        command: CommandObject,
        task: QuotaTaskService,
        device_task_members: DeviceTaskMemberService,
        device_quota: DeviceQuotaService,
    ) -> None:
        if not is_admin(message) or message.chat.type != "private":
            return
        try:
            name = await task.resolve((command.args or "").strip() or None)
            snapshot = await task.snapshot(name)
            members = await device_task_members.snapshot(name)
            async with device_quota.session_factory() as session:
                users = list(
                    (
                        await session.scalars(
                            select(User).where(User.id.in_(members.covered_user_ids)).order_by(User.id.asc())
                        )
                    ).all()
                ) if members.covered_user_ids else []
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
        if not users:
            if snapshot.scope_mode == ALLOWLIST:
                await message.answer(f"任务 {html.escape(snapshot.name)} 白名单为空，请使用 /addtaskmember 添加成员。")
            elif snapshot.scope_mode == EXCLUDE:
                await message.answer(f"任务 {html.escape(snapshot.name)} 范围内暂无本地用户。")
            else:
                await message.answer("任务范围内暂无本地用户。")
            return
        lines = [
            f"任务：{html.escape(snapshot.name)} | {'RUNNING' if snapshot.enabled else 'STOPPED'} | 范围：{snapshot.scope_mode} | "
            f"本地用户：{len(users)} 个 | 任务额度 ${snapshot.limit_usd:.2f}"
        ]
        for user in users:
            status = await device_quota.status(user.id, task_id=snapshot.id)
            used = f"${status.used_usd:.2f}" if status.used_usd is not None else "待同步"
            remaining = f"${status.remaining_usd:.2f}" if status.remaining_usd is not None else "未知"
            lines.append(
                f"- 本地 ID {user.id} | {html.escape(user.email)} | {user.binding_status}/{user.status} | "
                f"设备 {status.device_id if status.device_id is not None else '无'} | 已用 {used} | 剩余 {remaining} | {status.quality}"
            )
        await _answer_lines(message, lines)

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
    async def set_task_quota(message: Message, command: CommandObject, task: QuotaTaskService) -> None:
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
            name, value = await task.set_limit(name_arg, amount, message.from_user.id)  # type: ignore[union-attr]
            await message.answer(f"任务 {html.escape(name)} 每用户额度已设置为 ${value:.2f}", skip_auto_delete=True)
        except DomainError as exc:
            await message.answer(html.escape(str(exc)))

    @router.message(Command("addtaskmember"))
    async def add_task_member(
        message: Message,
        command: CommandObject,
        task: QuotaTaskService,
        device_task_members: DeviceTaskMemberService,
    ) -> None:
        if not is_admin(message):
            return
        try:
            values = (command.args or "").split()
            name, raw_ids = await task.resolve_members_args(
                values,
                usage="用法：/addtaskmember <任务名> <本地用户ID> ...（仅一个任务时可省略任务名）",
            )
            if len(raw_ids) == 1 and raw_ids[0].casefold() == "all":
                await device_task_members.reset_all(name, message.from_user.id)  # type: ignore[union-attr]
                await message.answer(f"任务 {html.escape(name)} 成员范围已切回 ALL。")
                return
            ids = _parse_local_ids(raw_ids)
            result = await device_task_members.add_members(name, ids, message.from_user.id)  # type: ignore[union-attr]
            snapshot = await device_task_members.snapshot(name)
            rendered = ", ".join(str(value) for value in result)
            if snapshot.scope_mode == EXCLUDE:
                await message.answer(f"已将本地用户重新纳入任务 {html.escape(name)}：{rendered}（范围为 EXCLUDE，剩余排除 {len(snapshot.member_ids)} 个）")
            else:
                await message.answer(f"已加入任务 {html.escape(name)} 本地用户：{rendered}（范围为 ALLOWLIST）")
        except (DomainError, ValueError) as exc:
            await message.answer(f"成员范围更新失败：{html.escape(str(exc))}")

    @router.message(Command("deletetaskmember"))
    async def delete_task_member(
        message: Message,
        command: CommandObject,
        task: QuotaTaskService,
        device_task_members: DeviceTaskMemberService,
    ) -> None:
        if not is_admin(message):
            return
        try:
            values = (command.args or "").split()
            name, raw_ids = await task.resolve_members_args(
                values,
                usage="用法：/deletetaskmember <任务名> <本地用户ID> ...（仅一个任务时可省略任务名）",
            )
            ids = _parse_local_ids(raw_ids)
            result = await device_task_members.delete_members(name, ids, message.from_user.id)  # type: ignore[union-attr]
            snapshot = await device_task_members.snapshot(name)
            rendered = ", ".join(str(value) for value in result)
            if snapshot.scope_mode == EXCLUDE:
                await message.answer(f"已将本地用户从任务 {html.escape(name)} 排除：{rendered}（范围为 EXCLUDE，其余及新加入用户仍被覆盖）")
            else:
                await message.answer(f"已移除任务 {html.escape(name)} 本地用户：{rendered}；ALLOWLIST 为空时不会执行任何配额动作。")
        except (DomainError, ValueError) as exc:
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
            await message.answer("所选账号已通过登录与健康校验；任务保持 STOPPED，写闸未开启。需要启用配额时请使用 /starttask。")
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
    if isinstance(value, datetime):
        return format_beijing(value)
    return value.isoformat() if hasattr(value, "isoformat") else "unknown"


async def _record_username_safely_from_store(
    factory: async_sessionmaker[AsyncSession],
    telegram_user_id: int,
    username: str | None,
) -> None:
    """Refresh the local username cache without breaking the calling command."""
    try:
        async with factory() as session:
            async with session.begin():
                user = await session.scalar(
                    select(User).where(User.telegram_user_id == telegram_user_id).with_for_update()
                )
                if user is not None:
                    user.telegram_username = username.casefold() if username else None
                    user.updated_at = utcnow()
    except Exception as exc:
        log.warning("telegram_username_refresh_failed", telegram_user_id=telegram_user_id, error=str(exc))


async def _find_local_user(
    factory: async_sessionmaker[AsyncSession],
    *,
    telegram_user_id: int | None = None,
    username: str | None = None,
) -> tuple[int, str] | None:
    if (telegram_user_id is None) == (username is None):
        raise EligibilityError("需要指定一个 Telegram 用户")
    async with factory() as session:
        statement = select(User.id, User.email)
        if telegram_user_id is not None:
            statement = statement.where(User.telegram_user_id == telegram_user_id)
        else:
            statement = statement.where(User.telegram_username == str(username).casefold())
        rows = (await session.execute(statement.limit(2))).all()
    if len(rows) > 1:
        raise EligibilityError("该用户名对应多个本地用户，请使用 Telegram 用户提及")
    if not rows:
        return None
    return rows[0].id, rows[0].email


async def _find_existing_user_by_email(
    factory: async_sessionmaker[AsyncSession],
    email: str,
) -> int:
    normalized = normalize_email(email)
    async with factory() as session:
        user_id = await session.scalar(select(User.id).where(User.email_normalized == normalized))
    if user_id is None:
        raise EligibilityError("该邮箱未绑定本地用户；请先完成 /bind，不会自动创建用户")
    return user_id


def _parse_local_ids(values: list[str]) -> list[int]:
    if not values or any(not value.isdigit() or int(value) <= 0 for value in values):
        raise EligibilityError("本地用户 ID 必须是正整数")
    return [int(value) for value in values]


async def _answer_lines(message: Message, lines: list[str]) -> None:
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


def _text_without_entities(text: str, entities: Iterable[MessageEntity]) -> str:
    """Drop entity spans from raw message text; entity offsets are UTF-16 code units."""
    encoded = text.encode("utf-16-le")
    for entity in sorted(entities, key=lambda item: (item.offset, item.length), reverse=True):
        start, end = entity.offset * 2, (entity.offset + entity.length) * 2
        encoded = encoded[:start] + encoded[end:]
    return encoded.decode("utf-16-le")
