"""Executors: actually dispatch a task to the selected agent.

This is the execution half of cost-aware routing (the other half being
plan_route's candidate chain). Two channel types:

- CLI headless: agent CLIs that support non-interactive print mode,
  configured per-user in ~/.qpool/executors.json (the repo ships no
  agent-to-CLI mappings). Usage flows back into the ledger on the next
  `quota sync --apply` via transcript collectors.
- gateway: OpenAI-compatible upstreams behind CLIProxyAPI (e.g. doubao/ark).
  Usage flows back via `quota cpa pull-usage`.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .collectors.base import CollectorError, http_json

def _load_cli_executors() -> Dict[str, List[str]]:
    """CLI executors from ~/.qpool/executors.json.

    The repo ships NO agent-to-CLI mappings: which harnesses you have
    installed is your personal configuration, not project knowledge.

    Format:
        {"cli": {"<agent-name>": ["<cli-command>", "-p"],
                 "another-agent": ["agent-cli", "-p"]}}
    """
    executors: Dict[str, List[str]] = {}
    path = Path.home() / ".qpool" / "executors.json"
    if not path.is_file():
        return executors
    try:
        custom = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return executors
    for agent, cmd in (custom.get("cli") or {}).items():
        if isinstance(cmd, list) and cmd:
            executors[str(agent).strip().lower()] = [str(c) for c in cmd]
    return executors


CLI_EXECUTORS = _load_cli_executors()

# Substrings in output that mean "this candidate is unusable right now"
# (quota exhausted, auth broke, rate limited) -> fail over to the next one.
FAIL_PATTERNS = (
    "authentication required",
    "usage limit",
    "rate limit",
    "quota exceeded",
    "insufficient",
    "credits exhausted",
    "402",
    "429",
)

DEFAULT_TIMEOUT_S = 900


@dataclass
class ExecResult:
    agent: str
    channel: str               # cli | gateway
    success: bool
    output: str
    error: str
    returncode: int
    duration_s: float


def _looks_failed(text: str) -> bool:
    lowered = text.lower()
    return any(p in lowered for p in FAIL_PATTERNS)


def _run_cli(agent: str, task: str, cwd: Optional[str], timeout: int) -> ExecResult:
    cmd = CLI_EXECUTORS[agent] + [task]
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=cwd or None, timeout=timeout,
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        return ExecResult(agent, "cli", False, "",
                          f"{cmd[0]} not found on PATH", 127, time.time() - started)
    except subprocess.TimeoutExpired:
        return ExecResult(agent, "cli", False, "",
                          f"timeout after {timeout}s", 124, time.time() - started)
    output = (proc.stdout or "") + (proc.stderr or "")
    failed = proc.returncode != 0 or _looks_failed(output)
    return ExecResult(
        agent, "cli", not failed, proc.stdout or "",
        (proc.stderr or output)[-400:] if failed else "",
        proc.returncode, time.time() - started,
    )


def _gateway_model_for(cpa: Any, agent: str) -> Optional[str]:
    """Pick the first registered model alias for an openai-compat upstream."""
    for p in cpa.list_openai_providers():
        if p.get("name") == agent:
            for m in p.get("models") or []:
                alias = m.get("alias") or m.get("name")
                if alias:
                    return alias
    return None


def _run_gateway(agent: str, task: str, timeout: int) -> ExecResult:
    from .cpa import CpaClient

    started = time.time()
    cpa = CpaClient.from_env()
    if not cpa.available():
        return ExecResult(agent, "gateway", False, "",
                          "no CLIProxyAPI management key (QPOOL_CPA_KEY)", 0,
                          time.time() - started)
    model = _gateway_model_for(cpa, agent)
    if not model:
        return ExecResult(agent, "gateway", False, "",
                          f"no registered model for upstream '{agent}'; "
                          f"run `qpool quota cpa register-provider` first", 0,
                          time.time() - started)
    api_keys = cpa._request("GET", "/api-keys") or {}
    keys = api_keys.get("api-keys") or []
    if not keys:
        return ExecResult(agent, "gateway", False, "",
                          "CLIProxyAPI has no downstream api-keys configured", 0,
                          time.time() - started)
    try:
        data = http_json(
            f"{cpa.base_url}/v1/chat/completions",
            method="POST",
            headers={
                "Authorization": f"Bearer {keys[0]}",
                "Content-Type": "application/json",
            },
            body=json.dumps({
                "model": model,
                "messages": [{"role": "user", "content": task}],
            }).encode("utf-8"),
            timeout=timeout,
        )
    except CollectorError as exc:
        return ExecResult(agent, "gateway", False, "", str(exc), 0,
                          time.time() - started)
    choices = data.get("choices") or []
    text = ""
    if choices:
        text = ((choices[0].get("message") or {}).get("content")) or ""
    failed = _looks_failed(text) if not text else False
    return ExecResult(agent, "gateway", not failed, text,
                      "" if text else "empty completion", 0, time.time() - started)


def execute(agent: str, task: str, *, cwd: Optional[str] = None,
            timeout: int = DEFAULT_TIMEOUT_S) -> ExecResult:
    """Dispatch `task` to `agent` over its execution channel."""
    if agent in CLI_EXECUTORS:
        return _run_cli(agent, task, cwd, timeout)
    return _run_gateway(agent, task, timeout)


def executable_agents() -> List[str]:
    """Agents with a known execution channel (CLI executors; gateway upstreams
    are validated at runtime)."""
    return sorted(CLI_EXECUTORS)
