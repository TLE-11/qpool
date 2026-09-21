# qpool

[English](README.md) | 中文

**多编码 Agent 的「配额池化 + 成本感知路由」控制平面。**

你各家编码 Agent 的订阅费早就付了。qpool 把散落各处的订阅额度、额度包、按量账户聚合成一个配额池，在**能力满足任务**的前提下按**边际成本从低到高**自动路由——**已付费的订阅额度（边际成本≈0）优先消耗、快过期的额度包在过期前用完、按量付费只在必要时才动用。**

省 token 的本质不是少用，而是**把本来要走按量付费的任务，挪到已经付过钱的订阅额度上**。

```
订阅额度（已付费，≈$0） →  额度包（临期优先消耗）  →  按量付费（真花钱）
      优先消耗                 过期前用完                 最后兜底
```

## 为什么做

| 痛点 | 现状 |
| --- | --- |
| 订阅额度不用白不用 | Cursor/Kiro/Codex 月费已付，额度散落各处无人统一调度 |
| 配额信息割裂 | 每个 agent 一个 dashboard，余额/模型/进度要登录 N 个地方看 |
| 按量 API 真花钱 | 默认路径容易被高频任务烧掉，却没有成本意识 |
| 额度包会过期 | 赠送/购买的额度包过期作废，无人提醒、无人自动消耗 |
| 能力与价格脱节 | 便宜的不一定够用，贵的不一定更强，路由必须在「干得了」的子集里选最便宜的 |

## 架构

qpool 是**控制平面**，刻意不重造透传网关——那是
[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) 的活，qpool 通过它的
Management API 驱动它。

```
┌─ 采集层（11 家真实 API + manual 通道 + 插件目录）
│     codex · claude-code · cursor · gemini · copilot · kimi
│     cline · grok(xAI mgmt) · 豆包(ark) · windsurf · devin
│         │  qpool quota sync --apply
│         ▼
│   ┌───────────────────────────────────────────────┐
│   │ 配额台账（SQLite）                              │
│   │  条目：类型/总量/剩余/过期/单价/能力档位          │
│   │  事件：只追加的用量流水（事件溯源）               │
│   └───────────────────────────────────────────────┘
│         │ route_sort_key（边际成本排序）
│         ▼
│   控制面 conductor：对账 → 启停 + priority + fill-first
│         │  Management API（localhost:8317/v0/management）
└─────────┤
          ▼
   CLIProxyAPI —— 协议转换 + 多账号池（不重造）
          │  ANTHROPIC_BASE_URL / OPENAI_BASE_URL
          ▼
   Claude Code · Codex CLI · 任意兼容 harness
          │  每请求 token 经 usage-queue 回流
          ▼
   任务级记账 → 月度节省报表（闭环）
```

## 功能

- **配额台账**——所有 agent × 账号统一一张表：类型（订阅/额度包/按量）、剩余量、过期时间、单位成本、能力档位。账期惰性滚动重置，用量事件只追加（事件溯源）。
- **11 家真实采集器**——用本机 CLI 已登录的凭据（OAuth 文件、系统钥匙串、应用状态库）直接调厂商配额 API，无需重新登录。
- **成本感知路由**——先能力硬约束过滤，再按边际成本排序：剩余多的订阅优先 → 最快过期的额度包优先 → 最便宜的按量 API 兜底。
- **路由模拟**——提交任务前先算**三笔账**：路由成本 vs 纯按量成本 vs 池化节省，附 failover 链路和每条候选被跳过的原因。
- **CLIProxyAPI 控制面**——对账循环把决策下发到网关：禁用耗尽的凭据、恢复已重置的凭据、写入 priority 字段、设置 `fill-first`，让整个池子按"最便宜优先"的顺序消耗。
- **任务级记账**——从网关的 per-request 用量队列回填台账；月报展示真实支出、池化用量的按量等价价值、以及**池化净省**。
- **Daemon**——`sync → reconcile → pull-usage` 定时循环，单实例锁防双开；`--once` 适配 cron/launchd。
- **插件采集器**——公司内部/私有 agent 放 `~/.qpool/collectors/`，不进本仓库（见下文）。

## 安装

要求 Python ≥ 3.9，零第三方依赖。

```bash
git clone <你的 fork 地址> qpool && cd qpool
python3.11 -m venv ~/.local/share/qpool/venv
~/.local/share/qpool/venv/bin/pip install -e .
mkdir -p ~/.local/bin
ln -sf ~/.local/share/qpool/venv/bin/qpool ~/.local/bin/qpool   # 确保在 PATH 里

qpool quota list
```

