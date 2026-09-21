"""Cline quota collector (official REST API).

Docs: https://docs.cline.bot/enterprise-solutions/api-reference
Cline is the only one of the open-source BYOK harnesses that also runs its own
usage-billing account with an official balance/usage REST API.

Credentials: CLINE_API_KEY env (created at app.cline.bot -> Settings -> API Keys;
the same key works for inference and usage).
API:
  GET https://api.cline.bot/api/v1/users/me           -> user id / org
  GET https://api.cline.bot/api/v1/users/{id}/balance -> credit balance
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from .base import CollectorError, QuotaReading, http_json

AGENT = "cline"
CAPABILITY_TIER = 3  # BYOK harness: capability follows the configured model

BASE_URL = "https://api.cline.bot"


def detect_credentials() -> Optional[Dict[str, Any]]:
    token = (os.environ.get("CLINE_API_KEY") or "").strip()
    if not token:
        return None
    return {"access_token": token, "source": "CLINE_API_KEY env"}


def _get(creds: Dict[str, Any], path: str) -> Dict[str, Any]:
    return http_json(
        BASE_URL + path,
        headers={"Authorization": f"Bearer {creds['access_token']}"},
    )


def _pick_number(data: Dict[str, Any], *keys: str) -> Optional[float]:
    """Tolerantly pick the first numeric field among candidate key names."""
    for key in keys:
        value = data.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def fetch_readings(creds: Dict[str, Any]) -> List[QuotaReading]:
    me = _get(creds, "/api/v1/users/me")
    user = me.get("data") if isinstance(me.get("data"), dict) else me
    user_id = user.get("id") or user.get("userId") or user.get("uid")
    if not user_id:
        raise CollectorError("users/me returned no user id")
    hint = user.get("email") or user.get("name") or None

    balance_resp = _get(creds, f"/api/v1/users/{user_id}/balance")
    balance_data = balance_resp.get("data") if isinstance(balance_resp.get("data"), dict) else balance_resp
    balance = _pick_number(balance_data, "balance", "credits", "creditBalance", "remaining")
    if balance is None:
        raise CollectorError("balance response had no recognizable balance field")

    return [QuotaReading(
        agent=AGENT,
        window_key="balance",
        label="cline credit balance",
        unit="credits",
        remaining_abs=balance,
        plan=None,
        account_hint=str(hint) if hint else None,
        capability_tier=CAPABILITY_TIER,
    )]
