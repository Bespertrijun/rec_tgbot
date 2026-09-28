# 设备版首次上线操作

2026-09-28 已确认：旧版剩余额度作废，不导入新版；旧数据库保留备份，不删除。`legacy` 保存旧代码，`main` 为设备版。

## 首次关联如何计费

- `/authuser 邮箱 device_id` 关联已授权设备：首次成功采集的 `range=all` 累计金额作为基线，只累计之后的增长。例如首次 $200，之后 $230，新版确认消费为 $30。不会自动扣除关联前的 $200。
- 此路径标记 `NEEDS_REVIEW`（用量待核对），因为无法确认关联到首次采集之间的消费。后续采集不会自动消除这个标记；已有设备仍可使用、确认增量仍会累计并参与达额处理，但普通重新授权及转出额度可能被待核对状态阻止。当前没有一条管理员命令可直接核定这个基线。
- 用户通过 `/auth 授权链接` 批准全新设备，且 REC 返回 `reused=false`：基线为零，首次采集到的累计消费计入账本。首次采集失败保留设备，后台重试。
- `/bind 邮箱` 只绑定 Telegram 身份，不关联设备、不查询设备用量。
- 个人额度来自任务，例如 `/newtask main 700` 设置每人 $700；REC 周期重置时间仍决定本周期结束时间。旧余额作废不会重置 REC 的账号周用量。

## 服务器操作

以下按独立目录、全新设备版数据库部署。因此四个用户需重新 `/bind`，任务名单和群管理设置也需重新配置。需要保留旧用户和群配置时，应另外安排旧数据库恢复及升级，不直接套用此全新部署流程。

1. 等待 `main` 的 GitHub Actions 检查与镜像发布成功，确认 `device-latest` 已更新。首次切换不要使用旧 bot 的 `/update`。
2. 在旧部署目录备份 PostgreSQL，妥善保存旧 `.env` 和持久化数据。可用下列命令（默认服务名 `db`）：

   ```sh
   umask 077
   docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > legacy-before-device.dump
   docker compose exec -T db pg_restore --list < legacy-before-device.dump > legacy-before-device.contents
   ```

   列出备份目录只校验归档可读；正式切换前还应在隔离数据库恢复验证。
3. 创建独立目录，例如 `/srv/reclaude-device`。将仓库 `docker-compose.device.yml` 放入该目录，并以 `.env.example` 为模板新建权限 `0600` 的 `.env`。填写 Telegram token、管理员 TG ID、REC 管理员登录凭证、`RECLAUDE_ORG_ID=178`、新的数据库密码，以及匹配密码的 `DATABASE_URL`（主机名 `db`）。不要复用旧数据库目录。
4. 在旧部署目录执行 `docker compose stop bot`。必须停掉旧 bot 进程，不能只执行 `/stoptask`；相同 Telegram token 不可同时运行两个 bot。
5. 在新部署目录执行：

   ```sh
   docker compose -f docker-compose.device.yml pull
   docker compose -f docker-compose.device.yml up -d
   docker compose -f docker-compose.device.yml logs --tail=100 bot
   ```

   启动会自动运行数据库迁移。PostgreSQL 目录若出现权限错误，按 `deployment.md` 的镜像实际 UID/GID 步骤处理，目录使用 `data-device/postgres`。

## Telegram 初始化

管理员私聊依次执行：

```text
/account
/use <account 返回的账号ID>
/newtask main 700
/startstats
/sync
```

让四个用户分别私聊 `/bind 自己的邮箱`。管理员用 `/member` 查看本地用户 ID，再限定任务范围：

```text
/addtaskmember main <用户ID1> <用户ID2> <用户ID3> <用户ID4>
/device
```

此处的用户 ID 是 bot 本地 ID，不是 Telegram ID 或 REC device ID。
管理员按实际设备归属执行 `/authuser 邮箱 device_id main`；或者让用户提交全新授权链接 `/auth https://www.recode.cat/cli/auth?state=...`。不要混淆已有设备关联与全新授权的计费起点。

随后执行 `/sync`、`/taskusers main`，用户用 `/status` 核对关联、额度及用量状态。确认后执行 `/starttask main` 开启自动限额。任务暂停期间仍允许授权，但不会自动达额撤销；不要长期停留在此阶段。

群管理使用全新数据库，需重新配置并确认群权限。首次人工部署不需要开启 `DEVICE_DEPLOY_ENABLED`；只有决定后续自动部署时，才设置独立的 device 部署变量与凭证。
