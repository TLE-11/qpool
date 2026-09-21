"""qpool command line interface."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from typing import Any, List, Sequence

from . import ledger
from .db import Database

# Known upstream OpenAI-compatible provider presets for `cpa register-provider`.
PROVIDER_PRESETS = {
    "ark": {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        # Agent Plan / Coding Plan subscribers use a different endpoint family
        "plan_base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
        "key_env": "ARK_API_KEY",
    },
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qpool",
        description="Quota pooling + cost-aware routing control plane for coding agents",
    )
    sub = p.add_subparsers(dest="resource", required=True)
    quota = sub.add_parser("quota", help="quota ledger operations")
    qsub = quota.add_subparsers(dest="action", required=True)

    add = qsub.add_parser("add", help="register a quota entry")
    add.add_argument("--agent", required=True,
                     help="agent id: codex, claude-code, gemini-cli, copilot-cli, grok-cli, "
                          "kimi-cli, qoder, cursor, windsurf, kiro, cline, opencode, aider, "
                          "doubao, devin, antigravity, internal, ...")
    add.add_argument("--account", required=True, help="account label: work, personal, an email, ...")
    add.add_argument("--kind", required=True, choices=ledger.KINDS,
                     help="subscription (already paid, marginal cost ~0) | "
                          "credit_pack (prepaid, may expire) | payg (pay-as-you-go, real money)")
    add.add_argument("--capability", type=int, default=3, metavar="TIER",
                     help="capability tier of the underlying model, 1-5 (default 3)")
    add.add_argument("--unit", help="quota unit: tokens / requests / credits / usd (default per kind)")
    add.add_argument("--total", type=float, help="total quota (per period for subscriptions)")
    add.add_argument("--remaining", type=float, help="initial remaining quota (default = total)")
    add.add_argument("--cost-per-unit", type=float, default=0.0,
                     help="marginal cost in USD per unit (payg requires > 0)")
    add.add_argument("--expires", help="credit-pack expiry: YYYY-MM-DD or ISO 8601")
    add.add_argument("--reset", dest="reset_period", choices=ledger.RESET_PERIODS,
                     help="billing period for subscriptions")
    add.add_argument("--notes", help="free-form notes")

    ls = qsub.add_parser("list", help="list quota entries (default: marginal-cost route order)")
    ls.add_argument("--agent")
    ls.add_argument("--account")
    ls.add_argument("--kind", choices=ledger.KINDS)
    ls.add_argument("--sort", choices=("cost", "expiry", "agent", "tier"), default="cost")

    use = qsub.add_parser("consume", help="record usage against an entry")
    use.add_argument("id", type=int, help="quota entry id")
    use.add_argument("--amount", type=float, required=True, help="units consumed")
    use.add_argument("--input-tokens", type=int, help="cost composition: input tokens")
    use.add_argument("--output-tokens", type=int, help="cost composition: output tokens")
    use.add_argument("--cached-tokens", type=int, help="cost composition: cached tokens")
    use.add_argument("--tool-calls", type=int, help="cost composition: tool calls")
    use.add_argument("--task", dest="task_ref", help="task reference for per-task accounting")
    use.add_argument("--note")

    rm = qsub.add_parser("remove", help="delete an entry (and its usage events)")
    rm.add_argument("id", type=int, help="quota entry id")

    ev = qsub.add_parser("events", help="show usage event history")
    ev.add_argument("--entry", type=int, help="filter by quota entry id")
    ev.add_argument("--limit", type=int, default=50)

    sim = qsub.add_parser("simulate",
                          help="dry-run routing for a task: three bills before you commit")
    sim.add_argument("--amount", type=float, required=True, help="task size estimate")
    sim.add_argument("--unit", required=True,
                     help="tokens / requests / credits (must match entry units)")
    sim.add_argument("--tier", type=int, default=1, metavar="MIN-TIER",
                     help="minimum capability tier the task needs, 1-5 (default 1 = any)")
    sim.add_argument("--strategy", choices=ledger.STRATEGIES, default="cost",
                     help="cost: cheapest entry that meets the tier floor (default); "
                          "capability: strongest entry first, cost breaks ties")

    rep = qsub.add_parser("report", help="monthly usage + savings report")
    rep.add_argument("--month", help="YYYY-MM (default: current month, UTC)")

    syn = qsub.add_parser("sync",
                          help="poll vendor quota APIs via local agent credentials (dry-run unless --apply)")
    syn.add_argument("--agent", help="only sync this agent (default: all with collectors)")
    syn.add_argument("--apply", action="store_true", help="write readings into the ledger")
    syn.add_argument("--account", default="auto",
                     help="account label for auto-created entries (default: auto)")

    cpa = qsub.add_parser("cpa", help="CLIProxyAPI control plane (passthrough gateway)")
    cpa_sub = cpa.add_subparsers(dest="cpa_action", required=True)
    cpa_sub.add_parser("status", help="credential pool x ledger reconciliation view")
    rec = cpa_sub.add_parser("reconcile",
                           help="push cost-aware disable/enable decisions (dry-run by default)")
    rec.add_argument("--apply", action="store_true", help="actually write decisions to CLIProxyAPI")
    rec.add_argument("--strategy", choices=("fill-first", "round-robin"), default="fill-first",
                     help="routing strategy pushed with --apply (default fill-first)")
    rec.add_argument("--no-priority", action="store_true",
                     help="skip pushing priority fields (keep disable/enable only)")
    pull = cpa_sub.add_parser("pull-usage", help="drain per-request usage queue into the ledger")
    pull.add_argument("--count", type=int, default=100, help="max records to drain (default 100)")

    dm = qsub.add_parser("daemon", help="run the control loop: sync -> reconcile -> pull-usage")
    dm.add_argument("--interval", type=int, default=60,
                    help="seconds between rounds (default 60)")
    dm.add_argument("--once", action="store_true",
                    help="run a single round and exit (cron/launchd friendly)")

    run = qsub.add_parser("run",
                          help="dispatch a task to the cheapest capable agent, with failover")
    run.add_argument("task", help="the task prompt to execute")
    run.add_argument("--amount", type=float, default=50000,
                     help="estimated task size in tokens for cost planning (default 50000)")
    run.add_argument("--tier", type=int, default=1, metavar="MIN-TIER",
                     help="minimum capability tier the task needs, 1-5 (default 1)")
    run.add_argument("--cwd", help="working directory for CLI agents (default: current dir)")
    run.add_argument("--timeout", type=int, default=900,
                     help="per-candidate timeout in seconds (default 900)")
    run.add_argument("--max-cost", type=float,
                     help="skip payg candidates whose est. marginal cost exceeds this (USD)")
    run.add_argument("--strategy", choices=ledger.STRATEGIES, default="cost",
                     help="cost: cheapest capable entry (default); capability: strongest first")
    run.add_argument("--dry-run", action="store_true",
                     help="print the dispatch plan without executing")

    cpa_sub.add_parser("providers", help="list upstream OpenAI-compatible providers")
    reg = cpa_sub.add_parser("register-provider",
                             help="register an upstream OpenAI-compatible provider (e.g. ark)")
    reg.add_argument("--name", required=True, help="provider name, e.g. doubao")
    reg.add_argument("--preset", choices=tuple(PROVIDER_PRESETS),
                     help="known endpoint preset (ark = Volcano Engine Ark)")
    reg.add_argument("--plan", action="store_true",
                     help="ark preset: use the Agent/Coding Plan endpoint (/api/plan/v3)")
    reg.add_argument("--base-url", help="upstream base URL (required without --preset)")
    reg.add_argument("--api-key-env",
                     help="env var holding the API key (default from preset)")
    reg.add_argument("--model", action="append", default=[],
                     help="model mapping 'name' or 'name:alias' (repeatable)")
    rmprov = cpa_sub.add_parser("remove-provider", help="remove an upstream provider")
    rmprov.add_argument("--name", required=True)
    return p


# ---------- formatting ----------

def fmt_num(x: Any) -> str:
    if x is None:
        return "-"
    if float(x) == int(x):
        return f"{int(x):,}"
    return f"{x:,.2f}"


def fmt_cost(c: Any) -> str:
    if c is None:
        return "-"
    if c == 0:
        return "$0"
    if c < 0.01:
        return f"${c:.6f}".rstrip("0")
    return f"${c:,.4f}".rstrip("0").rstrip(".")


def human_delta(future: datetime, now: datetime) -> str:
    secs = int((future - now).total_seconds())
    if secs <= 0:
        return "expired"
    days, secs = divmod(secs, 86400)
    hours, secs = divmod(secs, 3600)
    minutes = secs // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def render_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*("-" * w for w in widths))]
    for row in rows:
        lines.append(fmt.format(*(str(c) for c in row)))
    return "\n".join(lines)


# ---------- commands ----------

def cmd_quota_add(db: Database, args: argparse.Namespace) -> None:
    entry_id = ledger.add_entry(
        db,
        agent=args.agent,
        account=args.account,
        kind=args.kind,
        capability_tier=args.capability,
        unit=args.unit,
        total=args.total,
        remaining=args.remaining,
        cost_per_unit=args.cost_per_unit,
        expires_at=args.expires,
        reset_period=args.reset_period,
        notes=args.notes,
    )
    print(f"added quota entry #{entry_id}")
    if args.kind == "credit_pack" and not args.expires:
        print("hint: credit pack without --expires will not get expiry warnings", file=sys.stderr)


def cmd_quota_list(db: Database, args: argparse.Namespace) -> None:
    reset = ledger.apply_due_resets(db)
    if reset:
        print(f"(rolled over {reset} subscription quota(s) into a new billing period)")
    entries = ledger.list_entries(db, agent=args.agent, account=args.account, kind=args.kind)
    if not entries:
        print("no quota entries yet; add one with: qpool quota add --agent ... --account ... --kind ...")
        return
    if args.sort == "cost":
        entries.sort(key=ledger.route_sort_key)
    elif args.sort == "expiry":
        entries.sort(key=lambda e: (e["expires_at"] is None, e["expires_at"] or "", e["id"]))
    elif args.sort == "agent":
        entries.sort(key=lambda e: (e["agent"], e["account"], e["id"]))
    else:  # tier
        entries.sort(key=lambda e: (-e["capability_tier"], ledger.route_sort_key(e)))

    now = ledger.now_utc()
    headers = ["ID", "AGENT", "ACCOUNT", "KIND", "TIER", "QUOTA", "UNIT", "COST/U", "EXPIRES", "RESET", "STATUS"]
    rows: List[List[Any]] = []
    counts = {ledger.STATUS_ACTIVE: 0, ledger.STATUS_EXPIRING: 0,
              ledger.STATUS_EXPIRED: 0, ledger.STATUS_DEPLETED: 0}
    for e in entries:
        counts[e["status"]] += 1
        if e["remaining"] is not None and e["remaining"] < 0:
            quota = f"used {fmt_num(-e['remaining'])}"  # net consumption, no total known
        elif e["remaining"] is not None:
            quota = f"{fmt_num(e['remaining'])}/{fmt_num(e['total'])}"
        elif e["used_percent"] is not None:
            quota = f"{100.0 - e['used_percent']:.0f}% left"
        else:
            quota = "-"
        if e["expires_at"]:
            expires = human_delta(ledger.parse_dt(e["expires_at"]), now)
        elif e["window_seconds"] and e["period_start"]:
            from datetime import timedelta as _td
            resets_at = ledger.parse_dt(e["period_start"]) + _td(seconds=e["window_seconds"])
            expires = human_delta(resets_at, now)
        else:
            expires = "-"
        rows.append([
            e["id"], e["agent"], e["account"], e["kind"], f"T{e['capability_tier']}",
            quota, e["unit"], fmt_cost(e["cost_per_unit"]), expires,
            e["reset_period"] or "-", e["status"],
        ])
    print(render_table(headers, rows))
    print(
        f"\n{len(entries)} entries | active {counts[ledger.STATUS_ACTIVE]}, "
        f"expiring<=7d {counts[ledger.STATUS_EXPIRING]}, depleted {counts[ledger.STATUS_DEPLETED]}, "
        f"expired {counts[ledger.STATUS_EXPIRED]}"
    )
    if args.sort == "cost":
        print("route order: subscription (already paid, cost ~0) -> expiring credit packs -> "
              "payg (real money); expired/depleted last")


def cmd_quota_consume(db: Database, args: argparse.Namespace) -> None:
    result = ledger.consume(
        db, args.id, args.amount,
        input_tokens=args.input_tokens,
        output_tokens=args.output_tokens,
        cached_tokens=args.cached_tokens,
        tool_calls=args.tool_calls,
        task_ref=args.task_ref,
        note=args.note,
    )
    e = result["entry"]
    print(f"consumed {fmt_num(args.amount)} {e['unit']} from #{e['id']} "
          f"({e['agent']}/{e['account']} {e['kind']})")
    if result["remaining"] is not None:
        print(f"remaining: {fmt_num(result['remaining'])}/{fmt_num(e['total'])} {e['unit']}")
    if result["est_cost"] > 0:
        print(f"est. marginal cost: {fmt_cost(result['est_cost'])}")
    else:
        print("est. marginal cost: $0 (already-paid quota)")


def cmd_quota_remove(db: Database, args: argparse.Namespace) -> None:
    events = ledger.remove_entry(db, args.id)
    print(f"removed quota entry #{args.id} (deleted {events} usage event(s))")


def cmd_quota_events(db: Database, args: argparse.Namespace) -> None:
    rows = ledger.list_events(db, entry_id=args.entry, limit=args.limit)
    if not rows:
        print("no usage events recorded yet")
        return
    headers = ["ID", "ENTRY", "AGENT", "AMOUNT", "UNIT", "EST.COST",
               "IN", "OUT", "CACHED", "TOOLS", "TASK", "WHEN (UTC)", "NOTE"]
    body: List[List[Any]] = []
    for r in rows:
        when = (r["created_at"] or "")[:16].replace("T", " ")
        body.append([
            r["id"], r["entry_id"], r["agent"], fmt_num(r["amount"]), r["unit"],
            fmt_cost(r["est_cost_usd"]),
            fmt_num(r["input_tokens"]), fmt_num(r["output_tokens"]),
            fmt_num(r["cached_tokens"]), fmt_num(r["tool_calls"]),
            r["task_ref"] or "-", when, r["note"] or "-",
        ])
    print(render_table(headers, body))


def cmd_quota_simulate(db: Database, args: argparse.Namespace) -> None:
    plan = ledger.plan_route(db, args.amount, args.unit, args.tier,
                             strategy=args.strategy)
    print(f"route plan for {fmt_num(plan['amount'])} {plan['unit']}, "
          f"capability >= T{plan['min_tier']}, strategy={args.strategy}:\n")
    if not plan["candidates"]:
        print("  no eligible entries.")
    for i, c in enumerate(plan["candidates"]):
        if i == plan["primary_idx"]:
            role = "PRIMARY "
        elif plan["primary_idx"] is not None and i > plan["primary_idx"]:
            role = "FALLBACK"
        else:
            role = "NO-COVER"
        quota = "metered" if c["remaining"] is None else f"{fmt_num(c['remaining'])} left"
        cover_note = "" if c["covers"] else "  (insufficient alone; failover mid-task)"
        print(f"  {role}  #{c['id']} {c['agent']}/{c['account']} {c['kind']:<12} "
              f"T{c['capability_tier']}  {quota:<16} est {fmt_cost(c['est'])}{cover_note}")
    for s in plan["skipped"]:
        print(f"  SKIP      #{s['id']} {s['agent']}/{s['account']} {s['kind']:<12} {s['why']}")
    print("\nthe three bills:")
    if plan["routed_cost"] is None:
        print("  routed cost   : no eligible entry can fully cover this amount")
    else:
        print(f"  routed cost   : {fmt_cost(plan['routed_cost'])}")
    if plan["payg_only_cost"] is None:
        print("  payg-only     : no payg baseline in the ledger for this unit")
    else:
        print(f"  payg-only     : {fmt_cost(plan['payg_only_cost'])}")
    if plan["saved"] is not None:
        print(f"  saved by pool : {fmt_cost(plan['saved'])}")


def cmd_quota_report(db: Database, args: argparse.Namespace) -> None:
    month = args.month or ledger.now_utc().strftime("%Y-%m")
    rep = ledger.monthly_report(db, month)
    print(f"{rep['month']} usage report (UTC):\n")
    if not rep["rows"]:
        print("  no usage events recorded this month.")
        return
    headers = ["AGENT", "KIND", "AMOUNT", "UNIT", "EVENTS", "MARGINAL COST"]
    body = [[r["agent"], r["kind"], fmt_num(r["amount"]), r["unit"], r["n"],
             fmt_cost(r["cost"])] for r in rep["rows"]]
    print(render_table(headers, body))
    print(f"\n  real money spent (payg)       : {fmt_cost(rep['spent'])}")
    print(f"  pooled usage, payg-equivalent : {fmt_cost(rep['pooled_value'])}")
    print(f"  saved by pooling              : {fmt_cost(rep['saved'])}")
    if not rep["baselines"]:
        print("\n  (savings need a payg baseline: add a payg entry to price pooled usage)")


def cmd_quota_sync(db: Database, args: argparse.Namespace) -> None:
    from .collectors import COLLECTORS, CollectorError

    agents = [args.agent] if args.agent else list(COLLECTORS)
    rows: List[List[Any]] = []
    failures = 0
    for agent in agents:
        mod = COLLECTORS.get(agent)
        agent_filter = None
        if mod is None:
            # unknown agent name -> try the manual/static config
            manual_mod = COLLECTORS["manual"]
            creds = manual_mod.detect_credentials()
            if creds is not None and manual_mod.has_agent(creds, agent):
                mod = manual_mod
                agent_filter = agent
            else:
                print(f"error: no collector for {agent!r}; available: {', '.join(COLLECTORS)}, "
                      f"or declare it in static_quotas.json", file=sys.stderr)
                sys.exit(1)
        else:
            creds = mod.detect_credentials()
        if creds is None:
            rows.append([agent, "-", "-", "-", "-", "-", "no credentials found"])
            failures += 1
            continue
        try:
            if agent_filter is not None:
                readings = mod.fetch_readings(creds, agent_filter)
            else:
                readings = mod.fetch_readings(creds)
        except CollectorError as exc:
            rows.append([agent, "-", "-", "-", "-", "-", f"error: {exc}"])
            failures += 1
            continue
        for r in readings:
            remaining = fmt_num(r.remaining_abs) if r.remaining_abs is not None else "-"
            used = "-" if r.used_percent is None else f"{r.used_percent:.0f}%"
            resets = "-" if not r.resets_at else human_delta(r.resets_at, ledger.now_utc())
            if args.apply:
                outcome = ledger.upsert_reading(db, r, account_label=args.account)
                last_col = f"{outcome['action']} #{outcome['entry_id']}"
            else:
                last_col = r.label
            rows.append([agent, r.window_key, used, remaining, resets, r.plan or "-", last_col])
    print(render_table(
        ["AGENT", "WINDOW", "USED", "REMAINING", "RESETS-IN", "PLAN",
         "ACTION" if args.apply else "LABEL"],
        rows))
    if not args.apply:
        print("\ndry-run only; re-run with --apply to write these readings into the ledger")
    if failures:
        print(f"note: {failures} agent(s) had no usable credentials or API errors",
              file=sys.stderr)
    from .collectors import PLUGIN_ERRORS, plugin_dir
    for err in PLUGIN_ERRORS:
        print(f"plugin warning ({plugin_dir()}): {err}", file=sys.stderr)


def cmd_quota_cpa(db: Database, args: argparse.Namespace) -> None:
    from . import conductor
    from .collectors.base import CollectorError
    from .cpa import CpaClient

    cpa = CpaClient.from_env()
    try:
        if args.cpa_action in ("status", "reconcile"):
            do_apply = args.cpa_action == "reconcile" and args.apply
            strategy = args.strategy if do_apply else None
            set_priority = args.cpa_action == "reconcile" and not args.no_priority
            plan = conductor.reconcile(db, cpa, apply=do_apply,
                                       strategy=strategy, set_priority=set_priority)
            if not plan:
                print("CLIProxyAPI credential pool is empty (no auth files)")
                return
            headers = ["NAME", "PROVIDER", "CPA-STATUS", "DISABLED", "LEDGER",
                       "PRIORITY", "DECISION", "REASON"]
            rows = [[
                p["name"], p["provider"], p["cpa_status"],
                "yes" if p["disabled"] else "no",
                ",".join(f"#{i}" for i in p["ledger_entries"]) or "-",
                (str(p["priority"]) + ("*" if p["priority_applied"] else ""))
                if p["priority"] is not None else "-",
                p["decision"] + (" (applied)" if p["applied"] else ""),
                p["reason"],
            ] for p in plan]
            print(render_table(headers, rows))
            if args.cpa_action == "reconcile" and not do_apply:
                print("\ndry-run only; re-run with --apply to push decisions "
                      "(priority + strategy=fill-first included)")
        elif args.cpa_action == "pull-usage":
            result = conductor.pull_usage(db, cpa, count=args.count)
            print(f"pulled {result['records']} usage record(s) from CLIProxyAPI: "
                  f"{result['consumed']} booked ({fmt_num(result['tokens'])} tokens), "
                  f"{len(result['skipped'])} skipped")
            if result["skipped"]:
                print("skipped (no matching ledger entry): "
                      + ", ".join(result["skipped"][:10]))
        elif args.cpa_action == "providers":
            providers = cpa.list_openai_providers()
            if not providers:
                print("no upstream OpenAI-compatible providers registered")
            else:
                headers = ["NAME", "BASE-URL", "MODELS", "KEYS", "DISABLED"]
                rows = []
                for p in providers:
                    models = p.get("models") or []
                    model_str = ",".join((m.get("alias") or m.get("name") or "?")
                                         for m in models[:4]) or "-"
                    keys = p.get("api-key-entries") or []
                    rows.append([p.get("name"), p.get("base-url"), model_str,
                                 len(keys), "yes" if p.get("disabled") else "no"])
                print(render_table(headers, rows))
        elif args.cpa_action == "register-provider":
            preset = PROVIDER_PRESETS.get(args.preset) if args.preset else None
            base_url = args.base_url
            if preset:
                base_url = preset["plan_base_url"] if args.plan else preset["base_url"]
            if not base_url:
                print("error: --base-url is required without --preset", file=sys.stderr)
                sys.exit(1)
            key_env = args.api_key_env or (preset and preset.get("key_env"))
            if not key_env:
                print("error: --api-key-env is required", file=sys.stderr)
                sys.exit(1)
            from .collectors.base import credential
            api_key = credential(key_env)
            if not api_key:
                print(f"error: {key_env} not found; export it or add it to "
                      f"~/.qpool/credentials.json", file=sys.stderr)
                sys.exit(1)
            models: List[dict] = []
            for m in args.model:
                if ":" in m:
                    mname, alias = m.split(":", 1)
                    models.append({"name": mname, "alias": alias})
                else:
                    models.append({"name": m})
            outcome = cpa.upsert_openai_provider(
                name=args.name, base_url=base_url, api_key=api_key,
                models=models or None)
            print(f"{outcome} provider '{args.name}' -> {base_url}"
                  + (f" ({len(models)} model(s))" if models else ""))
            print("note: API key forwarded from env straight to CLIProxyAPI; "
                  "qpool itself stores nothing")
        elif args.cpa_action == "remove-provider":
            cpa.delete_openai_provider(args.name)
            print(f"removed provider '{args.name}'")
        print("\nrisk note: passthrough consumption of subscription quotas may violate "
              "vendor ToS\n(Google explicitly prohibits AI Pro subscription use via API; "
              "others are unenforced but gray).\nRisk sits in the passthrough layer; "
              "qpool's own quota polling is read-only.")
    except CollectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_quota_run(db: Database, args: argparse.Namespace) -> None:
    from . import executors

    plan = ledger.plan_route(db, args.amount, "tokens", args.tier,
                             unit_compat=True, strategy=args.strategy)
    chain = []
    for c in plan["candidates"]:
        agent = c["agent"]
        channel = "cli" if agent in executors.CLI_EXECUTORS else "gateway"
        chain.append((c, agent, channel))
    if not chain:
        print("error: no eligible entries with an execution channel.\n"
              "hint: sync your ledger first (`qpool quota sync --apply`), and map "
              "agent CLIs in ~/.qpool/executors.json, e.g.\n"
              '  {"cli": {"<agent-name>": ["<cli-command>", "-p"]}}', file=sys.stderr)
        sys.exit(1)

    print(f"dispatch plan (~{fmt_num(args.amount)} tokens, tier>=T{args.tier}):")
    for i, (c, agent, channel) in enumerate(chain):
        role = "PRIMARY " if i == 0 else "FALLBACK"
        est = fmt_cost(c["est"])
        print(f"  {role}  {agent:<12} [{channel}] entry #{c['id']}  est {est}")
    if args.dry_run:
        print("\ndry-run only; re-run without --dry-run to execute")
        return

    for c, agent, channel in chain:
        est_cost = c["est"]
        if args.max_cost is not None and c["kind"] == "payg" and est_cost > args.max_cost:
            print(f"skip {agent}: est {fmt_cost(est_cost)} exceeds --max-cost")
            continue
        print(f"\n→ trying {agent} [{channel}] (est {fmt_cost(est_cost)})...", flush=True)
        result = executors.execute(agent, args.task, cwd=args.cwd, timeout=args.timeout)
        if result.success:
            if result.output.strip():
                print(result.output.rstrip())
            print(f"\n✓ completed via {agent} in {result.duration_s:.1f}s")
            if channel == "cli":
                print(f"  usage will appear in the ledger after `qpool quota sync --apply`")
            else:
                print(f"  book it now with `qpool quota cpa pull-usage`")
            return
        print(f"✗ {agent} failed ({result.error.strip()[:160] or 'unknown error'}); "
              f"failing over...", flush=True)
    print("error: every candidate failed; see messages above", file=sys.stderr)
    sys.exit(1)


def cmd_quota_daemon(db: Database, args: argparse.Namespace) -> None:
    import fcntl
    import time as _time

    from . import conductor
    from .collectors.base import CollectorError
    from .cpa import CpaClient

    def run_once(cpa: CpaClient) -> str:
        stamp = _time.strftime("%H:%M:%S")
        sync_res = conductor.sync_all(db)
        actions = 0
        booked = 0
        if cpa.available():
            try:
                plan = conductor.reconcile(db, cpa, apply=True,
                                           strategy="fill-first", set_priority=True)
                actions = sum(1 for p in plan if p["applied"] or p["priority_applied"])
                booked = conductor.pull_usage(db, cpa, count=500)["consumed"]
            except CollectorError as exc:
                return f"[{stamp}] synced={sync_res['synced']} cpa error: {exc}"
        suffix = f" sync-errors={len(sync_res['errors'])}" if sync_res["errors"] else ""
        return f"[{stamp}] synced={sync_res['synced']} scheduled={actions} booked={booked}{suffix}"

    cpa = CpaClient.from_env()
    if args.once:
        print(run_once(cpa))
        return
    # single-instance guard (omx authority-lease style, Unix only)
    lock_path = db.path.parent / "qpool.daemon.lock"
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"error: another qpool daemon already holds {lock_path}", file=sys.stderr)
        sys.exit(1)
    print(f"qpool daemon started (interval {args.interval}s, Ctrl+C to stop)")
    try:
        while True:
            try:
                print(run_once(cpa), flush=True)
            except Exception as exc:  # one bad round must not kill the daemon
                print(f"round failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            _time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\ndaemon stopped")


def main(argv: Any = None) -> None:
    args = build_parser().parse_args(argv)
    handlers = {
        ("quota", "add"): cmd_quota_add,
        ("quota", "list"): cmd_quota_list,
        ("quota", "consume"): cmd_quota_consume,
        ("quota", "remove"): cmd_quota_remove,
        ("quota", "events"): cmd_quota_events,
        ("quota", "simulate"): cmd_quota_simulate,
        ("quota", "report"): cmd_quota_report,
        ("quota", "sync"): cmd_quota_sync,
        ("quota", "cpa"): cmd_quota_cpa,
        ("quota", "daemon"): cmd_quota_daemon,
        ("quota", "run"): cmd_quota_run,
    }
    db = Database()
    try:
        handlers[(args.resource, args.action)](db, args)
    except ledger.LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()
