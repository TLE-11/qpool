"""Shared types for quota collectors.

A collector detects locally stored agent credentials (auth files, OS keychains,
app state databases) and polls the vendor's quota API, returning normalized
QuotaReading objects that `qpool quota sync` upserts into the ledger.

Collector contract is qpool's own; vendor credential locations and API
endpoints were identified with reference to the onWatch project (GPL-3.0,
external reference only). Original implementations; no code copied.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class CollectorError(Exception):
    """Credential detection or API polling failure (user-facing)."""


@dataclass
class QuotaReading:
    """One normalized quota reading from a vendor API."""

    agent: str                       # codex / claude-code / cursor
    window_key: str                  # five_hour / seven_day / current_period / credits
    label: str                       # human-readable, e.g. "codex 5-hour window"
    unit: str = "requests"
    used_percent: Optional[float] = None    # 0-100, None if unknown
    remaining_abs: Optional[float] = None   # absolute remaining (credit balances)
    consumed_abs: Optional[float] = None    # absolute consumed this period (metering APIs
                                            # like ark/devin report usage, not balance;
                                            # remaining is derived against the entry's total)
    resets_at: Optional[datetime] = None    # when the window resets
    window_seconds: Optional[int] = None    # rolling window length
    plan: Optional[str] = None              # plan_type / membership label
    account_hint: Optional[str] = None      # email / account id (for display only)
    capability_tier: int = 4


@dataclass
class CollectResult:
    agent: str
    readings: List[QuotaReading] = field(default_factory=list)
    credential_source: Optional[str] = None  # where the token came from
    error: Optional[str] = None


def http_json(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """Minimal JSON-over-HTTP helper (stdlib only). Raises CollectorError."""
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(1 << 16)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise CollectorError("unauthorized (401): token expired or invalid") from exc
        if exc.code == 403:
            raise CollectorError("forbidden (403): access denied") from exc
        if exc.code == 429:
            raise CollectorError("rate limited (429)") from exc
        if exc.code >= 500:
            raise CollectorError(f"server error ({exc.code})") from exc
        raise CollectorError(f"unexpected HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise CollectorError(f"network error: {exc.reason}") from exc
    if not raw:
        raise CollectorError("empty response body")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CollectorError(f"invalid JSON response: {exc}") from exc


def parse_iso8601(value: str) -> Optional[datetime]:
    """Parse ISO 8601 with optional trailing Z, returning aware UTC datetime."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
