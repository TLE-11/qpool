<p align="right"><a href="README.md">English</a></p>

<h1 align="center">qpool</h1>

<p align="center">
  <strong>把多个 Codex 订阅用在一起。</strong><br>
  在本地统一路由账号与模型：常规工作用常规模型，复杂任务用主力模型。
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="docs/local-console.md">使用文档</a> ·
  <a href="#路由机制">路由机制</a> ·
  <a href="LICENSE">许可证</a>
</p>

继续使用你熟悉的 Codex 桌面端或 CLI。qpool 接入已有的 Plus、Pro 和 OpenAI API
账号，判断每个新用户轮次的任务难度，从常规组或主力组中选择模型。账号、模型目录和
额度窗口在本地控制台统一管理，每次请求实际用了哪个账号、哪个模型都可以查看。

新轮次可以重新选模型，同一轮里的工具续接和上下文压缩沿用本轮选择。同一个会话保持
账号不变；只有在路由方案中明确允许，才会使用付费 API。

## 为什么使用 qpool

| | |
| --- | --- |
| **按任务难度选择模型** | 常规轮次交给常规组，复杂或不确定的工作交给主力组。 |
| **按顺序使用多个账号** | 可以配置 Plus → Pro → OpenAI API，执行前检查额度和模型能力。 |
| **保留原来的 Codex 工作流** | 继续使用桌面端或 CLI，通过 `qpool auto` 接入路由。 |
| **查看实际执行情况** | 查看路由判断、所选账号和模型、执行状态及 Token 用量。 |

qpool 支持模型难度判断、本地规则、固定主力组，以及兼容的外部 HiRoute 决策服务。
账号接入、路由配置和 Agent 接入都可以在本地控制台完成。

## 快速开始

### 安装并启动

需要 Python 3.9+ 和已安装的 Codex 客户端。订阅账号还需要 CLIProxyAPI；OpenAI
API 账号直接连接 OpenAI。

```bash
git clone https://github.com/TLE-11/qpool.git
cd qpool
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m qp web --port 8765
```

打开 <http://127.0.0.1:8765>。

### 配置第一条路由

1. 在 **账号与模型** 中接入已有 Codex 登录、登录另一个订阅账号，或添加已有的
   OpenAI API Key，然后同步模型目录和额度窗口。
2. 在 **路由方案** 中选择模型判断，配置常规组、主力组、决策模型和账号顺序。
   需要使用 API 时，再明确开启 API 计费。
3. 在 **Agent 接入** 中启用方案，接入 Codex 桌面端或使用生成的 CLI 入口。
   桌面接入会先备份配置，重启应用后加载入口。
4. 新建 Codex 会话，选择 `qpool auto`，照常工作。
5. 在 **会话与用量** 中查看路由判断和执行记录。

账号接入、已有聊天接入和恢复原 Codex 配置的操作，见[本地控制台说明](docs/local-console.md)。

## 查看路由结果

每次请求记录实际使用的账号和模型、执行状态及 Token 用量。决策调用有独立记录，
可以查看所选模型组、概率、门槛、耗时和用量，并关联到对应的执行请求。

决策模型用量与执行模型用量分开统计。控制台不推算费用，也不把请求成功当作路由
准确或成本节省的证明。

## 路由机制

每个新用户轮次，qpool 将当前任务和可见的文字上下文发送给配置好的决策模型。
使用模型判断时，任务简单的概率达到设定门槛就选择常规组，否则选择主力组。
判断无效、超时或不可用时回退主力组，仍需通过账号和额度检查。

选定模型组后，qpool 按配置顺序检查候选。例如，新会话可以优先使用 Plus，再使用
Pro，最后才考虑明确启用的 OpenAI API。订阅额度未知不等于耗尽，不会因此触发
付费 API。

选定的账号在会话内保持不变。新的用户轮次可以在同一账号内重新选模型，工具续接和
上下文压缩沿用本轮绑定。如果当前账号无法继续执行该路由，需要新建会话。
请求失败或流式响应中断时，不会换另一个账号重放。

qpool 复用 HiRoute `smart_saving` 路径中的任务难度策略，不包含 HiRoute 完整的
路由与评价体系，例如基于胜任度的降级保护和阶段表现评分。源码来源与复用范围见
[SOURCE.md](qp/_vendor/hiroute/SOURCE.md)。

## 本地数据与配置

控制台和网关只监听本机回环地址。订阅登录使用独立目录；API Key 保存在受保护的
本地文件中，不写入路由配置。桌面接入需要明确操作，并提供配置备份和恢复入口。

默认配置为 `~/.qpool/pools.json`，额度台账为 `~/.qpool/qpool.db`，可通过
`QPOOL_CONFIG` 和 `QPOOL_DB` 指定其他路径。使用订阅适配器前，请确认对应服务条款。

## 文档

- [本地控制台说明](docs/local-console.md)：账号、模型同步、路由方案、桌面接入与恢复。
- [账号、额度池与渠道说明](docs/account-pools.md)：额度语义与已有的 `qpool quota` CLI。
- [浏览器与桌面回归](docs/browser-desktop-regression.md)：验证记录和未覆盖范围。
- [双账号示例](examples/pools.example.json)：使用前替换占位内容。

## 许可证

qpool 使用 [MIT License](LICENSE)。内置 HiRoute 策略源码使用 Apache-2.0，见
[SOURCE.md](qp/_vendor/hiroute/SOURCE.md) 和 [NOTICE](NOTICE)。
