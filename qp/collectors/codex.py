"""Codex (OpenAI) quota collector.

Credential locations and API endpoints informed by the onWatch project
(GPL-3.0, used as an external reference only). Original Python
implementation; no code copied.

Credentials: $CODEX_HOME/auth.json or ~/.codex/auth.json, written by `codex login`.
API: GET https://chatgpt.com/backend-api/wham/usage (fallback /api/codex/usage),
returning rate_limit windows: primary (5-hour) + secondary (7-day), plus credits.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import CollectorError, QuotaReading, http_json

AGENT = "codex"
CAPABILITY_TIER = 4

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
FALLBACK_USAGE_URL = "https://chatgpt.com/api/codex/usage"

FIVE_HOUR_SECONDS = 5 * 60 * 60
SEVEN_DAY_SECONDS = 7 * 24 * 60 * 60


def _auth_path() -> Optional[Path]:
    home_override = os.environ.get("CODEX_HOME")
    path = Path(home_override) / "auth.json" if home_override else Path.home() / ".codex" / "auth.json"
    return path if path.is_file() else None


def detect_credentials() -> Optional[Dict[str, Any]]:
    """Returns {access_token, account_id, source} or None."""
    path = _auth_path()
    if path is not None:
        try:
            auth = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            auth = {}
        tokens = auth.get("tokens") or {}
        access_token = (tokens.get("access_token") or "").strip()
        if access_token:
            return {
                "access_token": access_token,
                "account_id": (tokens.get("account_id") or "").strip(),
                "source": str(path),
            }
    env_token = (os.environ.get("CODEX_TOKEN") or "").strip()
    if env_token:
        return {"access_token": env_token, "account_id": "", "source": "CODEX_TOKEN env"}
    return None


def _fetch_usage(creds: Dict[str, Any]) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {creds['access_token']}"}
    if creds.get("account_id"):
        headers["chatgpt-account-id"] = creds["account_id"]
    try:
        return http_json(USAGE_URL, headers=headers)
    except CollectorError as exc:
        if "unexpected HTTP 404" in str(exc):
            return http_json(FALLBACK_USAGE_URL, headers=headers)
        raise


def _window_reading(key: str, label: str, window: Dict[str, Any],
                    plan: str, account_hint: str) -> Optional[QuotaReading]:
    if not window:
        return None
    resets_at = None
    reset_unix = window.get("reset_at")
    if isinstance(reset_unix, (int, float)) and reset_unix > 0:
        resets_at = datetime.fromtimestamp(reset_unix, tz=timezone.utc)
    return QuotaReading(
        agent=AGENT,
        window_key=key,
        label=label,
        unit="requests",
        used_percent=window.get("used_percent"),
        resets_at=resets_at,
        window_seconds=window.get("limit_window_seconds"),
        plan=plan,
        account_hint=account_hint,
        capability_tier=CAPABILITY_TIER,
    )


def fetch_readings(creds: Dict[str, Any]) -> List[QuotaReading]:
    data = _fetch_usage(creds)
    plan = data.get("plan_type") or "unknown"
    hint = creds.get("account_id") or ""
    readings: List[QuotaReading] = []
    rate = data.get("rate_limit") or {}
    primary = _window_reading("five_hour", "codex 5-hour window",
                              rate.get("primary_window"), plan, hint)
    if primary:
        readings.append(primary)
    secondary = _window_reading("seven_day", "codex weekly window",
                                rate.get("secondary_window"), plan, hint)
    if secondary:
        readings.append(secondary)
    credits = data.get("credits") or {}
    balance = credits.get("balance")
    if isinstance(balance, (int, float)):
        readings.append(QuotaReading(
            agent=AGENT, window_key="credits", label="codex credit balance",
            unit="credits", remaining_abs=float(balance), plan=plan,
            account_hint=hint, capability_tier=CAPABILITY_TIER,
        ))
    if not readings:
        raise CollectorError("usage response contained no quota windows")
    return readings
