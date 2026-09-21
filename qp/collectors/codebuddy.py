"""CodeBuddy (Tencent) quota collector.

Data source: local session transcripts at ~/.codebuddy/projects/**/*.jsonl
(claude-code-like format; timestamps are epoch MILLISECONDS here, and some
records lack a model field).

CodeBuddy bills in credits, but the token->credits ratio and the plan cap are
not published locally, so this collector reports absolute monthly token
consumption (per model when available). Once you know your real credit cap
(see `/usage` in the codebuddy TUI), set `total` on the ledger entry to turn
readings into percentages.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import QuotaReading

AGENT = "codebuddy"
CAPABILITY_TIER = 4

PROJECTS_DIR = Path.home() / ".codebuddy" / "projects"
AUTH_MARK = (
    Path.home() / "Library" / "Application Support"
    / "CodeBuddyExtension" / "Data" / "Public" / "auth"
)


def detect_credentials() -> Optional[Dict[str, Any]]:
    if not PROJECTS_DIR.is_dir():
        return None
    hint = None
    info = AUTH_MARK / "Tencent-Cloud.coding-copilot.info"
    if info.is_file():
        try:
            data = json.loads(info.read_text())
            hint = (data.get("account") or {}).get("nickname") or None
        except (OSError, json.JSONDecodeError):
            pass
    return {"source": str(PROJECTS_DIR), "account_hint": hint}


def _iter_month_tokens(month_start_ms: float) -> Dict[str, float]:
    """Aggregate this month's tokens per model, deduped by message id/uuid."""
    seen: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for path in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            fh = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = rec.get("timestamp")
                if isinstance(ts, str):  # tolerate ISO strings too
                    try:
                        ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
                    except ValueError:
                        continue
                if not isinstance(ts, (int, float)) or ts < month_start_ms:
                    continue
                if rec.get("role") != "assistant":
                    continue
                msg = rec.get("message") or {}
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                provider = rec.get("providerData") or {}
                key = rec.get("id") or provider.get("messageId") or rec.get("uuid")
                if not key:
                    continue
                if key not in seen:
                    order.append(key)
                seen[key] = {
                    "model": (provider.get("model") or provider.get("requestModelName")
                              or "unknown"),
                    "tokens": usage.get("total_tokens")
                    or (usage.get("input_tokens") or 0) + (usage.get("output_tokens") or 0),
                }
    per_model: Dict[str, float] = {}
    for key in order:
        item = seen[key]
        per_model[item["model"]] = per_model.get(item["model"], 0.0) + float(item["tokens"] or 0)
    return per_model


def fetch_readings(creds: Dict[str, Any]) -> List[QuotaReading]:
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    per_model = _iter_month_tokens(month_start.timestamp() * 1000.0)
    if not per_model:
        return []

    hint = creds.get("account_hint")
    next_month = (month_start.replace(day=28) + timedelta(days=8)).replace(day=1)
    readings: List[QuotaReading] = [QuotaReading(
        agent=AGENT,
        window_key="monthly_tokens",
        label="codebuddy monthly tokens (all models)",
        unit="tokens",
        consumed_abs=sum(per_model.values()),
        resets_at=next_month,
        plan=None,
        account_hint=hint,
        capability_tier=CAPABILITY_TIER,
    )]
    for model, tokens in sorted(per_model.items(), key=lambda kv: -kv[1]):
        if model == "unknown":
            continue
        safe = "".join(c if c.isalnum() else "_" for c in model.lower())[:40]
        readings.append(QuotaReading(
            agent=AGENT,
            window_key=f"model_{safe}",
            label=f"codebuddy {model}",
            unit="tokens",
            consumed_abs=tokens,
            resets_at=next_month,
            account_hint=hint,
            capability_tier=CAPABILITY_TIER,
        ))
    return readings
