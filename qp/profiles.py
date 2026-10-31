"""Explicit local account/pool/channel configuration; no secrets are persisted here."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Dict

from .collectors.base import CollectorError


def config_path() -> Path:
    return Path(os.environ.get("QPOOL_CONFIG", "~/.qpool/pools.json")).expanduser()


def field(data: Any, path: str) -> Any:
    for key in path.split("."):
        if not isinstance(data, dict) or key not in data:
            raise CollectorError("credential identity or mapped field is missing")
        data = data[key]
    return data


def load(data: Any = None) -> Dict[str, Any]:
    path = config_path()
    if data is None and not path.exists():
        data = {"accounts": [], "channels": []}
    try:
        data = json.loads(path.read_text()) if data is None else data
        accounts, pools, identities = {}, {}, set()
        for raw in data.get("accounts", []):
            account = dict(raw)
            aid, agent = account["id"], account["agent"]
            if not isinstance(aid, str) or not aid.strip() or aid in accounts:
                raise ValueError("account IDs must be nonempty and unique")
            if not isinstance(agent, str) or not agent.strip():
                raise ValueError("agent must be nonempty")
            account["agent"] = agent.strip().lower()
            if not isinstance(account.get("label", aid), str):
                raise ValueError("account label must be text")
            source = account["source"]
            if not all(isinstance(source.get(k), str) and source[k]
                       for k in ("subject_field", "expected_subject")):
                raise ValueError("each account requires subject_field and expected_subject")
            identity = (account["agent"], source["expected_subject"])
            if identity in identities:
                raise ValueError("provider identity must not be declared as multiple accounts")
            identities.add(identity)
            if source.get("credentials_file") and (not isinstance(source.get("fields"), dict)
                    or not all(isinstance(k, str) and isinstance(v, str) and v
                               for k, v in source["fields"].items())):
                raise ValueError("invalid credential field mapping")
            for key in ("credentials_file", "profile_env", "profile_dir"):
                if key in source and not isinstance(source[key], str):
                    raise ValueError("invalid credential reference")
            cpa = account.get("cpa", {})
            if not isinstance(cpa, dict) or ("exclusive" in cpa and not isinstance(cpa["exclusive"], bool)):
                raise ValueError("invalid CPA coverage declaration")
            if any(not isinstance(v, str) or not v for k, v in cpa.items() if k != "exclusive"):
                raise ValueError("CPA identity and source references must be nonempty text")
            accounts[aid] = account
            for raw_pool in account.get("pools", []):
                pool = dict(raw_pool)
                pid = pool["id"]
                if not isinstance(pid, str) or not pid.strip() or pid in pools:
                    raise ValueError("pool IDs must be nonempty and unique")
                if pool["kind"] not in ("subscription", "credit_pack", "payg"):
                    raise ValueError("invalid pool kind")
                pool.setdefault("unit", "tokens")
                pool.setdefault("capability_tier", 3)
                pool.setdefault("cost_per_unit", 0.0)
                pool.setdefault("max_age_seconds", 120)
                if not isinstance(pool["unit"], str) or not pool["unit"]:
                    raise ValueError("pool unit must be nonempty")
                if (not isinstance(pool["capability_tier"], int) or isinstance(pool["capability_tier"], bool)
                        or not 1 <= pool["capability_tier"] <= 5):
                    raise ValueError("invalid capability tier")
                for key in ("cost_per_unit", "max_age_seconds"):
                    if isinstance(pool[key], bool) or not math.isfinite(pool[key]) or pool[key] < 0:
                        raise ValueError("invalid pool price or freshness interval")
                if pool["max_age_seconds"] == 0:
                    raise ValueError("freshness interval must be positive")
                provider_billed = pool.get('billing') == 'provider' and account['agent'] == 'openai_api'
                if pool["kind"] == "payg" and pool["cost_per_unit"] <= 0 and not provider_billed:
                    raise ValueError("payg pools require a positive price")
                if pool["kind"] != "payg" and pool["cost_per_unit"] != 0:
                    raise ValueError("already-paid pools have zero marginal price")
                windows, seen = [], set()
                for raw_window in pool.get("windows", []):
                    window = dict(raw_window)
                    window.setdefault("scope", "*")
                    window.setdefault("role", "limit")
                    pair = (window["scope"], window["key"])
                    if (not all(isinstance(v, str) and v for v in pair)
                            or pair in seen or window["role"] not in ("limit", "stat")):
                        raise ValueError("invalid or duplicate window")
                    seen.add(pair)
                    windows.append(window)
                if pool["kind"] != "payg" and not any(w["role"] == "limit" for w in windows):
                    raise ValueError("finite pools need at least one limit window")
                pool.update(account_id=aid, agent=account["agent"],
                            account_label=account.get("label", aid),
                            subject=str(source["expected_subject"]), windows=windows)
                pools[pid] = pool
        channels, seen = data.get("channels", []), set()
        for channel in channels:
            cid = channel["id"]
            if not isinstance(cid, str) or not cid or cid in seen:
                raise ValueError("channel IDs must be nonempty and unique")
            seen.add(cid)
            pool = pools[channel["pool_id"]]
            if channel["account_id"] != pool["account_id"]:
                raise ValueError("channel account does not own its pool")
            if not isinstance(channel.get("model"), str) or not channel["model"] or channel["model"] == "*":
                raise ValueError("channel requires an explicit model")
            if "group" in channel and channel["group"] not in ("regular", "primary"):
                raise ValueError("invalid model group")
            tier = channel.get("capability_tier", pool["capability_tier"])
            if type(tier) is not int or not 1 <= tier <= 5:
                raise ValueError("invalid model capability tier")
        from .routing import validate_settings
        routing = validate_settings(data.get("routing", {}))
        return {"accounts": accounts, "pools": pools, "channels": channels, "routing": routing}
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        # Do not print config values: a malformed local config may contain secrets.
        raise CollectorError("invalid pools.json; check account, pool and channel definitions") from exc


def credentials(account: Dict[str, Any], collector: Any) -> Dict[str, Any]:
    source = account["source"]
    if source.get("credentials_file"):
        try:
            data = json.loads(Path(source["credentials_file"]).expanduser().read_text())
            creds = {name: field(data, location)
                     for name, location in source["fields"].items()}
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise CollectorError("cannot read configured credential reference") from exc
    else:
        creds = collector.detect_credentials()
    if not creds:
        raise CollectorError("no credentials for configured account")
    if str(field(creds, source["subject_field"])) != str(source["expected_subject"]):
        raise CollectorError("account identity changed; refusing to reuse the pool")
    return creds
