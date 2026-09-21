# qpool

**English** | [中文](README_CN.md)

**Quota pooling + cost-aware routing for AI coding agents.**

Your coding-agent subscriptions are already paid for. qpool pools the scattered
quota from every agent CLI/IDE you own into one ledger, then routes each task to
the cheapest entry that can actually do it — **already-paid subscription quota
(marginal cost ≈ 0) first, soon-expiring credit packs next, pay-as-you-go only
when nothing else fits.**

The way to save tokens is not to use fewer of them. It is to move work that
would have hit a metered API onto quota you have already paid for.

```
subscription (paid, ~$0)  →  credit packs (expiring soon)  →  pay-as-you-go ($$)
        consume first              before it expires              last resort
```

## Why

| Pain | Today |
| --- | --- |
| Subscription quota goes unused | Cursor/Kiro/Codex monthly fees are paid, but the quota sits idle in silos |
| Quota info is fragmented | N dashboards, N logins, N reset cycles to track |
| Metered APIs cost real money | The default path burns cash with no cost awareness |
| Credit packs expire | Free/purchased packs die quietly — nobody drains them first |
| Price ≠ capability | The cheapest entry that *can* do the task is the one that should |

## Architecture

qpool is a **control plane**. It deliberately does not re-implement the
passthrough gateway — that job belongs to
[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), which qpool drives
through its Management API.

```
┌─ collectors (11 real APIs + manual channel + plugin dir)
│     codex · claude-code · cursor · gemini · copilot · kimi
│     cline · grok(xAI mgmt) · doubao(ark) · windsurf · devin
│         │  qpool quota sync --apply
│         ▼
│   ┌───────────────────────────────────────────────┐
│   │ quota ledger (SQLite)                          │
│   │  entries: kind/total/remaining/TTL/cost/tier   │
│   │  events:  append-only usage log                │
│   └───────────────────────────────────────────────┘
│         │ route_sort_key (marginal-cost ordering)
│         ▼
│   conductor: reconcile → disable/enable + priority + fill-first
│         │  Management API (localhost:8317/v0/management)
└─────────┤
          ▼
   CLIProxyAPI — protocol bridge + multi-account pool (not reinvented here)
          │  ANTHROPIC_BASE_URL / OPENAI_BASE_URL
          ▼
   Claude Code · Codex CLI · any compatible harness
          │  per-request tokens flow back via usage-queue
          ▼
   task-level accounting → monthly savings report
```

## Features

- **Quota ledger** — one unified table for every agent × account: kind
  (subscription / credit pack / payg), remaining, TTL, unit cost, capability
  tier. Lazy reset-cycle rollover, append-only usage events (event sourcing).
- **11 real collectors** — poll the vendors' own quota APIs using local CLI
  credentials (OAuth files, OS keychains, app state DBs). No re-login.
- **Cost-aware routing** — capability hard-filter first, then marginal-cost
  order: fuller subscriptions → soonest-expiring packs → cheapest metered API.
- **Route simulation** — the *three bills* before you commit a task:
  routed cost vs pay-as-you-go cost vs saved-by-pooling, with a failover chain
  and per-entry skip reasons.
- **CLIProxyAPI control plane** — reconcile loop pushes decisions to the
  gateway: disable exhausted credentials, re-enable recovered ones, write
  priority fields, set `fill-first` so the pool drains cheapest-first.
- **Task-level accounting** — drains the gateway's per-request usage queue
  into the ledger; monthly report shows real money spent, payg-equivalent
  value of pooled usage, and **saved by pooling**.
- **Daemon** — `sync → reconcile → pull-usage` on an interval, with a
  single-instance lock. Also `--once` for cron/launchd.
- **Plugin collectors** — proprietary/internal agents live in
  `~/.qpool/collectors/`, outside this repo (see below).

## Install

Requires Python ≥ 3.9, zero third-party dependencies.

```bash
git clone <your-fork-url> qpool && cd qpool
python3.11 -m venv ~/.local/share/qpool/venv
~/.local/share/qpool/venv/bin/pip install -e .
mkdir -p ~/.local/bin
ln -sf ~/.local/share/qpool/venv/bin/qpool ~/.local/bin/qpool   # on your PATH

qpool quota list
```

## Quick start

**1. Register quota manually**

