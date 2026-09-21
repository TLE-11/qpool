"""Cursor quota collector.

Credential locations and API endpoints informed by the onWatch project
(GPL-3.0, used as an external reference only). Original Python
implementation; no code copied.

Credentials: Cursor's VS Code state DB at
~/Library/Application Support/Cursor/User/globalStorage/state.vscdb (macOS),
reading cursorAuth/accessToken from the ItemTable (read-only).
API: Connect-RPC POST https://api2.cursor.sh/aiserver.v1.DashboardService/
GetCurrentPeriodUsage -> planUsage.{totalPercentUsed, limit}.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import CollectorError, QuotaReading, http_json

AGENT = "cursor"
CAPABILITY_TIER = 4

BASE_URL = "https://api2.cursor.sh"
USAGE_METHOD = "/aiserver.v1.DashboardService/GetCurrentPeriodUsage"


def _state_db_path() -> Optional[Path]:
    home = Path.home()
    if sys.platform == "darwin":
        path = home / "Library" / "Application Support" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    elif sys.platform.startswith("win"):
        path = home / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    else:
        path = home / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    return path if path.is_file() else None


def _read_state_value(db: sqlite3.Connection, key: str) -> str:
    row = db.execute("SELECT value FROM ItemTable WHERE key = ? LIMIT 1", (key,)).fetchone()
    return (row[0] or "").strip() if row and row[0] else ""


def detect_credentials() -> Optional[Dict[str, Any]]:
    path = _state_db_path()
    if path is None:
        return None
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        access_token = _read_state_value(db, "cursorAuth/accessToken")
        if not access_token:
            return None
        return {
            "access_token": access_token,
            "email": _read_state_value(db, "cursorAuth/cachedEmail"),
            "membership": _read_state_value(db, "cursorAuth/stripeMembershipType").lower(),
            "source": str(path),
        }
    finally:
        db.close()


def fetch_readings(creds: Dict[str, Any]) -> List[QuotaReading]:
    data = http_json(
        BASE_URL + USAGE_METHOD,
        method="POST",
        headers={
            "Authorization": f"Bearer {creds['access_token']}",
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
        },
        body=b"{}",
    )
    plan_usage = data.get("planUsage")
    if not isinstance(plan_usage, dict):
        raise CollectorError("usage response had no planUsage object")
    plan = creds.get("membership") or "unknown"
    hint = creds.get("email") or None
    limit = plan_usage.get("limit")
    used_percent = plan_usage.get("totalPercentUsed")
    remaining_abs = None
    if isinstance(limit, (int, float)) and limit > 0 and isinstance(used_percent, (int, float)):
        remaining_abs = round(limit * (1.0 - used_percent / 100.0), 2)
    return [QuotaReading(
        agent=AGENT,
        window_key="current_period",
        label="cursor current period",
        unit="requests",
        used_percent=float(used_percent) if isinstance(used_percent, (int, float)) else None,
        remaining_abs=remaining_abs,
        plan=plan,
        account_hint=hint,
        capability_tier=CAPABILITY_TIER,
    )]
