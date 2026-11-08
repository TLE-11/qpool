<p align="right"><a href="README_CN.md">简体中文</a></p>

<h1 align="center">qpool</h1>

<p align="center">
  <strong>Put your Codex subscriptions to work together.</strong><br>
  Local routing across accounts and models: regular models for routine work, primary models for harder tasks.
</p>

<p align="center">
  <a href="#get-started">Get started</a> ·
  <a href="docs/local-console.md">Documentation</a> ·
  <a href="#how-routing-works">Routing</a> ·
  <a href="LICENSE">License</a>
</p>

Keep working in the Codex desktop app or CLI. qpool connects your Plus, Pro, and
OpenAI API accounts, assesses each new user turn, and selects a model from the
regular or primary group. Manage accounts, model catalogs, and quota windows in
one local console, then see which account and model handled each request.

New turns can select a different model; tool continuations and compaction keep
the current turn's selection. The account stays fixed within a session. Paid
API use requires explicit permission in the routing plan.

## Why qpool

| | |
| --- | --- |
| **Choose models by task difficulty** | Send routine turns to the regular group and complex or uncertain work to the primary group. |
| **Use accounts in order** | Configure a sequence such as Plus → Pro → OpenAI API, with quota and capability checks before execution. |
| **Stay in Codex** | Use the desktop app or CLI you already have, with `qpool auto` as the routing entry. |
| **See what actually ran** | Inspect routing decisions, selected accounts and models, execution status, and Token usage. |

qpool supports model-based difficulty assessment, local rules, a fixed primary
group, and a compatible external HiRoute decision service. Account setup, route
configuration, and Agent access are available through the local console.

## Get started

### Install and start

Requires Python 3.9+ and an installed Codex client. Subscription accounts also
require CLIProxyAPI; OpenAI API accounts connect directly to OpenAI.

```bash
git clone https://github.com/TLE-11/qpool.git
cd qpool
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m qp web --port 8765
```

Open <http://127.0.0.1:8765>.

### Configure your first route

1. In **Accounts and models**, connect an existing Codex login, sign in to
   another subscription account, or add an existing OpenAI API key. Sync model
   catalogs and quota windows.
2. In **Routing plans**, select model-based judgment, configure the regular and
   primary groups, and choose the decision model and account order. Enable API
   billing only if you want the route to use it.
3. In **Agent access**, enable the plan and connect the Codex desktop app, or
   use the generated CLI entry. Desktop setup backs up the configuration before
   changing it; restart the app to load the entry.
4. Start a new Codex session with `qpool auto` and work as usual.
5. In **Sessions and usage**, inspect the routing decision and execution record.

See the [local console guide](docs/local-console.md) for account setup, existing
chat access, and restoring the original Codex configuration.

For native ↔ qpool switching with different model context windows, use the
optional [App Server model-sync entry](docs/app-server-routing.md). It selects
the model before Codex prepares each turn. Initial desktop attachment needs one
restart; subsequent ordinary chat turns switch without restarting. The guide
includes live verification results and the current queued-followup limitation.

## See routing results

Each request records the selected account and model, execution status, and
Token usage. Decision calls have their own records, including the chosen group,
probability, threshold, latency, and usage, linked to the execution they informed.

Decision-model usage is separate from execution usage. The console does not
estimate charges or treat a completed request as proof of routing accuracy or
cost savings.

## How routing works

For each new user turn, qpool sends the current task and visible text context to
the configured decision model. In model-based routing, the regular group is
selected when the simple-task probability meets your threshold; otherwise the
primary group is selected. Invalid, timed-out, or unavailable decisions fall
back to the primary group, still subject to account and quota checks.

Within the selected group, qpool checks candidates in the configured order. For
example, a new session can prefer Plus, then Pro, then an explicitly enabled
OpenAI API account. Unknown subscription quota is not treated as exhausted and
does not trigger paid API use.

The selected account stays bound to the session. A new user turn can choose a
different model on that account; tool continuations and compaction reuse the
current turn's binding. If the account can no longer serve the route, start a
new session. Failed requests and interrupted streams are not replayed on
another account.

qpool reuses the task-degree policy from HiRoute's `smart_saving` path. It does
not implement HiRoute's full routing and evaluation system, including
competence-based downgrade protection and stage-performance scoring. Policy
provenance and scope are documented in [SOURCE.md](qp/_vendor/hiroute/SOURCE.md).

## Local data and configuration

The console and gateway listen only on loopback. Subscription logins use
isolated profiles; API keys are stored in protected local files rather than
embedded in route configuration. Connecting the desktop app is an explicit
action, with a backup and a restore option.

The default configuration is `~/.qpool/pools.json`; the quota ledger is
`~/.qpool/qpool.db`. Use `QPOOL_CONFIG` and `QPOOL_DB` to override those paths.
Check your provider's terms before using a subscription adapter.

## Documentation

- [Local console guide](docs/local-console.md) — accounts, model sync, routing plans, desktop access, and rollback.
- [Accounts, pools, and channels](docs/account-pools.md) — quota semantics and the existing `qpool quota` CLI.
- [Browser and desktop regression](docs/browser-desktop-regression.md) — validation records and remaining limits.
- [Two-account example](examples/pools.example.json) — replace placeholders before use.

## License

qpool is licensed under the [MIT License](LICENSE). The bundled HiRoute policy
sources use Apache-2.0; see [SOURCE.md](qp/_vendor/hiroute/SOURCE.md) and
[NOTICE](NOTICE).
