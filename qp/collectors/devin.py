"""Devin (Cognition) quota collector — Enterprise only.

Docs: https://docs.devin.ai/api-reference/overview
API: GET https://api.devin.ai/v3/enterprise/consumption/daily?time_after=&time_before=
     -> daily ACU (Agent Compute Unit) consumption. ACU measures CONSUMPTION,
        not balance, so readings feed the consumed_abs path (remaining is
        derived against a manually set monthly budget entry total).
Auth: Bearer cog_<key> (Settings -> Service Users, RBAC ViewAccountConsumption).
Note: billing-day boundary is PST midnight (08:00 UTC); Enterprise plans only.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .base import CollectorError, QuotaReading, http_json

AGENT = "devin"
CAPABILITY_TIER = 4

BASE_URL = "https://api.devin.ai"
PST_OFFSET = timedelta(hours=-8)


def detect_credentials() -> Optional[Dict[str, Any]]:
    key = (os.environ.get("DEVIN_API_KEY") or "").strip()
    if not key:
        return None
    return {"access_token": key, "source": "DEVIN_API_KEY env"}


def _find_acu(obj: Any) -> float:
    """Sum ACU-ish numeric leaves in the response."""
    total = 0.0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool) and "acu" in str(k).lower():
                total += float(v)
            else:
                total += _find_acu(v)
    elif isinstance(obj, list):
        for item in obj:
            total += _find_acu(item)
    return total


def fetch_readings(creds: Dict[str, Any]) -> List[QuotaReading]:
    now = datetime.now(timezone.utc)
    # billing month starts on the 1st at PST midnight
    month_start_pst = (now + PST_OFFSET).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    time_after = (month_start_pst - PST_OFFSET).strftime("%Y-%m-%dT%H:%M:%SZ")
    time_before = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    data = http_json(
        f"{BASE_URL}/v3/enterprise/consumption/daily?time_after={time_after}&time_before={time_before}",
        headers={"Authorization": f"Bearer {creds['access_token']}"},
    )
    payload = data.get("data") if isinstance(data.get("data"), (dict, list)) else data
    acu = _find_acu(payload)
    if acu <= 0:
        raise CollectorError(
            "consumption response had no ACU fields (or zero usage); "
            "verify the key has ViewAccountConsumption on an Enterprise plan")

    return [QuotaReading(
        agent=AGENT,
        window_key="monthly_acu",
        label="devin monthly ACU consumption",
        unit="acu",
        consumed_abs=acu,
        plan="enterprise",
        capability_tier=CAPABILITY_TIER,
    )]
