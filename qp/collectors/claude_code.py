"""Claude Code (Anthropic) quota collector.

Credential locations and API endpoints informed by the onWatch project
(GPL-3.0, used as an external reference only). Original Python
implementation; no code copied.

Credentials: macOS Keychain item "Claude Code-credentials" (via `security` CLI),
falling back to ~/.claude/.credentials.json. The JSON shape is
{"claudeAiOauth": {"accessToken", "refreshToken", "expiresAt"(ms), ...}}.
API: GET https://api.anthropic.com/api/oauth/usage with the oauth beta header.
The response is a dynamic map of quota windows (five_hour, seven_day, ...),
each {utilization, resets_at}.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import CollectorError, QuotaReading, http_json, parse_iso8601

AGENT = "claude-code"
CAPABILITY_TIER = 5

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

WINDOW_LABELS = {
    "five_hour": "claude 5-hour window",
    "seven_day": "claude weekly window",
}
WINDOW_SECONDS = {
    "five_hour": 5 * 60 * 60,
    "seven_day": 7 * 24 * 60 * 60,
}


def _parse_credentials(data: bytes) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return None
    oauth = obj.get("claudeAiOauth") or {}
    token = (oauth.get("accessToken") or "").strip()
    if not token:
        return None
    expires_ms = oauth.get("expiresAt")
    return {
        "access_token": token,
        "expires_at_ms": expires_ms if isinstance(expires_ms, (int, float)) else None,
        "subscription_type": oauth.get("subscriptionType") or "",
    }


def detect_credentials() -> Optional[Dict[str, Any]]:
    """macOS Keychain first (like the real Claude Code), then the file."""
    if platform.system() == "Darwin":
        username = os.environ.get("USER") or ""
        if username:
            try:
                out = subprocess.run(
                    ["security", "find-generic-password",
                     "-s", "Claude Code-credentials", "-a", username, "-w"],
                    capture_output=True, timeout=10, check=True,
                ).stdout
                creds = _parse_credentials(out)
                if creds:
                    creds["source"] = "macOS Keychain"
                    return creds
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                pass
    path = Path.home() / ".claude" / ".credentials.json"
    if path.is_file():
        try:
            creds = _parse_credentials(path.read_bytes())
        except OSError:
            creds = None
        if creds:
            creds["source"] = str(path)
            return creds
    return None


def credentials_expired(creds: Dict[str, Any]) -> bool:
    expires_ms = creds.get("expires_at_ms")
    if not expires_ms:
        return False
    import time
    return expires_ms <= time.time() * 1000


def fetch_readings(creds: Dict[str, Any]) -> List[QuotaReading]:
    data = http_json(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {creds['access_token']}",
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.1.69",
        },
    )
    plan = creds.get("subscription_type") or "unknown"
    readings: List[QuotaReading] = []
    for key, entry in data.items():
        if not isinstance(entry, dict):
            continue  # skip companion fields (limits[], spend, booleans)
        utilization = entry.get("utilization")
        if utilization is None:
            continue
        resets_at = None
        raw_resets = entry.get("resets_at")
        if isinstance(raw_resets, str):
            resets_at = parse_iso8601(raw_resets)
        label = WINDOW_LABELS.get(key, f"claude {key.replace('_', ' ')}")
        readings.append(QuotaReading(
            agent=AGENT,
            window_key=key,
            label=label,
            unit="requests",
            used_percent=float(utilization),
            resets_at=resets_at,
            window_seconds=WINDOW_SECONDS.get(key),
            plan=plan,
            account_hint=None,
            capability_tier=CAPABILITY_TIER,
        ))
    if not readings:
        raise CollectorError("usage response contained no quota windows")
    readings.sort(key=lambda r: (r.window_key != "five_hour", r.window_key != "seven_day", r.window_key))
    return readings
