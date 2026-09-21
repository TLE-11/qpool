# qpool

**English** | [中文](README_CN.md)

**Quota pooling + cost-aware routing for AI coding agents — burn the quota you already paid for before touching the metered API.**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python ≥ 3.9](https://img.shields.io/badge/python-%3E%3D3.9-blue.svg)](pyproject.toml)
[![dependencies: 0](https://img.shields.io/badge/dependencies-0-brightgreen.svg)](pyproject.toml)

Every coding agent silently spends your most expensive quota first. qpool
flips that: it pools the scattered subscriptions, credit packs, and metered
accounts from all your agent CLIs/IDEs into one ledger, then routes each task
to the **cheapest entry that can actually do it**.

```bash
$ qpool quota simulate --amount 500000 --unit tokens --tier 3

route plan for 500,000 tokens, capability >= T3:

  PRIMARY   #2 claude-code/main credit_pack  T5  1,000,000 left   est $0
  FALLBACK  #3 kimi-cli/main payg            T3  metered          est $1

the three bills:
  routed cost   : $0      # what qpool would spend
  payg-only     : $1      # what the default path would spend
  saved by pool : $1      # ← this line is the whole point
```

The way to save tokens is not to use fewer of them. It is to move work that
would have hit a metered API onto quota you have already paid for.

## Why it exists

- Your Cursor/Kiro/Codex subscriptions are **sunk costs** — quota left unused
  at month's end is money burned anyway.
- Credit packs are **depreciating assets** — they expire whether you use them
  or not.
- Metered APIs are **real marginal spend** — and the default path drains them
  first, because no single dashboard sees the whole picture.

Nobody routes by *marginal cost*. Routers route by rate limits, by task type,
by round-robin — never by "which entry costs me nothing right now."

## Architecture in 30 seconds

qpool is a **control plane**. It deliberately does not re-implement the
passthrough gateway — that job belongs to
[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), which qpool drives
through its Management API.

```
collectors: 11 real quota APIs (codex, claude, cursor, gemini, copilot, kimi,
            cline, grok/xAI, doubao/ark, windsurf, devin) + manual + plugins
      │  qpool quota sync --apply
      ▼
┌─ quota ledger (SQLite) ─────────────────────────────┐
│  entries: kind / remaining / TTL / cost / tier       │
│  events:  append-only usage log (event sourcing)     │
└──────────────────────────────────────────────────────┘
      │  route_sort_key: capability filter → marginal-cost order
      ▼
conductor: reconcile → disable/enable + priority + fill-first
      │  Management API (CLIProxyAPI :8317)
      ▼
CLIProxyAPI — protocol bridge + multi-account pool (not reinvented here)
      │  ANTHROPIC_BASE_URL / OPENAI_BASE_URL
      ▼
Claude Code · Codex CLI · any compatible harness
      │  per-request tokens flow back via usage-queue
      ▼
task-level accounting → monthly "saved by pooling" report
```

## Quick start (3 minutes)

Python ≥ 3.9, **zero third-party dependencies**.

```bash
pip install -e . && qpool quota list

# register quota manually...
qpool quota add --agent codex --account work --kind subscription \
    --total 500 --unit requests --reset monthly --capability 4

# ...or poll the vendors' own APIs with your local CLI credentials
qpool quota sync --apply

# the three bills before you commit a task
qpool quota simulate --amount 500000 --unit tokens --tier 3

# drive a real gateway (optional but recommended)
export QPOOL_CPA_KEY=<CLIProxyAPI remote-management.secret-key>
qpool quota cpa reconcile --apply   # push cost order into the pool
qpool quota daemon                  # sync → reconcile → pull-usage, every 60s

# end of month
qpool quota report                  # real money spent vs saved by pooling
```

## Design decisions

This is the part worth reading. Each entry: options considered, what was
chosen, and why.

### 1. Control plane over yet another gateway

**Options:** (a) write my own OAuth-bridging, protocol-translating,
multi-account gateway; (b) treat the gateway as solved and build only the
decision layer.

**Chose (b).** Protocol bridging is a commodity that CLIProxyAPI has already
pushed to production quality — OAuth flows, four wire protocols, cooldowns,
multi-account rotation. Rebuilding it would take months and always lag
behind. qpool's irreplaceable value is the *cost-aware decision*, and
CLIProxyAPI's Management API turned out to be read/write complete
(`auth-files` status, `priority`, `routing/strategy`, `reset-quota`,
`usage-queue`), which makes the split real, not aspirational. The rule of
thumb I used: **own the layer where your differentiation lives, integrate
everything else.**

### 2. Marginal-cost layering instead of weighted routing

**Options:** (a) round-robin; (b) weighted random by price; (c) a strict
total order by marginal cost.

**Chose (c).** The three quota kinds are economically *different objects*,
not the same object at different weights: a subscription is a sunk cost
(marginal cost 0, renews monthly), a credit pack is a depreciating asset
(its value collapses to 0 at expiry), pay-as-you-go is true marginal spend.
That is a sequencing problem, not a probability problem — so qpool computes
a total order (subscription with most headroom → soonest-expiring pack →
cheapest unit price) and pushes it to the gateway as `fill-first` +
priorities, instead of sprinkling traffic across the pool.

### 3. One unified ledger table instead of per-provider schemas

The reference project (onWatch) creates **three tables per provider — 40+
tables for 16 providers**; adding a vendor means a schema migration. qpool
keeps one `quota_entries` table where `agent` is a column, and one
`window_key` column handles vendors with several quota clocks (Codex's 5h /
7d / credits all coexist as rows). Adding a vendor is a new row, not a new
schema. Studying an existing project critically — taking its reset-cycle
detection ideas while rejecting its data model — was more useful than
copying it.

### 4. Event sourcing, and two different write paths on purpose

Usage is an **append-only event log**, never in-place updates: auditable,
replayable, reconcilable. On top of that, the two write paths have
deliberately different strictness: manual `consume` is strict (never
overdraw, never spend expired quota — constraints), while gateway usage
backfill is `force` (consumption already happened upstream — facts).
Recording a fact past zero yields a negative `remaining`, which is a
truthful "overdrawn" signal, not an error to suppress.

### 5. Honest modeling of Fair-Use subscriptions

Subscription APIs report `used_percent`, not absolute amounts — the absolute
quota does not exist anywhere (that is what Fair Use means). qpool does not
invent numbers: percentage-based entries display `38% left` with
`total = NULL`; if the user supplies an estimated total, sync *calibrates*
`remaining = total × (1 − used%)`. A model that admits what it cannot know
beats a precise-looking fiction.

### 6. Zero dependencies, even where it hurt

Stdlib only: `sqlite3`, `argparse`, `urllib`, `hmac` — including a hand-rolled
Volcano Engine signature V4 instead of pulling the vendor SDK. Install takes
seconds, runs anywhere Python ≥ 3.9 does, and the supply-chain surface is
the stdlib and nothing else.

### 7. Open-source hygiene as a feature

Proprietary collectors (company-internal agents) live in
`~/.qpool/collectors/` — a plugin directory *outside* the repo, so internal
logic never touches the public tree. GPL-licensed reference projects were
used as external references only (ideas and publicly observable API facts,
no copied code), documented in [NOTICE](NOTICE).

## Engineering notes

- **Lazy rollover instead of a background poller** — a CLI tool should not
  need a daemon to stay correct; reset-cycle detection runs on read/write
  (when it matters), catching up missed cycles by period arithmetic.
- **message.id dedupe in transcript parsing** — coding-agent session files
  rewrite the same message several times (streaming, retries); aggregation
  dedupes by message id keeping the final, fullest record (same rule as
  ccusage), which moved a real monthly estimate from a bogus $715 to the
  correct ~$350.
- **Consumed-vs-remaining telemetry** — metering APIs (Ark, Devin) report
  consumption, not balance; the ledger accepts `consumed_abs` and derives
  `remaining = total − consumed` against the entry's budget.

## Testing

No live vendor accounts in CI, so every integration is verified against
local mock servers replaying the documented wire shapes: ~30 assertions
covering credential detection, signature-V4 request construction, response
parsing (including string numerics and nested payloads), ledger upsert
idempotency, rolling-window rollover, reconcile decisions, priority
orchestration, and usage backfill semantics.

## Collector coverage

| Agent | Credentials | Quota API |
| --- | --- | --- |
| Codex | `~/.codex/auth.json` | `chatgpt.com/backend-api/wham/usage` (5h+7d windows) |
| Claude Code | macOS Keychain / `~/.claude/.credentials.json` | `api.anthropic.com/api/oauth/usage` |
| Cursor | `state.vscdb` (read-only) | `api2.cursor.sh` Connect-RPC |
| Gemini CLI | `~/.gemini/oauth_creds.json` | `cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota` |
| Copilot | `COPILOT_TOKEN` / IDE hosts.json | `api.github.com/copilot_internal/user` |
| Kimi CLI | `~/.kimi-code/credentials/` | `api.kimi.com/coding/v1/usages` |
| Cline | `CLINE_API_KEY` | `api.cline.bot` (official REST) |
| Grok / xAI | `XAI_MANAGEMENT_KEY` | `management-api.x.ai` billing |
| Doubao / Ark | Volcano AK/SK | `GetInferenceUsage` (signature V4) |
| Windsurf / Devin | service keys | `server.codeium.com` / `api.devin.ai/v3` (Enterprise) |
| anything else | `~/.qpool/static_quotas.json` | manual channel |
| internal agents | `~/.qpool/collectors/*.py` | out-of-tree plugins |

## Acknowledgments

Original implementation; no third-party code is copied. Ideas and publicly
observable API facts informed by [onWatch](https://github.com/onllm-dev/onWatch)
(**GPL-3.0**, external reference only), [claude-code-router](https://github.com/musistudio/claude-code-router),
[oh-my-codex](https://github.com/Yeachan-Heo/oh-my-codex), and
[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) (**MIT**, external
process). See [NOTICE](NOTICE).

## License

[MIT](LICENSE)
