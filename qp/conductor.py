"""Conductor: reconcile the qpool ledger with the CLIProxyAPI credential pool.

The ledger knows marginal costs and quota states; CLIProxyAPI knows how to
route requests. The conductor matches ledger entries to CPA credentials and
pushes cost-aware scheduling decisions:

- all of a credential's ledger windows depleted/expired -> disable it
- any window recovered (rollover, fresh sync)          -> enable + reset-quota
- short windows (codex 5h) are left to CPA's own cooldown; qpool only acts on
  decisive exhaustion (every tracked window dead)

It also drains CPA's per-request usage queue into the ledger's usage_events,
which is the data source for task-level accounting and the savings report.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import ledger
from .cpa import CpaClient
from .db import Database

# ledger agent -> candidate CPA provider names (auth-files[].provider)
AGENT_TO_CPA_PROVIDERS = {
    "codex": ["codex", "openai"],
    "claude-code": ["claude", "anthropic"],
    "gemini-cli": ["gemini"],
    "grok": ["grok", "xai"],
    "kimi-cli": ["kimi", "moonshot"],
    "cursor": ["cursor"],
    "copilot": ["copilot", "github-copilot"],
}

DECISION_ENABLE = "enable"
DECISION_DISABLE = "disable"
DECISION_KEEP = "keep"
DECISION_UNTRACKED = "untracked"


def _providers_for(agent: str) -> List[str]:
    return AGENT_TO_CPA_PROVIDERS.get(agent, [agent])


def _cred_identity(file: Dict[str, Any]) -> str:
    """Best-effort account identity of a CPA credential."""
    for key in ("email", "name", "label", "id"):
        value = file.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def match_credentials(
    entries: List[Dict[str, Any]],
    files: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Pair each CPA credential with its ledger entries (same agent + account)."""
    matched: List[Dict[str, Any]] = []
    for f in files:
        provider = (f.get("provider") or "").lower()
        identity = _cred_identity(f).lower()
        owned: List[Dict[str, Any]] = []
        for e in entries:
            if provider and provider not in _providers_for(e["agent"]):
                continue
            account = (e["account"] or "").lower()
            if identity and account and (account in identity or identity in account):
                owned.append(e)
            elif identity and not account:
                owned.append(e)  # ledger auto-created entries use account "auto"
        matched.append({"file": f, "entries": owned})
    return matched


def decide(entries: List[Dict[str, Any]], file: Dict[str, Any]) -> Tuple[str, str]:
    """Scheduling decision for one credential.

    Returns (decision, reason). decision in DECISION_*.
    """
    disabled_now = bool(file.get("disabled"))
    if not entries:
        return DECISION_UNTRACKED, "no ledger entries; run `qpool quota sync --apply` first"
    now = ledger.now_utc()
    statuses = [ledger.entry_status(e, now) for e in entries]
    dead = all(s in (ledger.STATUS_EXPIRED, ledger.STATUS_DEPLETED) for s in statuses)
    if dead:
        if disabled_now:
            return DECISION_KEEP, "already disabled; all windows dead"
        return DECISION_DISABLE, f"all tracked windows exhausted: {', '.join(statuses)}"
    if disabled_now:
        return DECISION_ENABLE, f"quota recovered ({', '.join(statuses)})"
    return DECISION_KEEP, f"active ({', '.join(statuses)})"


def _pair_sort_key(pair: Dict[str, Any]) -> Any:
    """A credential ranks by its BEST ledger window (marginal-cost order)."""
    entries = pair["entries"]
    if not entries:
        return (1, 0, 0, 0, 0)  # untracked credentials always last
    best = min(ledger.route_sort_key(e) for e in entries)
    return (0,) + tuple(best) if isinstance(best, tuple) else (0, best)


def compute_priorities(
    entries: List[Dict[str, Any]],
    files: List[Dict[str, Any]],
) -> Dict[str, int]:
    """Full ordering of the credential pool by marginal cost.

    Returns {credential_name: priority} where lower = consume first
    (steps of 10 leave room for manual interleaving).
    """
    pairs = match_credentials(entries, files)
    ordered = sorted(pairs, key=_pair_sort_key)
    priorities: Dict[str, int] = {}
    for rank, pair in enumerate(ordered):
        name = _cred_identity(pair["file"]) or (pair["file"].get("name") or "?")
        priorities[name] = (rank + 1) * 10
    return priorities


