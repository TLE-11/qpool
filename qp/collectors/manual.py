"""Manual / static quota collector.

For agents with no portable quota API to poll (yet), readings are declared in a
JSON config file and flow through the same sync pipeline. Covers:

- kiro        (AWS: subscription-only, ACP method list has no usage RPC;
              web console only -- confirmed no official API)
- antigravity (needs a live pty-wrapped `agy` process; heavyweight)
- opencode    (dashboard HTML scraping, needs workspace id + cookie)
- kilo-code   (gateway has inference endpoints only; balance is dashboard-only)
- continue    (hub API exposes only free-trial status)
- qoder       (usage readable only via the local CLI's Agent SDK, per node)
- aider, roo-code (pure BYOK; query the underlying provider key instead)
- acme-agent   (internal agent, wire its real API here later)

Note: grok / cline / windsurf / devin / doubao now have REAL collectors
(xai management API, cline.bot REST, codeium enterprise, devin v3, ark V4).

Config path: $QPOOL_STATIC_QUOTAS or ~/.qpool/static_quotas.json

Example:
{
  "agents": [
    {"agent": "kiro",      "window_key": "monthly", "label": "kiro pro monthly",
     "unit": "credits",  "used_percent": 40, "plan": "pro",  "capability_tier": 4},
    {"agent": "acme-agent", "window_key": "daily",   "label": "acme-agent internal",
     "unit": "requests", "remaining_abs": 900, "plan": "internal", "capability_tier": 4},
    {"agent": "doubao",    "window_key": "balance", "label": "ark balance",
     "unit": "usd",      "remaining_abs": 12.5, "capability_tier": 3}
  ]
}

Fields per entry: agent (required), window_key, label, unit, used_percent,
remaining_abs, resets_at (ISO), window_seconds, plan, account_hint,
capability_tier. All except agent are optional.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import CollectorError, QuotaReading, parse_iso8601

AGENT = "manual"
CAPABILITY_TIER = 3


def _config_path() -> Optional[Path]:
    env = os.environ.get("QPOOL_STATIC_QUOTAS")
    path = Path(env).expanduser() if env else Path.home() / ".qpool" / "static_quotas.json"
    return path if path.is_file() else None


def detect_credentials() -> Optional[Dict[str, Any]]:
    path = _config_path()
    if path is None:
        return None
    return {"source": str(path)}


def has_agent(creds: Dict[str, Any], agent: str) -> bool:
    path = _config_path()
    if path is None:
        return False
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return any(e.get("agent") == agent for e in data.get("agents") or [] if isinstance(e, dict))


def fetch_readings(creds: Dict[str, Any], agent_filter: Optional[str] = None) -> List[QuotaReading]:
    path = _config_path()
    if path is None:
        raise CollectorError("no static quota config found")
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectorError(f"cannot parse {path}: {exc}") from exc
    entries = data.get("agents")
    if not isinstance(entries, list) or not entries:
        raise CollectorError(f"{path} has no 'agents' array")

    readings: List[QuotaReading] = []
    for e in entries:
        if not isinstance(e, dict) or not e.get("agent"):
            continue
        if agent_filter and e["agent"] != agent_filter:
            continue
        resets_at = None
        if isinstance(e.get("resets_at"), str):
            resets_at = parse_iso8601(e["resets_at"])
        readings.append(QuotaReading(
            agent=str(e["agent"]).strip().lower(),
            window_key=e.get("window_key") or "manual",
            label=e.get("label") or f"{e['agent']} (manual)",
            unit=e.get("unit") or "requests",
            used_percent=float(e["used_percent"]) if isinstance(e.get("used_percent"), (int, float)) else None,
            remaining_abs=float(e["remaining_abs"]) if isinstance(e.get("remaining_abs"), (int, float)) else None,
            resets_at=resets_at,
            window_seconds=int(e["window_seconds"]) if isinstance(e.get("window_seconds"), int) else None,
            plan=e.get("plan"),
            account_hint=e.get("account_hint"),
            capability_tier=int(e.get("capability_tier") or CAPABILITY_TIER),
        ))
    if not readings:
        scope = f" for agent {agent_filter!r}" if agent_filter else ""
        raise CollectorError(f"no static quota entries{scope} in {path}")
    return readings
