"""Exact CPA bindings, owned pauses and idempotent receipts; quota snapshots stay authoritative."""

from __future__ import annotations

import hashlib
import json
import math

from typing import Any, Dict, List, Optional, Tuple

from . import ledger, profiles, pools
from .cpa import CpaClient
from .collectors.base import CollectorError
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
    # openai-compatibility upstreams are recorded under their registered name
    "doubao": ["doubao", "ark", "volcengine"],
}

DECISION_ENABLE = "enable"
DECISION_DISABLE = "disable"
DECISION_KEEP = "keep"
DECISION_UNTRACKED = "untracked"


def reconcile(db: Database, cpa: CpaClient, *, apply: bool = False,
              strategy: Optional[str] = None, set_priority: bool = False) -> List[Dict[str, Any]]:
    """Only exact, verified and exclusively managed bindings may be paused/resumed.

    Global strategy/priority writes are intentionally withheld until the deployed
    CPA contract has been verified. Model-scoped limits never disable an account.
    """
    config = profiles.load()
    ledger.apply_due_resets(db)
    plan = []
    for f in cpa.list_auth_files():
        name, provider = f.get("name"), (f.get("provider") or "").lower()
        owners = [a for a in config["accounts"].values()
                  if a.get("cpa", {}).get("auth_name") == name
                  and a.get("cpa", {}).get("provider") == provider]
        record = {"name": name or "?", "provider": provider or "-",
                  "cpa_status": f.get("status") or "-", "disabled": bool(f.get("disabled")),
                  "ledger_entries": [], "priority": None, "decision": DECISION_UNTRACKED,
                  "reason": "no unique explicit CPA binding", "applied": False,
                  "priority_applied": False, "strategy_applied": False}
        plan.append(record)
        if len(owners) != 1:
            continue
        account = owners[0]
        binding = account["cpa"]
        subject_field = binding.get("subject_field")
        try:
            identity_ok = subject_field and str(profiles.field(f, subject_field)) == str(
                account["source"]["expected_subject"])
        except CollectorError:
            identity_ok = False
        if not binding.get("exclusive") or not identity_ok:
            record["reason"] = "CPA identity or complete funding coverage is unverified"
            continue
        owned = [p for p in config["pools"].values() if p["account_id"] == account["id"]]
        record["ledger_entries"] = [r[0] for p in owned for r in db.conn.execute(
            "SELECT id FROM quota_entries WHERE pool_id=?", (p["id"],))]
        verdicts = [pools.evaluate(db, p, "*") for p in owned]
        all_blocked = bool(owned) and all(v["state"] == "blocked" for v in verdicts)
        scoped_models = {w["scope"] for p in owned for w in p["windows"] if w["scope"] != "*"}
        models = scoped_models | {c["model"] for c in config["channels"]
                                  if c["account_id"] == account["id"]}
        if not scoped_models:
            models.add("*")
        ready = any(pools.evaluate(db, p, model)["state"] == "ready" for p in owned for model in models)
        pause_key = hashlib.sha256(cpa.base_url.encode()).hexdigest() + ":" + name
        stamp = json.dumps([account["id"], account["agent"], account["source"]["expected_subject"], name])
        pause = db.conn.execute("SELECT binding FROM cpa_pauses WHERE name=?", (pause_key,)).fetchone()
        ours = bool(pause and pause[0] == stamp)
        record.update(decision=DECISION_KEEP, reason="quota unknown or model-scoped; keep credential state")
        if all_blocked and not record["disabled"]:
            record.update(decision=DECISION_DISABLE, reason="all funding pools blocked by common limits")
            if apply:
                cpa.set_auth_disabled(name, True)
                with db.conn:
                    db.conn.execute("INSERT OR REPLACE INTO cpa_pauses VALUES (?, ?)", (pause_key, stamp))
                record["applied"] = True
        elif record["disabled"] and ours and ready:
            record.update(decision=DECISION_ENABLE, reason="qpool pause recovered with fresh quota evidence")
            if apply:
                cpa.set_auth_disabled(name, False)
                with db.conn:
                    db.conn.execute("DELETE FROM cpa_pauses WHERE name=?", (pause_key,))
                record["applied"] = True
        elif record["disabled"] and not ours:
            record["reason"] = "disabled outside qpool; automatic recovery is forbidden"
        elif ready:
            record["reason"] = "available; global priority/strategy contract remains unverified"
    return plan