```bash
qpool quota add --agent codex --account work --kind subscription \
    --total 500 --unit requests --reset monthly --capability 4
qpool quota add --agent claude-code --account main --kind credit_pack \
    --total 1000000 --unit tokens --expires 2026-10-01 --capability 5
qpool quota add --agent kimi-cli --account main --kind payg \
    --cost-per-unit 0.000002 --unit tokens
```

**2. Or sync real readings from the vendors' own APIs** (auto-detects local
CLI credentials; dry-run until `--apply`):

```bash
qpool quota sync            # show what every collector can read
qpool quota sync --apply    # write readings into the ledger
```

**3. Simulate before you commit** — the three bills:

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

**4. Hook up CLIProxyAPI (the passthrough data plane)**

```bash
export QPOOL_CPA_KEY=<remote-management.secret-key from your CLIProxyAPI yaml>

qpool quota cpa status              # credential pool × ledger, with decisions
qpool quota cpa reconcile --apply   # push disable/enable + priority + fill-first
qpool quota cpa pull-usage          # book per-request tokens into the ledger
```

Standard OpenAI-compatible providers (e.g. Doubao/Ark) skip OAuth bridging
entirely — register them as upstreams straight into the gateway:

```bash
export ARK_API_KEY=...
qpool quota cpa register-provider --name doubao --preset ark \
    --model doubao-seed-2-1-pro-260628:doubao-pro
# Agent Plan / Coding Plan subscribers: add --plan (switches to /api/plan/v3)
# -> qpool quota cpa providers     to verify
```

**5. Run the loop**

```bash
qpool quota daemon                # every 60s: sync → reconcile → pull-usage
qpool quota daemon --once         # single round, cron/launchd friendly
```

**6. See what pooling saved you**

```bash
qpool quota report
#   real money spent (payg)       : $0.24
#   pooled usage, payg-equivalent : $1
#   saved by pooling              : $1
```

## Collector coverage

| Agent | Credentials source | Quota API | Notes |
| --- | --- | --- | --- |
| Codex | `~/.codex/auth.json` | `chatgpt.com/backend-api/wham/usage` | 5h + 7d windows, credits |
| Claude Code | macOS Keychain → `~/.claude/.credentials.json` | `api.anthropic.com/api/oauth/usage` | OAuth beta header |
| Cursor | `state.vscdb` ItemTable (read-only) | `api2.cursor.sh` Connect-RPC | email/membership too |
| Gemini CLI | `~/.gemini/oauth_creds.json` | `cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota` | pro/flash families |
| Copilot | `COPILOT_TOKEN` / IDE hosts.json | `api.github.com/copilot_internal/user` | free-plan normalize |
| Kimi CLI | `~/.kimi-code/credentials/` | `api.kimi.com/coding/v1/usages` | string numerics tolerated |
| Cline | `CLINE_API_KEY` | `api.cline.bot/api/v1/users/{id}/balance` | official REST |
| Grok / xAI | `XAI_MANAGEMENT_KEY` + `XAI_TEAM_ID` | `management-api.x.ai/.../invoice/preview` | replaces gRPC-web reverse engineering |
| Doubao / Ark | `VOLC_ACCESS_KEY_ID` + `VOLC_SECRET_ACCESS_KEY` | `GetInferenceUsage` (signature V4) | monthly consumption |
| Windsurf | `WINDSURF_SERVICE_KEY` | `server.codeium.com/api/v1/GetTeamCreditBalance` | Enterprise |
| Devin | `DEVIN_API_KEY` | `api.devin.ai/v3/.../consumption/daily` | Enterprise, ACU |
| everything else | `~/.qpool/static_quotas.json` | — | manual channel (kiro, antigravity, qoder, opencode, …) |
| internal agents | `~/.qpool/collectors/*.py` | your API | plugin dir, never committed |

## CLI reference

```
qpool quota add        register a quota entry (subscription/credit_pack/payg)
qpool quota list       ledger view (default: marginal-cost route order)
qpool quota consume    record usage manually (strict: no overdraft)
qpool quota remove     delete an entry and its events
qpool quota events     append-only usage history
qpool quota simulate   dry-run routing: the three bills + failover chain
qpool quota report     monthly usage + savings report
qpool quota sync       poll vendor APIs via local credentials (--apply to write)
qpool quota cpa        CLIProxyAPI control plane: status/reconcile/pull-usage
qpool quota daemon     control loop: sync -> reconcile -> pull-usage
qpool quota run        dispatch a task to the cheapest capable agent, with failover
```


