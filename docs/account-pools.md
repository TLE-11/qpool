# 账号、额度池、窗口与执行渠道（实验分支）

当前分支实现账号隔离、共同窗口限制和显式执行绑定。后续新增的任务启动前选模与本地前端见[控制台说明](local-console.md)；会话内模型切换、额度预测和并发预占仍不支持。

## 配置与试用

配置默认读取 `~/.qpool/pools.json`，也可用 `QPOOL_CONFIG` 指向其他文件。参考 [双账号示例](../examples/pools.example.json)。配置中只引用已登录的凭据文件及其字段，不放 token。`fields` 是“采集器参数名 → 凭据 JSON 字段路径”；字段必须与对应采集器的 `fetch_readings` 参数匹配。

示例中的账号、路径、模型都需要替换。CLI 命令参数以本机 `codex exec --help` 为准；本次仅检查了本机帮助所列的 `--ignore-user-config`、`--model` 和 `--sandbox`，未调用真实模型。先使用只读 sandbox 验证账号、模型和计费来源，再自行决定任务执行权限。

```bash
export QPOOL_CONFIG=/absolute/path/to/pools.json
export QPOOL_DB=/absolute/path/to/trial-qpool.db

qpool quota sync                         # 只读查看各显式账号的 API 读数
qpool quota sync --apply                 # 每个账号的一批窗口原子写入
qpool quota sync --account-id codex-work --apply
qpool quota pools                        # 查看池状态及渠道未就绪原因
qpool quota simulate --amount 50000 --unit tokens --tier 3
qpool quota run "说明当前目录的项目结构" --tier 3 --dry-run
qpool quota run "说明当前目录的项目结构" --tier 3 --max-cost 0
qpool quota events
qpool quota report
```

最后一个 `run` 会真正启动配置的 CLI，前提是候选通过检查。`--max-cost` 是按任务量计算的预计新增费用过滤，不是厂商账单的硬限额。没有额度预占，多个任务同时执行可能争用剩余配额。

## 账号与窗口语义

- `account.id` 是稳定的本机标识；同一厂商的同一 `expected_subject` 不得重复声明为多个账号。修改显示名、刷新同账号 token 不会新建账号；更换实际账号必须使用新标识。不存在 email 子串匹配。
- 采集前后检查 `subject_field` 与 `expected_subject`。来源缺失或身份变化会拒绝同步；一个账号失败不阻止其他账号完成同步。
- 每个池的 `windows` 明确列出窗口。`scope: "*"` 适用所有模型，其他 scope 是精确的模型名；`role: "limit"` 参与硬限制，`role: "stat"` 只用于展示。
- 同一个池内，适用本次模型的硬限制必须全部满足。其他模型的专属窗口不阻断本次模型。未知窗口名不能自动推断范围或角色，需要在配置中明确归类。
- 缺少必需窗口会使这一账号的本批池状态变为未知，不发布半批新余额。重复或乱序响应不会覆盖较新的完整或不完整批次。
- API 快照默认 120 秒有效；可按池设置 `max_age_seconds`。到达 API 提供的 `resets_at` 后等待新观测，不凭本地时钟恢复满额。手工录入的固定周期额度仍保留本地滚动。
- 百分比达到 100% 即耗尽，即使没有绝对总量；只剩百分比的窗口可以证明尚未耗尽，但会显示 `task capacity unknown`，不保证完成预计任务量。
- credits、requests、tokens、美元和 ACU 不隐式互换。只有同单位的绝对余额才参与任务量覆盖判断；纯消费统计不等于余额。

订阅和 credits 可以在同一账号下作为两个池分别展示。但通用 CLI 不能证明它会从哪一个资金池扣费，因此本版会阻止多池账号的 CLI 自动执行。单池账号的本机配置也只证明所选 profile 与模型参数已绑定；是否存在未声明的额外计费来源，必须用真实客户端和账号设置验收，不能靠文件检查认证。

## 执行渠道

首版支持通过独立 profile 环境变量选择账号的 CLI：

- `source.profile_env` / `profile_dir` 指定账号目录，凭据文件必须位于该目录内。
- `channel.account_id`、`pool_id` 和显式 `model` 必须对应；命令必须包含 `{task}` 和 `{model}`。
- 命令可发现、凭据身份与 profile 环境通过检查后，才作为执行候选。执行前再检查配置和额度；变化时明确报错，沿已经展示的候选链只尝试仍通过检查的候选。
- Codex/Claude 的已知 API Key 与 Base URL 环境覆盖会从子进程环境移除，避免绕过所选 OAuth profile；profile 内部的额外配置仍需适配器和真实客户端验证。示例使用本机支持的 `--ignore-user-config` 减少此类覆盖。
- CLI 一旦启动，非零退出或超时会停止自动切换，要求先检查工作区。退出码为零只证明进程成功，不代表代码质量已经通过验收。
- `simulate` 和 `run --dry-run` 共用同一规划方法，不启动 CLI，也不调用计费推理端点。它们可能检查本机凭据、运行固定周期手工额度的本地重置。