## 快速开始

**1. 手工录入额度**

```bash
qpool quota add --agent codex --account work --kind subscription \
    --total 500 --unit requests --reset monthly --capability 4
qpool quota add --agent claude-code --account main --kind credit_pack \
    --total 1000000 --unit tokens --expires 2026-10-01 --capability 5
qpool quota add --agent kimi-cli --account main --kind payg \
    --cost-per-unit 0.000002 --unit tokens
```

**2. 或者直接从厂商 API 同步真实读数**（自动探测本机 CLI 凭据；不加 `--apply` 时只是演练）：

```bash
qpool quota sync            # 看看各家采集器都能读到什么
qpool quota sync --apply    # 读数写入台账
```

**3. 提交任务前先模拟**——三笔账：

```bash
$ qpool quota simulate --amount 500000 --unit tokens --tier 3

route plan for 500,000 tokens, capability >= T3:

  PRIMARY   #2 claude-code/main credit_pack  T5  1,000,000 left   est $0
  FALLBACK  #3 kimi-cli/main payg            T3  metered          est $1

the three bills:
  routed cost   : $0
  payg-only     : $1
  saved by pool : $1
```

**4. 接入 CLIProxyAPI（透传数据面）**

```bash
export QPOOL_CPA_KEY=<CLIProxyAPI 配置里的 remote-management.secret-key>

qpool quota cpa status              # 凭据池 × 台账对账视图（含建议动作）
qpool quota cpa reconcile --apply   # 下发 启停 + priority + fill-first
qpool quota cpa pull-usage          # 每请求 token 回填台账
```

标准 OpenAI 兼容 provider（如豆包/方舟）完全不需要 OAuth 透传——直接注册为网关上游：

```bash
export ARK_API_KEY=...
qpool quota cpa register-provider --name doubao --preset ark \
    --model doubao-seed-2-1-pro-260628:doubao-pro
# Agent Plan / Coding Plan 用户：加 --plan（自动切到 /api/plan/v3）
# 用 qpool quota cpa providers 验证
```

**5. 跑控制循环**

```bash
qpool quota daemon                # 每 60s：sync → reconcile → pull-usage
qpool quota daemon --once         # 跑一轮退出，适合 cron/launchd
```

**6. 看这个月池化省了多少**

```bash
qpool quota report
#   real money spent (payg)       : $0.24
#   pooled usage, payg-equivalent : $1
#   saved by pooling              : $1
```

## 采集器覆盖

| Agent | 凭据来源 | 配额 API | 备注 |
| --- | --- | --- | --- |
| Codex | `~/.codex/auth.json` | `chatgpt.com/backend-api/wham/usage` | 5h + 7d 双窗口 + credits |
| Claude Code | macOS 钥匙串 → `~/.claude/.credentials.json` | `api.anthropic.com/api/oauth/usage` | OAuth beta 头 |
| Cursor | `state.vscdb` ItemTable（只读） | `api2.cursor.sh` Connect-RPC | 顺带读 email/套餐 |
| Gemini CLI | `~/.gemini/oauth_creds.json` | `cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota` | pro/flash 族聚合 |
| Copilot | `COPILOT_TOKEN` / IDE hosts.json | `api.github.com/copilot_internal/user` | 免费版新格式归一化 |
| Kimi CLI | `~/.kimi-code/credentials/` | `api.kimi.com/coding/v1/usages` | 兼容字符串数值 |
| Cline | `CLINE_API_KEY` | `api.cline.bot/api/v1/users/{id}/balance` | 官方 REST |
| Grok / xAI | `XAI_MANAGEMENT_KEY` + `XAI_TEAM_ID` | `management-api.x.ai/.../invoice/preview` | 替代 gRPC-web 逆向 |
| 豆包 / 方舟 | `VOLC_ACCESS_KEY_ID` + `VOLC_SECRET_ACCESS_KEY` | `GetInferenceUsage`（签名 V4） | 月度消耗 |
| Windsurf | `WINDSURF_SERVICE_KEY` | `server.codeium.com/api/v1/GetTeamCreditBalance` | 企业版 |
| Devin | `DEVIN_API_KEY` | `api.devin.ai/v3/.../consumption/daily` | 企业版，ACU 单位 |
| 其余全部 | `~/.qpool/static_quotas.json` | — | manual 通道（kiro、antigravity、qoder、opencode……） |
| 内部 agent | `~/.qpool/collectors/*.py` | 你的 API | 插件目录，不进仓库 |

