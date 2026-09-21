# qpool

[English](README.md) | 中文

**多编码 Agent 的配额池化 + 成本感知路由控制平面——先烧你已经付过钱的额度，再碰按量计费的 API。**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python ≥ 3.9](https://img.shields.io/badge/python-%3E%3D3.9-blue.svg)](pyproject.toml)
[![dependencies: 0](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](pyproject.toml)

每个编码 Agent 都在默不作声地先烧你最贵的额度。qpool 把这件事反过来：把你散落在各家 CLI/IDE 的订阅额度、额度包、按量账户聚合进一个台账，然后把每个任务路由给**干得了这个活、且最便宜**的那个入口。

```bash
$ qpool quota simulate --amount 500000 --unit tokens --tier 3

route plan for 500,000 tokens, capability >= T3:

  PRIMARY   #2 claude-code/main credit_pack  T5  1,000,000 left   est $0
  FALLBACK  #3 kimi-cli/main payg            T3  metered          est $1

the three bills:
  routed cost   : $0      # qpool 路由后的成本
  payg-only     : $1      # 默认路径的成本
  saved by pool : $1      # ← 整个项目的意义就在这一行
```

省 token 的本质不是少用，而是**把本来要走按量付费的任务，挪到已经付过钱的订阅额度上**。

## 为什么做

- Cursor/Kiro/Codex 的订阅是**沉没成本**——月底用不完的额度，钱照样烧掉。
- 额度包是**折旧资产**——过不过期它都在贬值，到期直接归零。
- 按量 API 是**真实边际支出**——而默认路径偏偏先烧它，因为没有任何一个 dashboard 能看到全局。

没有人按**边际成本**路由。市面上的路由按限流、按任务类型、按 round-robin 分，就是没人按"哪个入口此刻对我来说不要钱"分。

## 30 秒看懂架构

qpool 是**控制平面**，刻意不重造透传网关——那是
[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) 已经做到极致的事，qpool 通过它的 Management API 驱动它。

```
采集层：11 家真实配额 API（codex, claude, cursor, gemini, copilot, kimi,
        cline, grok/xAI, 豆包/ark, windsurf, devin）+ manual 通道 + 插件目录
      │  qpool quota sync --apply
      ▼
┌─ 配额台账（SQLite）─────────────────────────────────┐
│  条目：类型 / 剩余 / 过期 / 单价 / 能力档位            │
│  事件：只追加的用量流水（事件溯源）                    │
└──────────────────────────────────────────────────────┘
      │  route_sort_key：能力硬约束 → 边际成本全序
      ▼
控制面 conductor：对账 → 启停 + priority + fill-first
      │  Management API（CLIProxyAPI :8317）
      ▼
CLIProxyAPI —— 协议转换 + 多账号池（不重造）
      │  ANTHROPIC_BASE_URL / OPENAI_BASE_URL
      ▼
Claude Code · Codex CLI · 任意兼容 harness
      │  每请求 token 经 usage-queue 回流
      ▼
任务级记账 → 月度「池化净省」报表
```

## 快速上手（3 分钟）

Python ≥ 3.9，**零第三方依赖**。

```bash
pip install -e . && qpool quota list

# 手工录入……
qpool quota add --agent codex --account work --kind subscription \
    --total 500 --unit requests --reset monthly --capability 4

# ……或者用本机 CLI 已登录的凭据直接调厂商配额 API
qpool quota sync --apply

# 提交任务前先算三笔账
qpool quota simulate --amount 500000 --unit tokens --tier 3

# 接入真实网关（可选但推荐）
export QPOOL_CPA_KEY=<CLIProxyAPI 的 remote-management.secret-key>
qpool quota cpa reconcile --apply   # 把成本顺序下发到账号池
qpool quota daemon                  # 每 60s：sync → reconcile → pull-usage

# 月底
qpool quota report                  # 真实支出 vs 池化净省
```

## 设计取舍

**这部分最值得一读。** 每条都是：候选方案 → 我的选择 → 为什么。

### 1. 控制面，而不是再造一个网关

**选项**：(a) 自研 OAuth 透传、协议转换、多账号轮询的完整网关；(b) 把网关当作已解决的问题，只做决策层。

**选 (b)。** 协议透传是 CLIProxyAPI 已经推到生产级成熟的commodity——OAuth 流程、四种线协议、冷却、多账号轮换，重写要几个月且永远追不上。qpool 不可替代的价值在**成本感知决策**，而 CLIProxyAPI 的 Management API 经验证是读写双全的（`auth-files` 启停、`priority`、`routing/strategy`、`reset-quota`、`usage-queue`），这让分工真正成立而不是口号。我用的判断原则：**把拥有差异化的那一层握在手里，其余全部集成。**

### 2. 边际成本全序，而不是加权路由

**选项**：(a) round-robin；(b) 按价格加权随机；(c) 按边际成本做严格全序。

**选 (c)。** 三类额度在经济学上是**不同的物种**，不是同一物种的不同权重：订阅是沉没成本（边际成本 0，按月再生）、额度包是折旧资产（价值随到期日坍缩到 0）、按量是真实边际支出。这是**排序问题，不是概率问题**——所以 qpool 计算全序（剩余多的订阅 → 最快过期的额度包 → 单价最低的按量），并以 `fill-first` + priority 下发到网关，而不是把流量按比例撒在池子上。

### 3. 一张统一台账表，而不是 per-provider schema

参考项目 onWatch 给**每家 provider 建三张表——16 家 40+ 张表**，接新厂商就要改 schema。qpool 只用一张 `quota_entries` 表（`agent` 是列不是表名），一个 `window_key` 列容纳一家多窗口（Codex 的 5h/7d/credits 共存为多行）。接新厂商是加一行，不是加一套表。**批判性地研究现有项目**——吸收它的重置周期检测思路、拒绝它的数据模型——比照抄有用得多。

### 4. 事件溯源，以及两条刻意不同严格度的写入路径

用量是**只追加的事件流**，从不是就地更新：可审计、可回放、可对账。在此之上，两条写入路径的严格度刻意不同：手工 `consume` 是严格模式（不允许透支、不允许消耗过期额度——这是**约束**），而网关回填是 `force` 模式（消费已在上游发生——这是**事实**）。把事实记到零以下会得到负的 `remaining`——**超用是真实的信号，不是需要压制错误**。

### 5. 对 Fair Use 订阅做诚实建模

订阅 API 只报 `used_percent`，不报绝对量——绝对配额根本不存在（这正是 Fair Use 的含义）。qpool 不编造数字：百分比型条目显示 `38% left`（`total = NULL`）；用户手工估了 total，sync 就按百分比**校准** `remaining = total × (1 − used%)`。**承认自己不知道什么的模型，胜过看起来精确的虚构。**

### 6. 零依赖，哪怕更费劲

只用标准库（`sqlite3`、`argparse`、`urllib`、`hmac`）——火山引擎签名 V4 手写，不引厂商 SDK。安装以秒计，Python ≥ 3.9 到处能跑，供应链暴露面只有标准库。

### 7. 开源合规是功能，不是事后补救

私有采集器（公司内部 agent）放 `~/.qpool/collectors/`——**仓库之外**的插件目录，内部逻辑永远碰不到公开代码树。GPL 参考项目仅作外部参考（思路与可公开观察的 API 事实，零代码拷贝），完整声明见 [NOTICE](NOTICE)。

## 工程细节

- **惰性重置取代后台轮询**——CLI 工具不该依赖常驻进程才能保持正确；重置检测在读写时惰性发生（恰好在需要时），跨周期用周期运算追平。
- **transcript 解析按 message.id 去重**——编码 Agent 的会话文件会把同一消息重写多遍（流式、重试），聚合时按 message.id 去重并保留最终态（与 ccusage 同规则），这把一个真实月度估算从虚高的 $715 修正到正确的 ~$350。
- **消耗型与余额型两种遥测**——计量类 API（方舟、Devin）报的是消耗量而非余额；台账接受 `consumed_abs` 并推导 `remaining = total − consumed`。

## 测试

CI 里没有真实厂商账号，所以全部集成用本地 mock server 重放文档线协议验证：约 30 项断言覆盖凭据探测、签名 V4 请求构造、响应解析（含字符串数值与嵌套负载）、台账 upsert 幂等、滚动窗口重置、对账决策、priority 编排与用量回填语义。

## 采集器覆盖

| Agent | 凭据 | 配额 API |
| --- | --- | --- |
| Codex | `~/.codex/auth.json` | `chatgpt.com/backend-api/wham/usage`（5h+7d 窗口） |
| Claude Code | macOS 钥匙串 / `~/.claude/.credentials.json` | `api.anthropic.com/api/oauth/usage` |
| Cursor | `state.vscdb`（只读） | `api2.cursor.sh` Connect-RPC |
| Gemini CLI | `~/.gemini/oauth_creds.json` | `cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota` |
| Copilot | `COPILOT_TOKEN` / IDE hosts.json | `api.github.com/copilot_internal/user` |
| Kimi CLI | `~/.kimi-code/credentials/` | `api.kimi.com/coding/v1/usages` |
| Cline | `CLINE_API_KEY` | `api.cline.bot`（官方 REST） |
| Grok / xAI | `XAI_MANAGEMENT_KEY` | `management-api.x.ai` billing |
| 豆包 / 方舟 | 火山 AK/SK | `GetInferenceUsage`（签名 V4） |
| Windsurf / Devin | service key | `server.codeium.com` / `api.devin.ai/v3`（企业版） |
| 其余全部 | `~/.qpool/static_quotas.json` | manual 通道 |
| 内部 agent | `~/.qpool/collectors/*.py` | 仓库外插件 |

## 致谢

原创实现，未拷贝任何第三方代码。思路与可公开观察的 API 事实受益于 [onWatch](https://github.com/onllm-dev/onWatch)（**GPL-3.0**，仅外部参考）、[claude-code-router](https://github.com/musistudio/claude-code-router)、[oh-my-codex](https://github.com/Yeachan-Heo/oh-my-codex)、[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)（**MIT**，外部进程）。详见 [NOTICE](NOTICE)。

## 许可证

[MIT](LICENSE)
