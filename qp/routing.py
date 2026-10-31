"""Task-level model selection. The HiRoute protocol is isolated at this boundary."""
from __future__ import annotations

import json
import math
import os
import queue
import re
import threading
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from . import ledger, pools, profiles

PROTOCOL = "hiroute-groups-v1"
_slots = threading.BoundedSemaphore(4)


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = value
        return result
    def invalid(value):
        raise ValueError("non-finite JSON number")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def validate_settings(value):
    if not isinstance(value, dict) or set(value) - {"endpoint", "timeout_seconds", "auth_env", "protocol"}:
        raise ValueError("invalid decision settings")
    settings = dict(endpoint="", timeout_seconds=3.0, auth_env="", protocol=PROTOCOL)
    settings.update(value)
    endpoint, auth_env = settings["endpoint"], settings["auth_env"]
    if not isinstance(endpoint, str) or len(endpoint) > 2048 or any(c.isspace() for c in endpoint):
        raise ValueError("invalid decision URL")
    if endpoint:
        url = urlsplit(endpoint)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username
                or url.password or url.query or url.fragment):
            raise ValueError("decision URL must be HTTP(S), without credentials or query")
        if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("remote decision services require HTTPS")
        _ = url.port
    timeout = settings["timeout_seconds"]
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0.1 <= timeout <= 30:
        raise ValueError("decision timeout must be 0.1 to 30 seconds")
    if not isinstance(auth_env, str) or (auth_env and not re.fullmatch(r"QPOOL_DECIDER_[A-Z0-9_]+", auth_env)):
        raise ValueError("auth environment reference must start with QPOOL_DECIDER_")
    if settings["protocol"] != PROTOCOL:
        raise ValueError("unsupported decision protocol")
    return settings


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError("decision redirects are not allowed")


def decide(task, settings, *, incoming=None):
    settings = validate_settings(settings)
    start = time.monotonic()
    fallback = {"group": "primary", "source": "fallback", "reason": "决策服务未配置，保守使用主力组",
                "cost_usd": 0.0, "duration_ms": 0}
    if not settings["endpoint"]:
        return fallback
    if not _slots.acquire(blocking=False):
        return dict(fallback, reason="决策服务并发已满，保守使用主力组")
    result = queue.Queue(maxsize=1)
    payload = incoming if incoming is not None else {"route": {"kind": "smart_saving", "groups": ["regular", "primary"]},
               "latest_user": [{"kind": "text", "text": task}], "visible_conversation": [],
               "history_partial": False, "preceding_stage": None}
    body = json.dumps(payload, ensure_ascii=False).encode()

    def request():
        try:
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if settings["auth_env"]:
                token = os.environ.get(settings["auth_env"])
                if not token:
                    raise ValueError("decision authentication is not configured")
                headers["Authorization"] = "Bearer " + token
            opener = build_opener(ProxyHandler({}), NoRedirect())
            with opener.open(Request(settings["endpoint"], body, headers),
                             timeout=settings["timeout_seconds"]) as response:
                if response.status != 200:
                    raise ValueError("decision status is not 200")
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise ValueError("decision response exceeds limit")
                data = strict_json(raw)
            if (not isinstance(data, dict) or set(data) - {"group", "reason", "assessment"}
                    or data.get("group") not in ("regular", "primary")):
                raise ValueError("invalid decision response")
            if "reason" in data and (not isinstance(data["reason"], str) or not 1 <= len(data["reason"]) <= 1024):
                raise ValueError("invalid decision reason")
            result.put({"group": data["group"], "source": "service",
                        "reason": data.get("reason", "决策服务已选择模型组"), "cost_usd": None})
        except Exception:
            # Never include upstream error bodies, URLs or auth values in diagnostics.
            result.put(dict(fallback, reason="决策服务失败或响应无效，保守使用主力组", cost_usd=None))
        finally:
            _slots.release()

    threading.Thread(target=request, daemon=True).start()
    try:
        decision = result.get(timeout=settings["timeout_seconds"])
    except queue.Empty:
        decision = dict(fallback, reason="决策服务超时，保守使用主力组", cost_usd=None)
    decision["duration_ms"] = round((time.monotonic() - start) * 1000)
    return decision


def plan_task(db, task, *, mode="manual", amount=50000, min_tier=1, strategy="cost", max_cost=None):
    if not isinstance(task, str) or not task.strip() or len(task.encode()) > 64000:
        raise ledger.LedgerError("task must contain 1 to 64000 UTF-8 bytes")
    if mode not in ("manual", "auto", "regular", "primary"):
        raise ledger.LedgerError("invalid model selection mode")
    if type(amount) not in (int, float) or not math.isfinite(amount) or amount <= 0:
        raise ledger.LedgerError("positive finite task amount required")
    if type(min_tier) is not int or not 1 <= min_tier <= 5:
        raise ledger.LedgerError("tier must be an integer from 1 to 5")
    if max_cost is not None and (type(max_cost) not in (int, float) or not math.isfinite(max_cost) or max_cost < 0):
        raise ledger.LedgerError("cost estimate limit must be finite and nonnegative")
    config = profiles.load()
    # Validate routing inputs before potentially making a billable decision request.
    if strategy not in ledger.STRATEGIES:
        raise ledger.LedgerError("unknown routing strategy")
    decision = decide(task, config["routing"]) if mode == "auto" else {
        "group": mode if mode in ("regular", "primary") else None,
        "source": "manual", "reason": "使用手动指定的模型要求", "cost_usd": 0, "duration_ms": 0}
    plan = pools.plan_route(db, amount, "tokens", min_tier, strategy, config,
                            required_group=decision["group"])
    if max_cost is not None:
        eligible = []
        for candidate in plan["candidates"]:
            if candidate["est"] is None or candidate["est"] > max_cost:
                plan["skipped"].append(dict(candidate, why="estimated cost unknown or above limit"))
            else:
                eligible.append(candidate)
        plan["candidates"] = eligible
    plan.update(decision=decision, mode=mode, max_cost=max_cost,
                primary_idx=0 if plan["candidates"] else None,
                routed_cost=plan["candidates"][0]["est"] if plan["candidates"] else None)
    plan["saved"] = (plan["payg_only_cost"] - plan["routed_cost"]
                     if plan["payg_only_cost"] is not None and plan["routed_cost"] is not None else None)
    return plan


def public_plan(plan):
    fields = ("pool_id", "account_id", "account", "agent", "channel_id", "model", "group",
              "capability_tier", "kind", "state", "why", "coverage", "remaining", "est", "unit")
    result = {k: plan[k] for k in ("amount", "unit", "min_tier", "routed_cost", "payg_only_cost",
                                  "saved", "decision", "mode", "max_cost")}
    for key in ("candidates", "skipped"):
        result[key] = [{k: c.get(k) for k in fields} for c in plan[key]]
    return result
