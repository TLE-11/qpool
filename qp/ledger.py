"""Quota ledger business logic.

Borrowed ideas:
- onWatch tracker: reset-cycle detection -> simplified to lazy time-based rollover
  (no background poller needed for a manual ledger; we roll over on read/write).
- oh-my-codex dispatch: strict state checks before mutation (no overdraw,
  no consuming expired entries) + append-only event log.
- claude-code-router: marginal-cost route order as a sort key
  (subscription -> expiring credit packs -> payg), dead entries last.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .db import Database

KINDS = ("subscription", "credit_pack", "payg")
RESET_PERIODS = ("daily", "weekly", "monthly")
EXPIRING_SOON_DAYS = 7

DEFAULT_UNIT_BY_KIND = {
    "subscription": "requests",
    "credit_pack": "tokens",
    "payg": "tokens",
}

# Marginal-cost routing tiers: already-paid subscription (cost ~ 0) first,
# then expiring prepaid credit packs, real-money payg last.
KIND_ORDER = {"subscription": 0, "credit_pack": 1, "payg": 2}

STATUS_ACTIVE = "active"
STATUS_EXPIRING = "expiring<=7d"
STATUS_EXPIRED = "expired"
STATUS_DEPLETED = "depleted"


class LedgerError(Exception):
    """User-facing ledger error."""


# ---------- time helpers ----------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_dt(value: str) -> datetime:
    """Parse 'YYYY-MM-DD' (treated as end of day, local time) or full ISO 8601."""
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LedgerError(f"invalid datetime {value!r}: use YYYY-MM-DD or ISO 8601") from exc
    if len(text) == 10:  # date only -> end of day
        dt = dt.replace(hour=23, minute=59, second=59)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # interpret as local time
    return dt.astimezone(timezone.utc)


def _add_period(start: datetime, period: str) -> datetime:
    if period == "daily":
        return start + timedelta(days=1)
    if period == "weekly":
        return start + timedelta(weeks=1)
    if period == "monthly":
        year = start.year + start.month // 12
        month = start.month % 12 + 1
        day = min(start.day, calendar.monthrange(year, month)[1])
        return start.replace(year=year, month=month, day=day)
    raise LedgerError(f"unknown reset period: {period}")


def _plain(x: float) -> str:
    return str(int(x)) if float(x) == int(x) else f"{x:.2f}"


# ---------- status & rollover ----------

def entry_status(entry: Any, now: datetime) -> str:
    exp_raw = entry["expires_at"]
    if exp_raw and parse_dt(exp_raw) <= now:
        return STATUS_EXPIRED
    remaining = entry["remaining"]
    if remaining is not None and remaining <= 0:
        return STATUS_DEPLETED
    if exp_raw and parse_dt(exp_raw) <= now + timedelta(days=EXPIRING_SOON_DAYS):
        return STATUS_EXPIRING
    return STATUS_ACTIVE


def _next_reset_start(entry: Any, now: datetime) -> Optional[datetime]:
    """New period_start if the subscription billing period has rolled over."""
    if entry["kind"] != "subscription" or not entry["period_start"]:
        return None
    start = parse_dt(entry["period_start"])
    window_seconds = entry["window_seconds"]
    if window_seconds:
        # API-synced rolling windows (codex 5h, claude 7d, ...) reset by seconds
        boundary = start + timedelta(seconds=window_seconds)
        if now < boundary:
            return None
        while boundary <= now:
            start = boundary
            boundary = start + timedelta(seconds=window_seconds)
        return start
    if not entry["reset_period"]:
        return None
    boundary = _add_period(start, entry["reset_period"])
    if now < boundary:
        return None
    # catch up over any missed cycles
    while boundary <= now:
        start = boundary
        boundary = _add_period(start, entry["reset_period"])
    return start


def apply_due_resets(db: Database) -> int:
    """Lazily reset subscription quotas whose billing period has passed."""
    now = now_utc()
    rows = db.conn.execute(
        "SELECT * FROM quota_entries WHERE kind = 'subscription' "
        "AND (reset_period IS NOT NULL OR window_seconds IS NOT NULL)"
    ).fetchall()
    reset = 0
    for row in rows:
        new_start = _next_reset_start(row, now)
        if new_start is None:
            continue
        if row["window_seconds"]:
            # rolling-window entries also clear the last API reading
            db.conn.execute(
                "UPDATE quota_entries SET remaining = total, period_start = ?, "
                "used_percent = 0, updated_at = ? WHERE id = ?",
                (iso(new_start), iso(now), row["id"]),
            )
        else:
            db.conn.execute(
                "UPDATE quota_entries SET remaining = total, period_start = ?, updated_at = ? WHERE id = ?",
                (iso(new_start), iso(now), row["id"]),
            )
        reset += 1
    if reset:
        db.conn.commit()
    return reset


# ---------- commands ----------

def add_entry(
    db: Database,
    *,
    agent: str,
    account: str,
    kind: str,
    capability_tier: int = 3,
    unit: Optional[str] = None,
    total: Optional[float] = None,
    remaining: Optional[float] = None,
    cost_per_unit: float = 0.0,
    expires_at: Optional[str] = None,
    reset_period: Optional[str] = None,
    notes: Optional[str] = None,
) -> int:
    agent = agent.strip().lower()
    account = account.strip()
    if not agent or not account:
        raise LedgerError("agent and account must be non-empty")
    if kind not in KINDS:
        raise LedgerError(f"kind must be one of: {', '.join(KINDS)}")
    if not 1 <= capability_tier <= 5:
        raise LedgerError("capability tier must be between 1 and 5")
    if reset_period and reset_period not in RESET_PERIODS:
        raise LedgerError(f"reset period must be one of: {', '.join(RESET_PERIODS)}")
    if total is not None and total <= 0:
        raise LedgerError("total must be > 0")
    if cost_per_unit < 0:
        raise LedgerError("cost per unit must be >= 0")

    if kind == "subscription":
        if total is None:
            raise LedgerError("subscription requires --total (quota per billing period)")
        if not reset_period:
            raise LedgerError("subscription requires --reset (daily/weekly/monthly)")
        if expires_at:
            raise LedgerError("subscriptions renew automatically; --expires only applies to credit packs")
    elif kind == "credit_pack":
        if total is None:
            raise LedgerError("credit pack requires --total")
    else:  # payg
        if cost_per_unit <= 0:
            raise LedgerError("pay-as-you-go requires --cost-per-unit > 0")
        if total is not None:
            raise LedgerError(
                "pay-as-you-go has no fixed quota; for prepaid balance use "
                "--kind credit_pack --unit usd --total <amount>"
            )

    if remaining is not None:
        if remaining < 0 or (total is not None and remaining > total):
            raise LedgerError("remaining must be between 0 and total")
    elif total is not None:
        remaining = total

    exp_iso = iso(parse_dt(expires_at)) if expires_at else None
    now = now_utc()
    unit = unit or DEFAULT_UNIT_BY_KIND[kind]
    period_start = iso(now) if kind == "subscription" else None
    cur = db.conn.execute(
        """INSERT INTO quota_entries
           (agent, account, kind, capability_tier, unit, total, remaining,
            cost_per_unit, expires_at, reset_period, period_start, notes, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (agent, account, kind, capability_tier, unit, total, remaining,
         cost_per_unit, exp_iso, reset_period, period_start, notes, iso(now), iso(now)),
    )
    db.conn.commit()
    return int(cur.lastrowid)