def reconcile(
    db: Database,
    cpa: CpaClient,
    *,
    apply: bool = False,
    strategy: Optional[str] = None,
    set_priority: bool = False,
) -> List[Dict[str, Any]]:
    """Diff ledger vs CPA pool and (optionally) push scheduling decisions.

    Decisions pushed when apply=True:
    - disable/enable credentials per ledger exhaustion state
    - priority fields per marginal-cost ordering (fill-first consumes the
      cheapest-first subscription quota before touching pay-as-you-go)
    - the routing strategy itself (default fill-first for cost layering)
    """
    ledger.apply_due_resets(db)
    entries = ledger.list_entries(db)
    files = cpa.list_auth_files()
    pairs = match_credentials(entries, files)
    priorities = compute_priorities(entries, files)

    plan: List[Dict[str, Any]] = []
    for pair in pairs:
        f = pair["file"]
        action, reason = decide(pair["entries"], f)
        name = _cred_identity(f) or (f.get("name") or "?")
        record = {
            "name": name,
            "provider": f.get("provider") or "-",
            "cpa_status": f.get("status") or "-",
            "disabled": bool(f.get("disabled")),
            "ledger_entries": [e["id"] for e in pair["entries"]],
            "priority": priorities.get(name),
            "decision": action,
            "reason": reason,
            "applied": False,
            "priority_applied": False,
        }
        if apply and action == DECISION_DISABLE:
            cpa.set_auth_disabled(name, True)
            record["applied"] = True
        elif apply and action == DECISION_ENABLE:
            cpa.set_auth_disabled(name, False)
            auth_index = f.get("auth_index")
            if auth_index:
                cpa.reset_quota(str(auth_index))
            record["applied"] = True
        if apply and set_priority and record["priority"] is not None:
            try:
                cpa.set_auth_fields(name, priority=record["priority"])
                record["priority_applied"] = True
            except Exception:
                # older CPA builds may ignore/reject priority on OAuth
                # credentials; the disable/enable plan still stands
                record["priority_applied"] = False
        plan.append(record)
    if apply and strategy:
        cpa.set_strategy(strategy)
    return plan


def sync_all(db: Database, account_label: str = "auto") -> Dict[str, Any]:
    """Silently poll every collector with usable credentials and upsert readings.

    Used by the daemon; the interactive `quota sync` command renders its own
    table instead.
    """
    from .collectors import COLLECTORS, CollectorError

    synced = 0
    errors: List[str] = []
    for agent, mod in COLLECTORS.items():
        creds = mod.detect_credentials()
        if creds is None:
            continue
        try:
            readings = mod.fetch_readings(creds)
        except CollectorError as exc:
            errors.append(f"{agent}: {exc}")
            continue
        except Exception as exc:  # a broken collector must not kill the loop
            errors.append(f"{agent}: {type(exc).__name__}: {exc}")
            continue
        for r in readings:
            ledger.upsert_reading(db, r, account_label=account_label)
            synced += 1
    return {"synced": synced, "errors": errors}


def pull_usage(
    db: Database,
    cpa: CpaClient,
    *,
    count: int = 100,
    provider_agent_hint: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Drain CPA per-request usage records into ledger usage_events.

    Mapping: record.provider -> ledger agent (via hint or AGENT_TO_CPA_PROVIDERS
    reversed), record.source (email) -> ledger account. Unmapped records are
    counted and skipped (never silently dropped).
    """
    records = cpa.drain_usage_queue(count)
    reverse: Dict[str, str] = {}
    for agent, providers in AGENT_TO_CPA_PROVIDERS.items():
        for p in providers:
            reverse.setdefault(p, agent)
    if provider_agent_hint:
        reverse.update(provider_agent_hint)

    consumed = 0
    skipped: List[str] = []
    total_tokens = 0.0
    for rec in records:
        provider = (rec.get("provider") or "").lower()
        agent = reverse.get(provider)
        tokens = rec.get("tokens") or {}
        amount = tokens.get("total_tokens")
        if not agent or not isinstance(amount, (int, float)) or amount <= 0:
            skipped.append(rec.get("request_id") or provider or "?")
            continue
        source = (rec.get("source") or "").lower()
        row = None
        candidates = db.conn.execute(
            "SELECT * FROM quota_entries WHERE agent = ? ORDER BY id", (agent,)
        ).fetchall()
        for cand in candidates:
            account = (cand["account"] or "").lower()
            if source and account and (account in source or source in account):
                row = cand
                break
        if row is None and len(candidates) == 1:
            row = candidates[0]
        if row is None:
            skipped.append(f"{agent}/{source or 'unknown'}")
            continue
        try:
            ledger.consume(
                db, row["id"], amount,
                input_tokens=tokens.get("input_tokens"),
                output_tokens=tokens.get("output_tokens"),
                cached_tokens=tokens.get("cached_tokens"),
                task_ref=rec.get("request_id"),
                note=f"cpa {provider} {rec.get('model') or ''}".strip(),
                force=True,  # fact-backfill: already consumed upstream
            )
            consumed += 1
            total_tokens += amount
        except ledger.LedgerError:
            skipped.append(f"{agent}#{row['id']}")
    return {"records": len(records), "consumed": consumed,
            "tokens": total_tokens, "skipped": skipped}
