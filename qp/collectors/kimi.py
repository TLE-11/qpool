"""Kimi CLI (Moonshot) quota collector.

Credential locations and API endpoints informed by the onWatch project
(GPL-3.0, used as an external reference only). Original Python
implementation; no code copied.

Credentials: $KIMI_CODE_HOME/credentials/kimi-code.json or
~/.kimi-code/credentials/kimi-code.json (written by the kimi-code CLI).
API: GET https://api.kimi.com/coding/v1/usages ->
{usage: {limit, used, remaining, resetTime},
 limits: [{window: {duration, timeUnit}, detail: {...}}],
 user: {membership: {level}}}. Numeric fields may arrive as strings.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import CollectorError, QuotaReading, http_json, parse_iso8601

AGENT = "kimi-cli"
CAPABILITY_TIER = 3

USAGE_URL = "https://api.kimi.com/coding/v1/usages"


def _creds_path() -> Optional[Path]:
    override = os.environ.get("KIMI_CODE_HOME") or os.environ.get("KIMI_CODE_CREDENTIALS")
    if override:
        path = Path(override)
        if path.is_dir():
            path = path / "credentials" / "kimi-code.json"
        return path if path.is_file() else None
    path = Path.home() / ".kimi-code" / "credentials" / "kimi-code.json"
    return path if path.is_file() else None


def _expires_at_unix(value: Any) -> int:
    """Normalize expires_at: seconds, or milliseconds if past ~year 2001 in ms."""
    if not isinstance(value, (int, float)) or value <= 0:
        return 0
    if value > 1e12:
        return int(value / 1000)
    return int(value)


def detect_credentials() -> Optional[Dict[str, Any]]:
    path = _creds_path()
    if path is None:
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    token = (data.get("access_token") or "").strip()
    if not token:
        return None
    exp = _expires_at_unix(data.get("expires_at"))
    # treat as expired 60s early, like onWatch
    expired = exp > 0 and time.time() >= exp - 60
    return {"access_token": token, "source": str(path), "expired": expired}


def _num(value: Any) -> Optional[float]:
    """Numeric fields often arrive as strings."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _parse_reset_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    dt = parse_iso8601(value)
    if dt is not None:
        return dt
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _detail_to_reading(window_key: str, label: str, detail: Dict[str, Any],
                       plan: str, hint: Optional[str]) -> Optional[QuotaReading]:
    limit = _num(detail.get("limit"))
    used = _num(detail.get("used"))
    remaining = _num(detail.get("remaining"))
    used_percent = None
    if limit and used is not None and limit > 0:
        used_percent = round(used / limit * 100.0, 1)
    return QuotaReading(
        agent=AGENT,
        window_key=window_key,
        label=label,
        unit="requests",
        used_percent=used_percent,
        remaining_abs=remaining,
        resets_at=_parse_reset_time(detail.get("resetTime")),
        window_seconds=None,
        plan=plan,
        account_hint=hint,
        capability_tier=CAPABILITY_TIER,
    )


def fetch_readings(creds: Dict[str, Any]) -> List[QuotaReading]:
    if creds.get("expired"):
        raise CollectorError("kimi token expired; re-run the kimi-code CLI to log in again")
    data = http_json(USAGE_URL, headers={"Authorization": f"Bearer {creds['access_token']}"})

    plan = data.get("subType") or ""
    user = data.get("user") or {}
    membership = user.get("membership") or {}
    if membership.get("level"):
        plan = membership["level"]
    hint = user.get("userId") or None

    readings: List[QuotaReading] = []
    total = data.get("totalQuota") or data.get("usage")
    if isinstance(total, dict):
        r = _detail_to_reading("total", "kimi total quota", total, plan, hint)
        if r:
            readings.append(r)
    for item in data.get("limits") or []:
        if not isinstance(item, dict):
            continue
        window = item.get("window") or {}
        detail = item.get("detail")
        if not isinstance(detail, dict):
            continue
        duration = window.get("duration")
        unit = (window.get("timeUnit") or "?").lower()
        suffix = {"minutes": "m", "hours": "h", "days": "d"}.get(unit, unit[:1] or "w")
        key = f"window_{duration}{suffix}" if duration else "window_unknown"
        label = f"kimi {duration}{suffix} window" if duration else "kimi window"
        readings.append(_detail_to_reading(key, label, detail, plan, hint))
    readings = [r for r in readings if r]
    if not readings:
        raise CollectorError("usage response contained no quota details")
    return readings