def list_entries(
    db: Database,
    *,
    agent: Optional[str] = None,
    account: Optional[str] = None,
    kind: Optional[str] = None,
) -> List[Dict[str, Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if agent:
        clauses.append("agent = ?")
        params.append(agent.strip().lower())
    if account:
        clauses.append("account = ?")
        params.append(account.strip())
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    sql = "SELECT * FROM quota_entries"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY id"
    rows = db.conn.execute(sql, params).fetchall()
    now = now_utc()
    return [dict(r) | {"status": entry_status(r, now)} for r in rows]


def route_sort_key(entry: Dict[str, Any]) -> Any:
    """Marginal-cost route order: subscription -> expiring credit packs -> payg.

    Expired/depleted entries always sort last.
    """
    dead = 1 if entry["status"] in (STATUS_EXPIRED, STATUS_DEPLETED) else 0
    rank = KIND_ORDER[entry["kind"]]
    if entry["kind"] == "subscription":
        inner: Any = -(entry["remaining"] or 0.0)  # fuller quota first
    elif entry["kind"] == "credit_pack":
        inner = entry["expires_at"] or "9999"  # soonest expiring first
    else:
        inner = entry["cost_per_unit"]  # cheapest first
    return (dead, rank, inner, entry["id"])


def consume(
    db: Database,
    entry_id: int,
    amount: float,
    *,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    cached_tokens: Optional[int] = None,
    tool_calls: Optional[int] = None,
    task_ref: Optional[str] = None,
    note: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    if amount <= 0:
        raise LedgerError("amount must be > 0")
    apply_due_resets(db)
    row = db.conn.execute("SELECT * FROM quota_entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        raise LedgerError(f"quota entry #{entry_id} not found")
    now = now_utc()
    remaining = row["remaining"]
    if not force:
        # manual bookkeeping path: strict state checks (omx-style)
        status = entry_status(row, now)
        if status == STATUS_EXPIRED:
            raise LedgerError(f"entry #{entry_id} expired at {row['expires_at']}; cannot consume")
        if status == STATUS_DEPLETED:
            raise LedgerError(f"entry #{entry_id} is depleted")
        if remaining is not None and amount > remaining:
            raise LedgerError(
                f"insufficient quota on entry #{entry_id}: "
                f"{_plain(remaining)} {row['unit']} left, requested {_plain(amount)}"
            )
    # force=True (fact-backfill from the passthrough gateway): the consumption
    # already happened upstream, so we record it even past zero -- a negative
    # remaining is a truthful "overdrawn" signal.
    est_cost = amount * row["cost_per_unit"]
    with db.conn:  # single transaction: decrement + append event
        if remaining is not None:
            db.conn.execute(
                "UPDATE quota_entries SET remaining = remaining - ?, updated_at = ? WHERE id = ?",
                (amount, iso(now), entry_id),
            )
        else:
            db.conn.execute(
                "UPDATE quota_entries SET updated_at = ? WHERE id = ?", (iso(now), entry_id)
            )
        db.conn.execute(
            """INSERT INTO usage_events
               (entry_id, amount, input_tokens, output_tokens, cached_tokens,
                tool_calls, est_cost_usd, task_ref, note, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (entry_id, amount, input_tokens, output_tokens, cached_tokens,
             tool_calls, est_cost, task_ref, note, iso(now)),
        )
    new_remaining = None if remaining is None else remaining - amount
    return {"est_cost": est_cost, "remaining": new_remaining, "entry": dict(row)}


def remove_entry(db: Database, entry_id: int) -> int:
    row = db.conn.execute("SELECT id FROM quota_entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        raise LedgerError(f"quota entry #{entry_id} not found")
    events = db.conn.execute(
        "SELECT COUNT(*) AS c FROM usage_events WHERE entry_id = ?", (entry_id,)
    ).fetchone()["c"]
    with db.conn:
        db.conn.execute("DELETE FROM usage_events WHERE entry_id = ?", (entry_id,))
        db.conn.execute("DELETE FROM quota_entries WHERE id = ?", (entry_id,))
    return int(events)


def list_events(db: Database, *, entry_id: Optional[int] = None, limit: int = 50) -> List[Any]:
    sql = """SELECT e.*, q.agent, q.account, q.unit
             FROM usage_events e JOIN quota_entries q ON q.id = e.entry_id"""
    params: List[Any] = []
    if entry_id is not None:
        sql += " WHERE e.entry_id = ?"
        params.append(entry_id)
    sql += " ORDER BY e.id DESC LIMIT ?"
    params.append(limit)
    return db.conn.execute(sql, params).fetchall()


# ---------- routing simulation & reports ----------

def plan_route(db: Database, amount: float, unit: str, min_tier: int = 1) -> Dict[str, Any]:
    """Dry-run routing: capability hard filter -> marginal-cost order.

    Returns the failover chain plus the "three bills"
    (routed cost vs payg-only cost vs saved-by-pooling).
    """
    if amount <= 0:
        raise LedgerError("amount must be > 0")
    if not 1 <= min_tier <= 5:
        raise LedgerError("tier must be between 1 and 5")
    apply_due_resets(db)
    entries = list_entries(db)
    candidates: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    payg_prices: List[float] = []
    for e in entries:
        if e["unit"] != unit:
            skipped.append(dict(e) | {"why": f"unit mismatch ({e['unit']} != {unit})"})
            continue
        if e["capability_tier"] < min_tier:
            skipped.append(dict(e) | {"why": f"tier T{e['capability_tier']} < required T{min_tier}"})
            continue
        if e["kind"] == "payg" and e["cost_per_unit"] > 0:
            payg_prices.append(e["cost_per_unit"])
        if e["status"] in (STATUS_EXPIRED, STATUS_DEPLETED):
            skipped.append(dict(e) | {"why": e["status"]})
            continue
        remaining = e["remaining"]
        candidates.append(dict(e) | {
            "covers": remaining is None or remaining >= amount,
            "est": amount * e["cost_per_unit"],
        })
    candidates.sort(key=route_sort_key)
    primary_idx = next((i for i, c in enumerate(candidates) if c["covers"]), None)
    payg_only = amount * min(payg_prices) if payg_prices else None
    routed = candidates[primary_idx]["est"] if primary_idx is not None else None
    return {
        "amount": amount,
        "unit": unit,
        "min_tier": min_tier,
        "candidates": candidates,
        "primary_idx": primary_idx,
        "skipped": skipped,
        "routed_cost": routed,
        "payg_only_cost": payg_only,
        "saved": None if (routed is None or payg_only is None) else payg_only - routed,
    }


def monthly_report(db: Database, month: str) -> Dict[str, Any]:
    """Aggregate usage events for a month ('YYYY-MM'); compute pooled savings.

    Savings baseline: cheapest payg price per unit in the ledger. Subscription /
    credit-pack consumption is priced at that baseline to estimate what the same
    work would have cost on pure pay-as-you-go.
    """
    if len(month) != 7 or month[4] != "-":
        raise LedgerError("month must be YYYY-MM")
    rows = db.conn.execute(
        """SELECT q.agent, q.kind, q.unit,
                  SUM(e.amount) AS amount, SUM(e.est_cost_usd) AS cost, COUNT(*) AS n
           FROM usage_events e JOIN quota_entries q ON q.id = e.entry_id
           WHERE substr(e.created_at, 1, 7) = ?
           GROUP BY q.agent, q.kind, q.unit
           ORDER BY q.agent, q.kind""",
        (month,),
    ).fetchall()
    baselines = {
        r["unit"]: r["c"]
        for r in db.conn.execute(
            "SELECT unit, MIN(cost_per_unit) AS c FROM quota_entries "
            "WHERE kind = 'payg' AND cost_per_unit > 0 GROUP BY unit"
        )
    }
    spent = sum(r["cost"] or 0.0 for r in rows)
    pooled_value = 0.0  # payg-equivalent value of subscription / credit-pack usage
    for r in rows:
        if r["kind"] == "payg":
            continue
        base = baselines.get(r["unit"])
        if base:
            pooled_value += (r["amount"] or 0.0) * base
    return {
        "month": month,
        "rows": [dict(r) for r in rows],
        "baselines": baselines,
        "spent": spent,
        "pooled_value": pooled_value,
        "saved": pooled_value,  # pooled usage cost ~0 marginally; on payg it would be this
    }


# ---------- collector sync ----------

def upsert_reading(db: Database, reading: Any, account_label: str = "auto") -> Dict[str, Any]:
    """Upsert one collector QuotaReading into the ledger.

    Match key: (agent, window_key). Creates a percentage-based entry when the API
    only reports utilization (total unknown); calibrates remaining against the
    manually estimated total when one exists.
    """
    now = now_utc()
    row = db.conn.execute(
        "SELECT * FROM quota_entries WHERE agent = ? AND window_key = ? ORDER BY id LIMIT 1",
        (reading.agent, reading.window_key),
    ).fetchone()
    period_start = None
    if reading.resets_at and reading.window_seconds:
        period_start = iso(reading.resets_at - timedelta(seconds=reading.window_seconds))

    if row is None:
        kind = "credit_pack" if reading.window_key == "credits" else "subscription"
        note_parts = [f"plan: {reading.plan}"] if reading.plan else []
        if reading.account_hint:
            note_parts.append(f"acct: {reading.account_hint}")
        initial_remaining = reading.remaining_abs
        initial_used = reading.used_percent
        if initial_remaining is None and initial_used is None and reading.consumed_abs is not None:
            initial_used = None  # consumed alone can't fix a percentage without a total
        cur = db.conn.execute(
            """INSERT INTO quota_entries
               (agent, account, kind, capability_tier, unit, total, remaining,
                cost_per_unit, expires_at, reset_period, period_start,
                window_key, window_seconds, used_percent, last_synced_at, notes,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (reading.agent, account_label, kind, reading.capability_tier, reading.unit,
            None, initial_remaining, 0.0, None, None, period_start,
            reading.window_key, reading.window_seconds, initial_used,
            iso(now), "; ".join(note_parts) or None, iso(now), iso(now)),
       )
        db.conn.commit()
        return {"action": "created", "entry_id": int(cur.lastrowid)}

    remaining = row["remaining"]
    used_percent = reading.used_percent
    if reading.remaining_abs is not None:
        remaining = reading.remaining_abs
    elif reading.used_percent is not None and row["total"]:
        remaining = round(row["total"] * (1.0 - reading.used_percent / 100.0), 4)
    elif reading.consumed_abs is not None and row["total"]:
        # metering APIs (ark, devin) report consumption, not balance
        remaining = max(round(row["total"] - reading.consumed_abs, 4), 0)
        if used_percent is None and row["total"] > 0:
            used_percent = round(min(reading.consumed_abs / row["total"], 1.0) * 100.0, 2)
    db.conn.execute(
        """UPDATE quota_entries SET remaining = ?, used_percent = ?, last_synced_at = ?,
           period_start = COALESCE(?, period_start), updated_at = ? WHERE id = ?""",
        (remaining, used_percent, iso(now), period_start, iso(now), row["id"]),
    )
    db.conn.commit()
    return {"action": "updated", "entry_id": row["id"]}
