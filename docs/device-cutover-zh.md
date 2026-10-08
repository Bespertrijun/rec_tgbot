# 设备版首次上线操作

2026-09-28 已确认：旧版剩余额度作废，不导入新版；旧数据库保留备份，不删除。`legacy` 保存旧代码，`main` 为设备版。

## 首次关联如何计费

- 本次迁移使用 `/authuser 邮箱 device_id device --used 500`，明确该用户本周期已用 $500。bot 同时读取设备当前 `all` 为基线；之后累计增加 $20，用户本周期已用变成 $520。`--used` 是已用消费，不是额度，也不是额外赠送；已达任务额度时由运行中的限额任务撤销，最后24小时豁免仍适用。
- 仅首次迁移关联可设置 `--used`，不能用于覆盖已有周期账本。相同导入重发不会重复记账；修改金额或给已普通关联的设备补填参数会被拒绝。查询基线失败时不会建立本次关联，可稍后重试。
- 不带 `--used` 的 `/authuser 邮箱 device_id` 关联已授权设备：首次成功采集的 `range=all` 累计金额作为基线，只累计之后的增长。例如首次 $200，之后 $230，新版确认消费为 $30。不会自动扣除关联前的 $200。
- 此路径标记 `NEEDS_REVIEW`（用量待核对），因为无法确认关联到首次采集之间的消费。后续采集不会自动消除这个标记；已有设备仍可使用、确认增量仍会累计并参与达额处理，但普通重新授权及转出额度可能被待核对状态阻止。当前没有一条管理员命令可直接核定这个基线。
- 用户通过 `/auth 授权链接` 批准全新设备，且 REC 返回 `reused=false`：基线为零，首次采集到的累计消费计入账本。首次采集失败保留设备，后台重试。
- `/bind 邮箱` 只绑定 Telegram 身份，不关联设备、不查询设备用量。
- 首次个人额度来自任务，例如 `/newtask device 700` 设置每人 $700；后续开轮按下文公式自动分配；REC 周期重置时间仍决定本周期结束时间。旧余额作废不会重置 REC 的账号周用量。

## 服务器操作

迁移完整旧数据库，保留邮箱与 Telegram 绑定、用户 ID、群配置、审计记录和旧历史表，用户无需重新 `/bind`。独立目录只隔离新旧运行环境，不代表清空业务数据。旧余额表保留历史，但新版设备账本不读取旧余额。

1. 等待 `main` 的 GitHub Actions 检查与镜像发布成功，确认 `device-latest` 已更新。首次切换不要使用旧 bot 的 `/update`。
2. 创建独立目录，例如 `/srv/reclaude-device`，放入 `docker-compose.device.yml`。保留旧部署 `.env` 的备份，在新目录安全复制配置并设置权限 `0600`；保留 Telegram、REC、群相关配置，核对 `RECLAUDE_ORG_ID=178`、PostgreSQL 密码及 `DATABASE_URL`（新 Compose 主机名 `db`）。旧数据目录保持不动。
3. 在旧部署目录先停止旧 bot，再做最终完整备份，避免备份后仍有人修改绑定。默认服务名为 `bot` 和 `db`：

   ```sh
   docker compose stop bot
   umask 077
   docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > legacy-before-device.dump
   docker compose exec -T db pg_restore --list < legacy-before-device.dump > legacy-before-device.contents
   ```

   妥善保存旧 `.env`、cookie 文件和完整数据。将 dump 安全复制到新部署目录。不要删除旧数据库或旧容器卷。相同 Telegram token 不可同时运行两个 bot。
4. 在新部署目录先只启动数据库，恢复完整旧库；不要在恢复前启动新 bot：

   ```sh
   docker compose -f docker-compose.device.yml pull
   docker compose -f docker-compose.device.yml up -d --wait db
   docker compose -f docker-compose.device.yml exec -T db sh -c 'pg_restore --exit-on-error --no-owner --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < legacy-before-device.dump
   ```

   此恢复命令只用于尚未启动新版、业务表为空的新目标库，不要重复执行或加 `--clean` 覆盖运行中的库。若旧库来自更高 PostgreSQL 主版本，应先确认目标版本兼容。数据库目录权限处理见 `deployment.md`，新目录使用 `data-device/postgres`。
5. 恢复成功后启动新 bot，自动执行 Alembic 增量迁移：

   ```sh
   docker compose -f docker-compose.device.yml up -d bot
   docker compose -f docker-compose.device.yml logs --tail=100 bot
   ```

   这些迁移新增设备表并调整旧用户字段可空性，不清空用户绑定或群表。对照旧库确认用户 ID、邮箱、Telegram ID、群配置一致后再开启限额。若启动失败，保留日志和恢复后的数据库，不删除旧库；回退只能在停掉新 bot 后使用旧库启动旧 bot。

