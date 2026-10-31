"""Account-isolated snapshots and one eligibility policy for all callers."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import ledger, profiles
from .collectors.base import CollectorError
from .db import Database


def register(db: Database, config: Dict[str, Any]) -> None:
    """Project non-secret configuration without reassigning existing ownership."""
    with db.conn:
        db.conn.execute("BEGIN IMMEDIATE")
        for pool in config["pools"].values():
            identities = db.conn.execute("SELECT agent, subject FROM quota_pools WHERE account_id=?",
                                         (pool["account_id"],)).fetchall()
            if any((r["agent"], r["subject"]) != (pool["agent"], pool["subject"]) for r in identities):
                raise CollectorError("account ID is already registered to another provider identity")
            other = db.conn.execute("SELECT 1 FROM quota_pools WHERE agent=? AND subject=? AND account_id<>? LIMIT 1",
                                    (pool["agent"], pool["subject"], pool["account_id"])).fetchone()
            if other:
                raise CollectorError("provider identity is already registered under another account ID")
            old = db.conn.execute("SELECT * FROM quota_pools WHERE id = ?", (pool["id"],)).fetchone()
            if old and any(old[k] != pool[k] for k in ("agent", "account_id", "subject", "kind", "unit")):
                raise CollectorError("pool ownership, funding kind or unit changed; use a new pool ID")
            windows = json.dumps(pool["windows"], sort_keys=True)
            if pool.get("expires_at"):
                ledger.parse_dt(pool["expires_at"])
            last = old["last_batch_at"] if old and old["windows"] == windows else None
            db.conn.execute("""INSERT INTO quota_pools
                (id, agent, account_id, account_label, subject, kind, capability_tier,
                 unit, cost_per_unit, expires_at, windows, max_age_seconds, last_batch_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET account_label=excluded.account_label,
                    capability_tier=excluded.capability_tier, cost_per_unit=excluded.cost_per_unit,
                    expires_at=excluded.expires_at, windows=excluded.windows,
                    max_age_seconds=excluded.max_age_seconds, last_batch_at=excluded.last_batch_at""",
                (pool["id"], pool["agent"], pool["account_id"], pool["account_label"],
                 pool["subject"], pool["kind"], pool["capability_tier"], pool["unit"],
                 pool["cost_per_unit"], pool.get("expires_at"), windows, pool["max_age_seconds"], last))


def apply_snapshot(db: Database, config: Dict[str, Any], account_id: str,
                   readings: List[Any], observed_at: datetime) -> Dict[str, Any]:
    """All configured pools of one account update atomically; a late batch is ignored."""
    selected = [p for p in config["pools"].values() if p["account_id"] == account_id]
    by_key = {}
    for r in readings:
        if r.window_key in by_key:
            raise CollectorError("duplicate window in collector batch")
        if any(v is not None and (not isinstance(v, (float, int)) or not math.isfinite(v))
               for v in (r.used_percent, r.total_abs, r.remaining_abs, r.consumed_abs)):
            raise CollectorError("invalid numeric quota reading")
        if r.used_percent is not None and not 0 <= r.used_percent <= 100:
            raise CollectorError("invalid percentage reading")
        by_key[r.window_key] = r
    stamp = observed_at.astimezone(timezone.utc).isoformat(timespec="microseconds")
    with db.conn:
        db.conn.execute("BEGIN IMMEDIATE")
        stored = [db.conn.execute("SELECT * FROM quota_pools WHERE id=?", (p["id"],)).fetchone()
                  for p in selected]
        if any(not row or any(row[k] != p[k] for k in ("agent", "account_id", "subject"))
               or row["windows"] != json.dumps(p["windows"], sort_keys=True)
               for row, p in zip(stored, selected)):
            raise CollectorError("pool registration changed while collecting; discard batch")
        if any(p and p["last_attempt_at"] and ledger.parse_dt(p["last_attempt_at"]) >= observed_at
               for p in stored):
            return {"updated": 0, "ignored": True}
        missing = [w["key"] for p in selected for w in p["windows"]
                   if w["role"] == "limit" and w["key"] not in by_key]
        if missing:
            for pool in selected:
                db.conn.execute("UPDATE quota_pools SET last_batch_at=NULL, last_attempt_at=? WHERE id=?",
                                (stamp, pool["id"]))
            return {"updated": 0, "incomplete": True}
        updated = 0
        for pool in selected:
            for window in pool["windows"]:
                r = by_key.get(window["key"])
                if r is None:
                    continue
                if r.agent != pool["agent"]:
                    raise CollectorError("collector returned another agent's reading")
                remaining = r.remaining_abs
                if remaining is None and r.total_abs is not None:
                    if r.used_percent is not None:
                        remaining = r.total_abs * (1 - r.used_percent / 100)
                    elif r.consumed_abs is not None:
                        remaining = max(0, r.total_abs - r.consumed_abs)
                db.conn.execute("""INSERT INTO quota_entries
                    (agent, account, kind, capability_tier, unit, total, remaining,
                     cost_per_unit, expires_at, window_key, window_seconds, used_percent,
                     last_synced_at, created_at, updated_at, pool_id, scope, role,
                     source_kind, resets_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'snapshot', ?)
                    ON CONFLICT(pool_id, scope, window_key) WHERE pool_id IS NOT NULL
                    DO UPDATE SET account=excluded.account, unit=excluded.unit, total=excluded.total,
                        remaining=excluded.remaining, used_percent=excluded.used_percent,
                        last_synced_at=excluded.last_synced_at, updated_at=excluded.updated_at,
                        window_seconds=excluded.window_seconds, resets_at=excluded.resets_at,
                        role=excluded.role, source_kind='snapshot'""",
                    (pool["agent"], pool["account_label"], pool["kind"], pool["capability_tier"],
                     r.unit, r.total_abs, remaining, pool["cost_per_unit"], pool.get("expires_at"),
                     r.window_key, r.window_seconds, r.used_percent, stamp, stamp, stamp,
                     pool["id"], window["scope"], window["role"],
                     ledger.iso(r.resets_at) if r.resets_at else None))
                updated += 1
            db.conn.execute("UPDATE quota_pools SET last_batch_at=?, last_attempt_at=? WHERE id=?",
                            (stamp, stamp, pool["id"]))
    return {"updated": updated, "ignored": False}


def sync_accounts(db: Database, *, apply: bool, agent: Optional[str] = None,
                  account_id: Optional[str] = None, deadline: Optional[float] = None) -> Dict[str, Any]:
    from .collectors import COLLECTORS

    config = profiles.load()
    selected_accounts = {aid: account for aid, account in config["accounts"].items()
                         if (not agent or account["agent"] == agent)
                         and (not account_id or aid == account_id)}
    if (agent or account_id) and not selected_accounts:
        raise CollectorError("no configured account matches the requested account/agent")
    if apply:
        register(db, dict(config, pools={pid: p for pid, p in config["pools"].items()
                                         if p["account_id"] in selected_accounts}))
    rows, errors = [], []
    for aid, account in selected_accounts.items():
        if account['agent'] == 'openai_api':
            # A project API key does not provide subscription quota/balance windows.
            continue
        try:
            collector = COLLECTORS[account["agent"]]
            creds = profiles.credentials(account, collector)
            observed_at = ledger.now_utc()  # capture before polling, for out-of-order responses
            if deadline is not None and account['agent'] == 'codex':
                creds['_deadline'] = deadline
            readings = collector.fetch_readings(creds)
            profiles.credentials(account, collector)  # local login may have changed while polling
            outcome = apply_snapshot(db, config, aid, readings, observed_at) if apply else {}
            if outcome.get("incomplete"):
                errors.append(f"{aid}: required window missing; pool is unknown")
            rows.append({"account_id": aid, "agent": account["agent"],
                         "readings": readings, "outcome": outcome})
        except Exception as exc:
            # A failing provider/account must not prevent the others from syncing.
            errors.append(f"{aid}: {str(exc) if isinstance(exc, CollectorError) else 'collector failed (' + type(exc).__name__ + ')'}")
    return {"rows": rows, "errors": errors,
            "synced": sum(r["outcome"].get("updated", 0) for r in rows)}


def bind_entry(db: Database, config: Dict[str, Any], entry_id: int,
               pool_id: str, window_key: str, scope: str = "*") -> None:
    pool = config["pools"].get(pool_id)
    if not pool:
        raise ledger.LedgerError("unknown configured pool")
    window = next((w for w in pool["windows"] if (w["key"], w["scope"]) == (window_key, scope)), None)
    row = db.conn.execute("SELECT * FROM quota_entries WHERE id=?", (entry_id,)).fetchone()
    if not row or not window or row["agent"] != pool["agent"] or row["kind"] != pool["kind"]:
        raise ledger.LedgerError("entry, pool kind/agent or window does not match")
    if row["pool_id"] and row["pool_id"] != pool_id:
        raise ledger.LedgerError("entry is already owned by another pool")
    if row["unit"] != pool["unit"]:
        raise ledger.LedgerError("manual entry unit must match pool unit")
    if row["cost_per_unit"] != pool["cost_per_unit"]:
        raise ledger.LedgerError("manual entry marginal price must match pool price")
    register(db, dict(config, pools={pool_id: pool}))
    with db.conn:
        if row["last_synced_at"] or row["source_kind"] == "snapshot":
            # Old auto snapshots cannot become a verified fresh observation through binding.
            source_kind = "snapshot"
        else:
            source_kind = "manual"
        db.conn.execute("""UPDATE quota_entries SET pool_id=?, scope=?, role=?,
            window_key=?, source_kind=?, last_synced_at=NULL WHERE id=?""",
            (pool_id, scope, window["role"], window_key, source_kind, entry_id))


def evaluate(db: Database, pool: Dict[str, Any], model: str, *, amount: float = 0,
             unit: str = "tokens", now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or ledger.now_utc()
    reasons, blocked, unknown = [], False, False
    if pool.get("expires_at") and ledger.parse_dt(pool["expires_at"]) <= now:
        blocked = True
        reasons.append("pool expired")
    stored = db.conn.execute("SELECT * FROM quota_pools WHERE id=?", (pool["id"],)).fetchone()
    if not stored or any(stored[k] != pool[k] for k in ("agent", "account_id", "subject", "kind", "unit")):
        return {"state": "unknown", "why": "pool is not registered with this identity; sync --apply first"}
    if stored["windows"] != json.dumps(pool["windows"], sort_keys=True):
        return {"state": "unknown", "why": "window configuration changed; sync --apply first"}
    rows = {(r["scope"], r["window_key"]): dict(r) for r in db.conn.execute(
        "SELECT * FROM quota_entries WHERE pool_id=?", (pool["id"],))}
    limits = [w for w in pool["windows"] if w["role"] == "limit" and w["scope"] in ("*", model)]
    if pool["kind"] != "payg" and not limits:
        unknown = True
        reasons.append("no applicable limit windows")
    remaining_values, coverage_known = [], True
    for window in limits:
        key = (window["scope"], window["key"])
        row = rows.get(key)
        if not row:
            unknown = True
            reasons.append(f"{window['key']}: missing")
            continue
        if row["expires_at"] and ledger.parse_dt(row["expires_at"]) <= now:
            blocked = True
            reasons.append(f"{window['key']}: expired")
            continue
        if row["source_kind"] == "snapshot":
            observed = row["last_synced_at"]
            if (not observed or observed != stored["last_batch_at"]
                    or not 0 <= (now - ledger.parse_dt(observed)).total_seconds() <= pool["max_age_seconds"]
                    or (row["resets_at"] and ledger.parse_dt(row["resets_at"]) <= now)):
                unknown = True
                reasons.append(f"{window['key']}: stale or awaiting reset confirmation")
                continue
        elif row["source_kind"] != "manual":
            unknown = True
            reasons.append(f"{window['key']}: unverified legacy row")
            continue
        value, percent = row["remaining"], row["used_percent"]
        if ((value is not None and value <= 0) or (percent is not None and percent >= 100)):
            blocked = True
            reasons.append(f"{window['key']}: depleted")
        elif value is None and percent is None:
            unknown = True
            reasons.append(f"{window['key']}: no balance evidence")
        if row["unit"] == unit and value is not None:
            remaining_values.append(value)
            if amount and value < amount:
                blocked = True
                reasons.append(f"{window['key']}: insufficient {unit}")
        else:
            coverage_known = False
    state = "blocked" if blocked else "unknown" if unknown else "ready"
    if pool["kind"] == "payg":
        est = amount * pool["cost_per_unit"] if unit == pool["unit"] and pool.get('billing') != 'provider' else None
    else:
        est = 0.0
    return {"state": state, "why": "; ".join(reasons) or "all applicable limits available",
            "remaining": min(remaining_values) if remaining_values and coverage_known else None,
            "coverage": "metered" if pool["kind"] == "payg" and not limits else
                        "known" if coverage_known and remaining_values else "unknown",
            "est": est}


def plan_route(db: Database, amount: float, unit: str, min_tier: int,
               strategy: str, config: Optional[Dict[str, Any]] = None,
               required_group: Optional[str] = None) -> Dict[str, Any]:
    from .executors import channel_status

    if not math.isfinite(amount) or amount <= 0 or not 1 <= min_tier <= 5:
        raise ledger.LedgerError("positive finite amount and tier 1-5 required")
    if strategy not in ledger.STRATEGIES:
        raise ledger.LedgerError("unknown routing strategy")
    config = config if config is not None else profiles.load()
    ledger.apply_due_resets(db)
    candidates, skipped = [], []
    for pool in config["pools"].values():
        base = dict(pool, pool_id=pool["id"], account=pool["account_label"], status="active")
        channels = [c for c in config["channels"] if c["pool_id"] == pool["id"]]
        if not channels:
            skipped.append(dict(base, why="no account-bound execution channel"))
        for channel in channels:
            verdict = evaluate(db, pool, channel["model"], amount=amount, unit=unit)
            ready, reason = channel_status(channel, pool, config)
            why = None
            tier = channel.get("capability_tier", pool["capability_tier"])
            group = channel.get("group")
            if required_group and (group not in ("regular", "primary") or "capability_tier" not in channel):
                why = "automatic routing requires explicit channel group and capability tier"
            elif required_group == "primary" and group != "primary":
                why = "task requires primary model group"
            elif tier < min_tier:
                why = "capability below required tier"
            elif verdict["state"] != "ready":
                why = verdict["why"]
            elif not ready:
                why = reason
            item = dict(base, **verdict, channel=channel, channel_id=channel["id"],
                        model=channel["model"], covers=True, task_amount=amount, task_unit=unit)
            item.update(capability_tier=tier, group=group, pool_config=dict(pool))
            if why:
                skipped.append(dict(item, why=why))
            else:
                candidates.append(item)
    for row in ledger.list_entries(db):
        if row["pool_id"] is None:
            skipped.append(dict(row, why="legacy entry needs explicit account/pool binding"))

    def key(c: Dict[str, Any]) -> Any:
        balance = -(c["remaining"] or 0) if c["coverage"] == "known" else 0
        inner = balance
        if c["kind"] == "credit_pack":
            inner = ledger.parse_dt(c["expires_at"]).timestamp() if c.get("expires_at") else float("inf")
        elif c["kind"] == "payg":
            inner = c["cost_per_unit"]
        cost = (ledger.KIND_ORDER[c["kind"]],
                inner,
                c["pool_id"], c["channel_id"])
        if required_group == "regular":
            # Prefer regular before balance within subscriptions, but never prefer
            # a paid regular model over an eligible already-paid primary model.
            price = c["cost_per_unit"] if c["kind"] == "payg" else 0
            cost = (ledger.KIND_ORDER[c["kind"]], price,
                    0 if c.get("group") == "regular" else 1, inner,
                    c["pool_id"], c["channel_id"])
        return (-c["capability_tier"], cost) if strategy == "capability" else cost

    candidates.sort(key=key)
    prices = [c["cost_per_unit"] for c in candidates if c["kind"] == "payg" and c["unit"] == unit]
    baseline = amount * min(prices) if prices else None
    routed = candidates[0]["est"] if candidates else None
    return {"amount": amount, "unit": unit, "min_tier": min_tier,
            "candidates": candidates, "skipped": skipped,
            "primary_idx": 0 if candidates else None, "routed_cost": routed,
            "payg_only_cost": baseline,
            "saved": baseline - routed if baseline is not None and routed is not None else None}