def sync_all(db: Database, account_label: str = "auto") -> Dict[str, Any]:
    """Daemon writes only accounts with explicit, checked identities."""
    result = pools.sync_accounts(db, apply=True)
    return {"synced": result["synced"], "errors": result["errors"]}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def pull_usage(db: Database, cpa: CpaClient, *, count: int = 100,
               provider_agent_hint: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Persist receipts once; exact source attribution only. Never mutate snapshots.

    CPA's destructive dequeue has no local zero-loss guarantee. Once committed,
    request IDs are idempotent; unmatched receipts remain queryable as pending.
    """
    config = profiles.load()
    records = cpa.drain_usage_queue(count)
    namespace = _hash(cpa.base_url)
    reverse = {p: a for a, providers in AGENT_TO_CPA_PROVIDERS.items() for p in providers}
    reverse.update(provider_agent_hint or {})
    consumed, total_tokens, skipped = 0, 0.0, []
    for rec in records:
        rec = rec if isinstance(rec, dict) else {}
        provider = rec.get("provider")
        provider = provider.lower()[:256] if isinstance(provider, str) else ""
        # openai-compatibility upstreams are recorded as "openai-compatible-<name>"
        if provider.startswith("openai-compatible-"):
            provider = provider[len("openai-compatible-"):]
        # fall back to the raw provider name: custom upstreams registered under
        # their own name (e.g. maas-gpt) map to the same-named ledger agent,
        # mirroring _providers_for's default
        agent = reverse.get(provider) or provider
        source_hash = _hash(str(rec.get("source") or ""))
        model = str(rec.get("model") or "")[:256]
        rid = rec.get("request_id")
        rid = rid if isinstance(rid, str) and 0 < len(rid) <= 256 else None
        tokens = rec.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        amount = tokens.get("total_tokens")
        if (isinstance(amount, bool) or not isinstance(amount, (int, float))
                or not math.isfinite(amount) or amount < 0):
            amount = None
        owners = [a for a in config["accounts"].values()
                  if a["agent"] == agent and a.get("cpa", {}).get("exclusive")
                  and a.get("cpa", {}).get("usage_source")
                  and _hash(str(a["cpa"]["usage_source"])) == source_hash]
        matched_pool, account_id = None, None
        reason = "missing request ID or usage" if rid is None or amount is None else "no exact account/funding attribution"
        if rid and amount is not None and len(owners) == 1:
            account_id = owners[0]["id"]
            owned = [p for p in config["pools"].values() if p["account_id"] == account_id]
            if len(owned) == 1:
                matched_pool = owned[0]
        if matched_pool:
            registered = db.conn.execute("SELECT * FROM quota_pools WHERE id=?", (matched_pool["id"],)).fetchone()
            if not registered or registered["account_id"] != account_id or registered["subject"] != matched_pool["subject"]:
                matched_pool = None
                reason = "account/pool registration is missing or changed"
        cost = None
        if matched_pool:
            if matched_pool["kind"] != "payg":
                cost = 0.0
            elif matched_pool["unit"] == "tokens":
                cost = amount * matched_pool["cost_per_unit"]
                if not math.isfinite(cost):
                    cost = None
        status = "matched" if matched_pool else "pending"
        now = ledger.iso(ledger.now_utc())
        occurred = rec.get("timestamp")
        try:
            occurred = ledger.iso(ledger.parse_dt(occurred)) if isinstance(occurred, str) else now
        except ledger.LedgerError:
            occurred = now
        counts = []
        for key in ("input_tokens", "output_tokens", "cached_tokens"):
            val = tokens.get(key)
            counts.append(val if isinstance(val, int) and not isinstance(val, bool)
                          and 0 <= val <= 2**63 - 1 else None)
        with db.conn:
            db.conn.execute("BEGIN IMMEDIATE")
            previous = db.conn.execute("SELECT * FROM imported_usage WHERE namespace=? AND request_id=?",
                                       (namespace, rid)).fetchone() if rid else None
            if previous:
                if (previous["provider"], previous["source"], previous["model"], previous["amount"]) != (
                        provider, source_hash, model, amount):
                    skipped.append("conflicting receipt; original retained")
                continue
            db.conn.execute("""INSERT INTO imported_usage
                (namespace, request_id, provider, source, model, pool_id, account_id, amount,
                 input_tokens, output_tokens, cached_tokens, est_cost_usd, status, reason,
                 occurred_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (namespace, rid, provider, source_hash, model, matched_pool["id"] if matched_pool else None,
                 account_id, amount, *counts, cost, status, None if matched_pool else reason, occurred, now))
        if matched_pool:
            consumed += 1
            total_tokens += amount
        else:
            skipped.append(reason)
    return {"records": len(records), "consumed": consumed, "tokens": total_tokens, "skipped": skipped}
