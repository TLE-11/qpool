"""GitHub Copilot quota collector.

Credential locations and API endpoints informed by the onWatch project
(GPL-3.0, used as an external reference only). Original Python
implementation; no code copied.

Credentials: COPILOT_TOKEN env (GitHub PAT with copilot scope), or the IDE's
oauth token from ~/.config/github-copilot/hosts.json / apps.json.
API: GET https://api.github.com/copilot_internal/user ->
{quota_snapshots: {key: {entitlement, remaining, percent_remaining, unlimited}}},
or the newer {limited_user_quotas, monthly_quotas} free-plan format, which we
normalize to the same shape (same normalization semantics as onWatch).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import CollectorError, QuotaReading, http_json, parse_iso8601

AGENT = "copilot"
CAPABILITY_TIER = 3

USAGE_URL = "https://api.github.com/copilot_internal/user"


def _token_from_ide_file(path: Path) -> str:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return ""
    gh = data.get("github.com")
    if isinstance(gh, dict):
        return (gh.get("oauth_token") or "").strip()
    return ""


def detect_credentials() -> Optional[Dict[str, Any]]:
    env_token = (os.environ.get("COPILOT_TOKEN") or "").strip()
    if env_token:
        return {"access_token": env_token, "source": "COPILOT_TOKEN env"}
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    for name in ("hosts.json", "apps.json"):
        path = config_home / "github-copilot" / name
        if path.is_file():
            token = _token_from_ide_file(path)
            if token:
                return {"access_token": token, "source": str(path)}
    return None


def _normalize_snapshots(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Port of CopilotUserResponse.normalize(): synthesize quota_snapshots
    from limited_user_quotas/monthly_quotas when absent (free plans)."""
    snapshots = data.get("quota_snapshots")
    if isinstance(snapshots, dict) and snapshots:
        return {k: v for k, v in snapshots.items() if isinstance(v, dict)}
    limited = data.get("limited_user_quotas")
    monthly = data.get("monthly_quotas")
    if not isinstance(limited, dict) or not isinstance(monthly, dict):
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for key, remaining in limited.items():
        limit = monthly.get(key)
        if not isinstance(limit, (int, float)) or limit == 0:
            result[key] = {"unlimited": True}
            continue
        if not isinstance(remaining, (int, float)) or remaining < 0:
            remaining = 0
        result[key] = {
            "entitlement": limit,
            "remaining": remaining,
            "percent_remaining": remaining / limit * 100.0,
            "unlimited": False,
        }
    return result


def fetch_readings(creds: Dict[str, Any]) -> List[QuotaReading]:
    data = http_json(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {creds['access_token']}",
            "Accept": "application/json",
        },
    )
    snapshots = _normalize_snapshots(data)
    plan = data.get("copilot_plan") or data.get("access_type_sku") or "unknown"
    hint = data.get("login") or None

    resets_at = None
    raw_reset = data.get("quota_reset_date_utc") or data.get("limited_user_reset_date")
    if isinstance(raw_reset, str):
        resets_at = parse_iso8601(raw_reset)
        if resets_at is None and len(raw_reset.strip()) == 10:
            resets_at = parse_iso8601(raw_reset.strip() + "T00:00:00Z")

    readings: List[QuotaReading] = []
    for key in sorted(snapshots):
        snap = snapshots[key]
        if snap.get("unlimited"):
            continue  # no quota constraint to track
        pct_remaining = snap.get("percent_remaining")
        used_percent = round(100.0 - pct_remaining, 1) if isinstance(pct_remaining, (int, float)) else None
        readings.append(QuotaReading(
            agent=AGENT,
            window_key=key,
            label=f"copilot {key.replace('_', ' ')}",
            unit="requests",
            used_percent=used_percent,
            remaining_abs=float(snap["remaining"]) if isinstance(snap.get("remaining"), (int, float)) else None,
            resets_at=resets_at,
            window_seconds=None,
            plan=plan,
            account_hint=hint,
            capability_tier=CAPABILITY_TIER,
        ))
    if not readings:
        raise CollectorError("no metered quota snapshots in response (plan may be unlimited)")
    return readings