## Telegram 初始化

管理员先 `/member` 确认原有用户和绑定仍在，再私聊依次执行：

```text
/account
/use <account 返回的账号ID>
/newtask device 700
/startstats
/sync
```

用户无需重新绑定。旧成员分配任务保留历史，不直接成为设备任务；创建一个不与旧任务重名的设备任务（以下使用 `device`），用 `/member` 中保留的本地用户 ID 配置其范围：

```text
/addtaskmember device <用户ID1> <用户ID2> <用户ID3> <用户ID4>
/device
```

此处的用户 ID 是 bot 本地 ID，不是 Telegram ID 或 REC device ID。
本次迁移，管理员按实际设备归属和已核对的本周期消费执行 `/authuser 邮箱 device_id device --used 已用金额`（例如 `--used 700`）。不导入历史消费的普通关联仍用 `/authuser 邮箱 device_id device`；或者让用户提交全新授权链接 `/auth https://www.recode.cat/cli/auth?state=...`。同周期换设备时，旧设备已确认的消费始终保留并与新设备消费相加；新设备从零计数不代表用户账本归零。

随后执行 `/sync`、`/taskusers device`，用户用 `/status` 核对关联、额度及用量状态。确认后执行 `/starttask device` 开启自动限额。任务暂停期间仍允许授权，但不会自动达额撤销；不要长期停留在此阶段。

群配置随完整旧库恢复，保留原配置并核对 bot 的实际群权限。`/use` 应校验原已选账号，不要切换账号；设备任务命令显式带任务名，避免旧历史任务影响省略名称的解析。首次人工部署不需要开启 `DEVICE_DEPLOY_ENABLED`；只有决定后续自动部署时，才设置独立的 device 部署变量与凭证。

## REC 手动重置后的任务额度重置

在 REC 完成账号额度重置后，管理员私聊发送：

```text
/reset device
```

`device` 是任务名。bot 会重新读取 REC 周期，并采集所有仍关联设备的 `all` 用量作为新基线，再一次性开启新的本地计费轮次：用户已用归零，每人基础额度按 **（上一轮最后有效预估总额度 − $100）÷ 4** 确定，最低 $0，向下保留两位小数；本期调整和转赠不带入新轮。旧账本与调整记录保留。绑定、设备关联、成员范围、任务运行状态及写闸门状态不变。

`/reset` 会先主动刷新 REC 账号用量，再读取 `/me`，不受普通查询的 3 小时刷新冷却限制。刷新失败或上游仍返回旧快照时不会开启新轮。刚重置后托管设备消费为零，预估总额度暂时显示 `—` 属于正常情况；有有效消费和使用率后才可估算。

自然周期刷新也使用同一公式。使用重置卡后，即使 REC 显示的结束时间不变，`/reset` 仍会开启独立新轮。例如第一轮 100% 消费 $2400，重置后每人 $575；新轮 100% 消费 $2700 时，当前每人仍为 $575；再次自然刷新或 `/reset` 后每人变为 $650。两轮消费不合并。

后台采样后保存本轮有效预估，不需要管理员查询；每人额度只在开新轮时自动计算一次。`/settaskquota` 仍能覆盖当前限额，下一轮重新按公式计算。无上一轮有效预估（包括升级后尚未保存过预估）时，沿用现有任务额度并提示原因。`/task device` 可查看开轮额度来源及本轮已保存预估的快照时间。未采集到重置前最终用量时，只能使用最后保存的有效预估。

检测到同周期使用率下降或账号/周期异常后，暂停旧轮预估更新并保留此前有效值，等待 `/reset` 建立新轮。重置、额度变更和基线保存一起提交，失败不会只改额度。部署此版本前需执行 `alembic upgrade head` 增加预估与额度来源字段；迁移不会追溯调整现有额度。

例如设备当前累计 $900，重置后用户已用为 $0；下次设备累计 $920，新周期已用为 $20。再次换设备仍累计这个新周期的消费。

设备采集失败或存在待核对的授权/撤销操作时，本次重置不生效，处理后可重试。同一条命令的重复投递不会重复清零，但主动再发一条新的 `/reset device` 会再次重置。已因超额撤销的用户不会自动获得设备；采集循环运行时，符合条件者会在后续轮次收到可以重新 `/auth` 的通知。
