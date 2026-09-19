# 计划：新增 `/member` 命令显示上游成员

来源会话：`session_c8ec21a4`（2026-09-07，计划模式报错中断，改为书面计划）。

## 目标

新增管理员命令 `/member`：列出全部上游成员的 `email` + `reclaude_user_id`，解决"想 `/addtaskmember` 却无处可查 ID"的缺口。

## 改动点

### 1. 注册命令 — `src/reclaude_bot/bot/commands.py:18`

- `_ADMIN_COMMANDS` 加一项 `("member", "查看上游成员列表")`，建议放在 `sync` 之后（语义相近）。
- 注意 `tests/unit/test_commands.py:32-54` 硬编码了完整命令列表断言，必须同步加，否则单测红。

### 2. 查询逻辑 — `src/reclaude_bot/application/quota.py`

- 给 `QuotaService` 加一个小方法（如 `list_upstream_members()`）：用现有 `self.session_factory` 查 `UpstreamMember` 表，`ORDER BY email_normalized`，返回行列表。
- 数据源用本地缓存表而非实时调 API，理由：`/task` 的"最近成员同步"就是这么读的；不引入新的失败模式；缓存由 `/sync`、`/use` 和轮询定期刷新，够新。
- 注意语义：`upstream_members` 是上游组织**全部**成员的缓存，与是否 `/bind` 无关（同步时 `user_id=user.id if user else None`，quota.py:184；`/addtaskmember` 校验也是对实时上游列表而非 `users` 表）。未绑定 Telegram 的成员必须照样列出，只列已绑定的会漏人。
- 放 service 层而不是 handler 直接查库，理由是集成测试有现成 fixture（`tests/integration/conftest.py:39` 的 `app_context` + `FakeReclaudeGateway`）可以直接覆盖，handler 保持纯格式化。

### 3. Handler — `src/reclaude_bot/bot/handlers.py`（`build_admin_router` 内，参考 `/task` 在 198-216 行的写法）

- `@router.message(Command("member"))`，注入 `quota: QuotaService`，`is_admin` 检查照抄。
- 输出格式：头部一行"上游成员：N 个 | 最近同步：<时间>"（时间用现成的 `_format_datetime`，取 `max(sampled_at)`，顺便让管理员判断数据是否过期）；下面每行 `- email | reclaude_user_id`，`html.escape` 处理。
- 空表时回"暂无上游成员，请先执行 /sync"。
- **Telegram 4096 字符上限**：按行累积、约 4000 字符切一刀分多条 `message.answer` 发送。代码库目前没有分块先例，需要新写一个简单循环——`/task` 拼一长串 ID 就有这个隐患，`/member` 一次做对。
- 异常兜底照 `/account` 的模式：`log.error` + 回一句"成员列表暂时不可用"。

### 4. 测试

- `tests/unit/test_commands.py`：两处列表断言加 `member`。
- `tests/integration/` 新增用例（可放 `test_task_controls.py` 或新文件）：用 `app_context` 落几个成员 → 调 `list_upstream_members()` → 断言数量、排序、字段；空表返回空列表。

### 5. 文档 — `docs/operations.md:25-27`

- 成员段加一句：`/member` 列出上游成员 email + ID，供 `/addtaskmember` 使用。

## 验证

- `pytest tests/unit/test_commands.py tests/integration/ -q`
- `ruff check src tests`、`mypy src`
- 部署后菜单由启动时的 `register_command_menus` 自动更新，无需额外操作；无数据库 migration。

## 明确不做（一期）

- 不 join `users` 表显示绑定状态（谁绑了 Telegram）——有用但非本次需求，需要时后续加一列即可。
- 不改 `/addtaskmember` 的报错文案附带可用 ID——可作为后续优化。
- 不动 `/task` 现有的 4096 隐患——顺带修会扩大 diff，建议单独处理。
