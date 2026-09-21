"""Gemini CLI (Google) quota collector.

Credential locations and API endpoints informed by the onWatch project
(GPL-3.0, used as an external reference only). Original Python
implementation; no code copied.

Credentials: ~/.gemini/oauth_creds.json (written by `gemini` CLI login),
GEMINI_TOKEN env as fallback.
API: POST https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota
-> {buckets: [{modelId, remainingFraction (0-1), resetTime}]}.
Buckets are aggregated per model family (pro / flash / flash_lite):
the tightest bucket in a family governs.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import CollectorError, QuotaReading, http_json, parse_iso8601

AGENT = "gemini-cli"
CAPABILITY_TIER = 4

QUOTA_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"


def _creds_path() -> Optional[Path]:
    path = Path.home() / ".gemini" / "oauth_creds.json"
    return path if path.is_file() else None


def detect_credentials() -> Optional[Dict[str, Any]]:
    path = _creds_path()
    if path is not None:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        token = (data.get("access_token") or "").strip()
        if token:
            expiry_ms = data.get("expiry_date")
            expired = isinstance(expiry_ms, (int, float)) and expiry_ms <= time.time() * 1000
            return {
                "access_token": token,
                "source": str(path),
                "expired": expired,
            }
    env_token = (os.environ.get("GEMINI_TOKEN") or "").strip()
    if env_token:
        return {"access_token": env_token, "source": "GEMINI_TOKEN env", "expired": False}
    return None


def _family(model_id: str) -> str:
    lower = model_id.lower()
    if "flash" in lower and "lite" in lower:
        return "flash_lite"
    if "pro" in lower:
        return "pro"
    if "flash" in lower:
        return "flash"
    return model_id  # unknown model = its own family


def fetch_readings(creds: Dict[str, Any]) -> List[QuotaReading]:
    if creds.get("expired"):
        raise CollectorError("gemini oauth token expired; re-run `gemini` to log in again")
    data = http_json(
        QUOTA_URL,
        method="POST",
        headers={
            "Authorization": f"Bearer {creds['access_token']}",
            "Content-Type": "application/json",
        },
        body=b"{}",
    )
    buckets = data.get("buckets")
    if not isinstance(buckets, list) or not buckets:
        raise CollectorError("quota response contained no buckets")

    # aggregate per family: tightest (lowest remaining) bucket governs
    families: Dict[str, Dict[str, Any]] = {}
    for b in buckets:
        if not isinstance(b, dict):
            continue
        model_id = b.get("modelId") or ""
        fraction = b.get("remainingFraction")
        if not isinstance(fraction, (int, float)):
            continue
        fam = _family(model_id)
        resets = parse_iso8601(b["resetTime"]) if isinstance(b.get("resetTime"), str) else None
        cur = families.get(fam)
        if cur is None or fraction < cur["fraction"]:
            families[fam] = {"fraction": fraction, "resets": resets or (cur and cur["resets"])}

    readings: List[QuotaReading] = []
    for fam in sorted(families, key=lambda f: (f != "pro", f != "flash", f)):
        info = families[fam]
        used_percent = round((1.0 - info["fraction"]) * 100.0, 1)
        readings.append(QuotaReading(
            agent=AGENT,
            window_key=f"models_{fam}",
            label=f"gemini {fam} models",
            unit="requests",
            used_percent=used_percent,
            resets_at=info["resets"],
            window_seconds=None,
            plan=None,
            account_hint=None,
            capability_tier=CAPABILITY_TIER,
        ))
    if not readings:
        raise CollectorError("no usable quota buckets in response")
    return readings