旧 `executors.json` 的 agent → command 格式仍可读取，但不再被当作账号已绑定的证据。网关渠道保留明确的不可用原因：没有验证账号和资金池固定能力，不能从“有 provider/model”推导“可以精确使用目标账号”。

## CLIProxyAPI 控制与用量

本分支不新增网关协议。全局 `priority` / routing strategy 写入暂停，CLI 兼容保留相关参数；需要先验证真实 CPA 版本的排序含义与作用范围。不会清空 CPA 的运行时冷却状态来绕过限制。

若已有管理与用量对接，可在 account 上增加可选的 `cpa` 声明：

```json
{
  "auth_name": "exact-auth-file-name.json",
  "provider": "codex",
  "subject_field": "account_id",
  "usage_source": "EXACT_SOURCE_FROM_VERIFIED_USAGE_CONTRACT",
  "exclusive": true
}
```

`exclusive: true` 表示显式声明该凭据所有资金来源都已包含在本账号配置内。`subject_field` 是真实 `auth-files` 返回里的身份字段路径，必须等于账号 `expected_subject`；这里的字段名是示意，不能猜测真实 CPA 返回。任何条件无法验证时不修改凭据状态。

控制面只有在全部资金池被通用限制阻断时才暂停整个凭据；模型专属限制不会导致全账号禁用。恢复需要新的可用证据，并有同一身份下 qpool 成功暂停的本地记录；外部已禁用且没有该记录的凭据不会自动恢复。远端动作与本地记录没有分布式事务：暂停成功后若进程在保存记录前中断，按保守策略需要人工核对，不自动恢复。

`qpool quota cpa pull-usage` 对已取得的记录进行本地持久化：

- 按 CPA 实例和请求 ID 去重；只有 provider、精确 source 与唯一资金池能够对应时才入账。自定义 provider 名称回退仍被保留。
- source 在本地仅存摘要；不持久化 token、完整请求响应和提示词。
- 资金池有歧义、缺少请求 ID 或用量的记录保留为 pending，可用 `qpool quota pending` 查看。当前只提供检查，未提供自动重归属操作。
- 一次请求只计一次，不复制到 5 小时和周窗口，也不从厂商窗口里扣 token。余额以之后的采集快照为准；CLI 本次没有新增转录日志记账。
- 费用是已知价格下的边际估算；未知价格和待核对记录单独显示，报表不是完整账单对账证明。
- CPA 队列是破坏性出队：取得记录与本地提交之间仍可能丢失。当前只保证已经持久化且有唯一 ID 的记录幂等，不承诺端到端零丢失。

## 旧数据迁移与回退

打开旧库前先停止旧 daemon。本版首次迁移会生成 `<数据库名>.pre-pools-v1.bak`，保留旧条目 ID、余额及历史事件。窗口增加 pool、scope、role 和观测来源；历史不自动合并，不通过 `auto` 标签猜账号。

旧条目继续通过 `quota list/events/report` 查看，未绑定的条目不会自动路由。配置好账号池后，可以显式关联手工额度：

```bash
qpool quota bind 12 --pool YOUR_POOL_ID --window manual --scope '*'
```

绑定时要求 agent、额度种类与手工额度单位匹配，目标窗口必须先在配置中定义。已有窗口冲突会报错，不合并条目。绑定旧 API 快照后仍需重新同步，反复绑定不能把未验证快照变成手工余额。

新旧版本禁止同时写同一数据库。旧版程序本身不认识新 schema，无法从新版一侧强制禁止旧程序写入。回退时停止新版、保留当前数据库与配置，再将迁移前备份复制到另一个路径并用 `QPOOL_DB` 指向它；备份之后新增的记录需另行保留，不能假设会自动回放。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q qp tests
git diff --check
```

测试使用临时数据库、合成凭据、真正启动的本机 Python CLI 替身和 CPA 替身，覆盖账号隔离、窗口 AND/范围/过期、不完整与乱序批次、模拟/执行一致、启动前复查、手工额度、重复及并发回填、旧库备份与历史保留、人工禁用保护。没有读取真实账号 token、调用厂商配额 API、执行付费推理或操作真实网关。

真实 Codex/Claude profile、实际计费来源、CPA 身份字段及全局排序契约仍需单独联调；替身通过不代表这些原生路径已通过。