## 命令参考

```
qpool quota add        录入额度条目（订阅/额度包/按量）
qpool quota list       台账视图（默认按边际成本路由顺序）
qpool quota consume    手工记账（严格校验：不允许透支）
qpool quota remove     删除条目及其用量事件
qpool quota events     只追加的用量历史
qpool quota simulate   路由演练：三笔账 + failover 链
qpool quota report     月度用量 + 节省报表
qpool quota sync       用本机凭据轮询厂商 API（--apply 写入台账）
qpool quota cpa        CLIProxyAPI 控制面：status/reconcile/pull-usage
qpool quota daemon     控制循环：sync -> reconcile -> pull-usage
```

## 配置

| 环境变量 | 用途 |
| --- | --- |
| `QPOOL_DB` | 台账路径（默认 `~/.qpool/qpool.db`） |
| `QPOOL_CPA_URL` | CLIProxyAPI 地址（默认 `http://localhost:8317`） |
| `QPOOL_CPA_KEY` | CLIProxyAPI 管理密钥（`remote-management.secret-key`） |
| `QPOOL_STATIC_QUOTAS` | manual 通道 JSON 路径 |
| `QPOOL_COLLECTORS_DIR` | 插件采集器目录（默认 `~/.qpool/collectors`） |
| `CODEX_HOME`、`KIMI_CODE_HOME`、`COPILOT_TOKEN`、`GEMINI_TOKEN`、`CLINE_API_KEY`、`XAI_MANAGEMENT_KEY`、`XAI_TEAM_ID`、`VOLC_ACCESS_KEY_ID`、`VOLC_SECRET_ACCESS_KEY`、`ARK_API_KEY_ID`、`WINDSURF_SERVICE_KEY`、`DEVIN_API_KEY` | 各采集器凭据 |

## 插件采集器

公司内部或私有 agent 不属于本仓库。在 `~/.qpool/collectors/` 放一个 Python 文件，暴露三个符号即可加入注册表：

```python
# ~/.qpool/collectors/acme-agent.py
from qp.collectors.base import QuotaReading

AGENT = "acme-agent"

def detect_credentials():
    return {"access_token": "...", "source": "internal"}

def fetch_readings(creds):
    return [QuotaReading(agent="acme-agent", window_key="daily",
                         label="acme-agent (internal)", used_percent=33.0)]
```

然后 `qpool quota sync --agent acme-agent --apply` 就会走与内置采集器完全相同的管道。

## 路由原理

1. **能力硬约束**——条目的 `capability_tier` 必须 ≥ 任务需求（`--tier`），且额度单位必须匹配（tokens 任务不能花 requests 额度）。
2. **边际成本排序**——订阅优先（已付费，剩余多者先）、额度包次之（最快过期者先，过期前榨干）、按量最后（单价低者先）。过期/耗尽的条目永远垫底。
3. **网关编排**——这个顺序会被翻译成 CLIProxyAPI 的 `priority` 字段 + `fill-first` 策略，池子严格按"最便宜优先"消耗；耗尽的凭据被禁用，恢复的凭据被启用并 `reset-quota`。

## ToS 风险说明

轮询自己的配额（qpool 采集器做的事）是**只读**行为。**通过透传网关消费订阅额度是另一回事**——Google 明确禁止把 AI Pro 订阅用于 API 调用；其他厂商属于未执法的灰色地带。这个风险在透传层（CLIProxyAPI 及其同类），不在本控制面。请了解各厂商条款；qpool 只负责把账算清楚、把路由摆明白。

## 致谢

qpool 是原创实现——本仓库不拷贝任何第三方代码。设计思路与可公开观察的功能事实（API 端点、凭据位置）受益于：

- [onWatch](https://github.com/onllm-dev/onWatch)（**GPL-3.0**）——配额轮询与重置周期检测思路。仅作**外部参考**；未包含、未拷贝、未链接，构建运行 qpool 均不需要它。
- [claude-code-router](https://github.com/musistudio/claude-code-router)——凭据池状态与冷却模型思路。
- [oh-my-codex](https://github.com/Yeachan-Heo/oh-my-codex)——任务分发状态机与事件溯源思路。
- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)（**MIT**）——qpool 驱动的透传数据面，以**外部进程**方式经 Management API 对接。不捆绑，请独立安装运行。

完整的第三方致谢见 [NOTICE](NOTICE)。

## 许可证

[MIT](LICENSE)——第三方署名见 [NOTICE](NOTICE)。
