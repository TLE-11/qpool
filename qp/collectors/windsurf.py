"""Windsurf (Codeium) quota collector — Enterprise only.

Docs: https://docs.windsurf.com/windsurf/accounts/api-reference/get-team-credit-balance
API: POST https://server.codeium.com/api/v1/GetTeamCreditBalance
     body {"service_key": "..."}  (the key goes in the BODY, not a header)
     -> {promptCreditsPerSeat, numSeats, addOnCreditsAvailable,
         addOnCreditsUsed, billingCycleStart, billingCycleEnd}

Credentials: WINDSURF_SERVICE_KEY env (team settings -> Service Keys;
requires an Enterprise plan). Devin's Desktop API shares this same
server.codeium.com backend and service_key auth.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from .base import CollectorError, QuotaReading, http_json, parse_iso8601

AGENT = "windsurf"
CAPABILITY_TIER = 4

BALANCE_URL = "https://server.codeium.com/api/v1/GetTeamCreditBalance"


def detect_credentials() -> Optional[Dict[str, Any]]:
    key = (os.environ.get("WINDSURF_SERVICE_KEY") or "").strip()
    if not key:
        return None
    return {"service_key": key, "source": "WINDSURF_SERVICE_KEY env"}


def fetch_readings(creds: Dict[str, Any]) -> List[QuotaReading]:
    data = http_json(
        BALANCE_URL,
        method="POST",
        headers={"Content-Type": "application/json"},
        body=json.dumps({"service_key": creds["service_key"]}).encode("utf-8"),
    )

    per_seat = data.get("promptCreditsPerSeat")
    seats = data.get("numSeats")
    addon_available = data.get("addOnCreditsAvailable")
    addon_used = data.get("addOnCreditsUsed")
    cycle_end = parse_iso8601(data["billingCycleEnd"]) if isinstance(data.get("billingCycleEnd"), str) else None
    cycle_start = parse_iso8601(data["billingCycleStart"]) if isinstance(data.get("billingCycleStart"), str) else None

    # seat-pool consumption is not exposed by this endpoint, so the pool itself
    # yields no meaningful reading; surface it as account context only.
    pool_hint = None
    if isinstance(per_seat, (int, float)) and isinstance(seats, (int, float)) and seats > 0:
        pool_hint = f"{int(seats)} seats x {per_seat:g} credits"

    readings: List[QuotaReading] = []
    if isinstance(addon_available, (int, float)):
        readings.append(QuotaReading(
            agent=AGENT, window_key="addon_credits",
            label="windsurf add-on credits",
            unit="credits", remaining_abs=float(addon_available),
            resets_at=cycle_end, plan="enterprise",
            account_hint=pool_hint,
            capability_tier=CAPABILITY_TIER,
        ))
    if not readings:
        raise CollectorError("GetTeamCreditBalance returned no recognizable credit fields")
    return readings
