"""xAI / Grok quota collector (official Management API).

Replaces onWatch's grok.com gRPC-web + protobuf reverse engineering
(grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig) with the documented
management API.

Docs: https://docs.x.ai/api-reference/
API:
  GET https://management-api.x.ai/v1/billing/teams/{teamId}/postpaid/invoice/preview
      -> prepaid credit balance + current cycle consumption (field names are
         parsed tolerantly; confirm against a real key)

Credentials (both env):
  XAI_MANAGEMENT_KEY  - from console.x.ai Management Keys (NOT the sk-xai-
                        inference key)
  XAI_TEAM_ID         - team UUID from console.x.ai
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .base import CollectorError, QuotaReading, http_json

AGENT = "grok"
CAPABILITY_TIER = 3

BASE_URL = "https://management-api.x.ai"


def detect_credentials() -> Optional[Dict[str, Any]]:
    key = (os.environ.get("XAI_MANAGEMENT_KEY") or "").strip()
    team = (os.environ.get("XAI_TEAM_ID") or "").strip()
    if not key or not team:
        return None
    return {"access_token": key, "team_id": team,
            "source": "XAI_MANAGEMENT_KEY/XAI_TEAM_ID env"}


def _find_numbers(obj: Any, path: str = "") -> Dict[str, float]:
    """Flatten numeric leaf fields for tolerant response parsing."""
    found: Dict[str, float] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            found.update(_find_numbers(v, f"{path}.{k}" if path else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found.update(_find_numbers(v, f"{path}[{i}]"))
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        found[path] = float(obj)
    return found


def _pick(flat: Dict[str, float], *needles: str) -> Optional[float]:
    for path, value in flat.items():
        lowered = path.lower()
        if any(n in lowered for n in needles):
            return value
    return None


def fetch_readings(creds: Dict[str, Any]) -> List[QuotaReading]:
    team_id = creds["team_id"]
    data = http_json(
        f"{BASE_URL}/v1/billing/teams/{team_id}/postpaid/invoice/preview",
        headers={"Authorization": f"Bearer {creds['access_token']}"},
    )
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    flat = _find_numbers(payload)

    balance = _pick(flat, "prepaidbalance", "creditbalance", "balance", "prepaidcredit")
    consumed = _pick(flat, "currentcycleconsumption", "cycleusage", "consumption",
                     "amountdue", "usage")
    if balance is None and consumed is None:
        raise CollectorError(
            "invoice preview had no recognizable balance/consumption fields; "
            "the management API shape may have changed")

    readings: List[QuotaReading] = []
    if balance is not None:
        readings.append(QuotaReading(
            agent=AGENT, window_key="prepaid_balance",
            label="xai prepaid credits", unit="usd",
            remaining_abs=balance, account_hint=f"team:{team_id[:8]}",
            capability_tier=CAPABILITY_TIER,
        ))
    if consumed is not None:
        readings.append(QuotaReading(
            agent=AGENT, window_key="cycle_consumption",
            label="xai current cycle consumption", unit="usd",
            consumed_abs=consumed, account_hint=f"team:{team_id[:8]}",
            capability_tier=CAPABILITY_TIER,
        ))
    return readings