## Configuration

| Variable | Purpose |
| --- | --- |
| `QPOOL_DB` | ledger path (default `~/.qpool/qpool.db`) |
| `QPOOL_CPA_URL` | CLIProxyAPI base (default `http://localhost:8317`) |
| `QPOOL_CPA_KEY` | CLIProxyAPI management key (`remote-management.secret-key`) |
| `QPOOL_STATIC_QUOTAS` | manual-channel JSON path |
| `QPOOL_COLLECTORS_DIR` | plugin collector dir (default `~/.qpool/collectors`) |
| `~/.qpool/executors.json` | per-user agent→CLI mappings for `quota run` (out of repo): `{"cli": {"<agent>": ["<cli>", "-p"]}}` |
| `~/.qpool/credentials.json` | API keys as env fallback (`{"ARK_API_KEY": "..."}`) |
| `CODEX_HOME`, `KIMI_CODE_HOME`, `COPILOT_TOKEN`, `GEMINI_TOKEN`, `CLINE_API_KEY`, `XAI_MANAGEMENT_KEY`, `XAI_TEAM_ID`, `VOLC_ACCESS_KEY_ID`, `VOLC_SECRET_ACCESS_KEY`, `ARK_API_KEY_ID`, `WINDSURF_SERVICE_KEY`, `DEVIN_API_KEY` | collector credentials |

## Plugin collectors

Internal or proprietary agents do not belong in this repo. Drop a Python file
in `~/.qpool/collectors/` exposing three symbols and it joins the registry:

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

Then `qpool quota sync --agent acme-agent --apply` flows through the same
pipeline as every built-in collector.

## How routing works

1. **Capability hard filter** — `capability_tier` of the entry must be ≥ what
   the task needs (`--tier`), and the quota unit must match (`tokens` tasks
   cannot spend `requests` quota).
2. **Marginal-cost order** — subscriptions first (already paid; the fuller
   one wins), credit packs next (soonest expiry wins — drain before it dies),
   pay-as-you-go last (cheapest unit price wins). Expired/depleted entries
   always sink to the bottom. Two strategies: `--strategy cost` (default)
   picks the cheapest entry that meets the tier floor; `--strategy capability`
   picks the strongest entry and lets cost break ties.
3. **Gateway orchestration** — that order becomes `priority` fields +
   `fill-first` on CLIProxyAPI, so the pool literally drains cheapest-first;
   exhausted credentials get disabled, recovered ones re-enabled with
   `reset-quota`.
4. **Execution** — `qpool quota run "<task>"` walks the same failover chain
   and actually dispatches: headless agent CLIs (mapped per-user in
   `~/.qpool/executors.json`) or openai-compatible gateway upstreams, with
   per-candidate timeout, a `--max-cost` payg guard, and failover on
   quota/auth failure. The repo ships no agent-to-CLI mappings — which
   harnesses you run is your configuration, not project knowledge.

## A note on ToS risk

Polling your own quota (what qpool's collectors do) is read-only. **Consuming
subscription quota through a passthrough gateway is a different matter** —
Google explicitly prohibits using an AI Pro subscription via API; other
vendors are unenforced but gray. That risk lives in the passthrough layer
(CLIProxyAPI and friends), not in this control plane. Know your vendors'
terms; qpool only makes the accounting and routing visible to you.

## Acknowledgments

qpool is an original implementation — no third-party code is copied into this
repository. Ideas and publicly observable functional facts (API endpoints,
credential locations) were informed by:

- [onWatch](https://github.com/onllm-dev/onWatch) (**GPL-3.0**) — quota polling
  and reset-cycle detection ideas. Used strictly as an **external reference**;
  not included, copied, or linked, and not required to build or run qpool.
- [claude-code-router](https://github.com/musistudio/claude-code-router) —
  credential-pool status and cooldown model ideas.
- [oh-my-codex](https://github.com/Yeachan-Heo/oh-my-codex) — dispatch state
  machine and event-sourcing ideas.
- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) (**MIT**) — the
  passthrough data plane qpool drives, as an **external process** via its
  Management API. Not bundled; install and run it separately.

See [NOTICE](NOTICE) for the full third-party acknowledgment text.

## License

[MIT](LICENSE) — see also [NOTICE](NOTICE) for third-party attributions.
