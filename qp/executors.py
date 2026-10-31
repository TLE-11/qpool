"""Execute explicitly bound local profiles. Unverified gateway pinning stays unavailable."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .collectors.base import CollectorError
from . import profiles

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


CLI_EXECUTORS: Dict[str, List[str]] = {}  # legacy mappings are inspected lazily

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
    retry_safe: bool = False


def execution_env(channel: Dict[str, Any], pool: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, str]:
    env = dict(os.environ, **channel.get("env", {}))
    # Ambient API auth/endpoint overrides must not defeat a selected OAuth profile.
    overrides = {"codex": ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_TOKEN", "OPENAI_BASE_URL"),
                 "claude-code": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL")}
    for key in overrides.get(pool["agent"], ()):
        env.pop(key, None)
    source = config["accounts"][pool["account_id"]]["source"]
    env[source["profile_env"]] = str(Path(source["profile_dir"]).expanduser().resolve())
    return env


def channel_status(channel: Dict[str, Any], pool: Dict[str, Any],
                   config: Dict[str, Any]) -> Any:
    if channel.get("pool_id") != pool["id"] or channel.get("account_id") != pool["account_id"]:
        return False, "channel account/pool mismatch"
    if channel.get("type") != "cli":
        return False, "gateway account/funding pinning is not verified in this version"
    if len([p for p in config["pools"].values() if p["account_id"] == pool["account_id"]]) != 1:
        return False, "CLI funding selection is unverified for accounts with multiple pools"
    command = channel.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(v, str) for v in command):
        return False, "CLI command is missing"
    if not any("{task}" in v for v in command) or not any("{model}" in v for v in command):
        return False, "CLI command must bind {task} and {model} explicitly"
    account = config["accounts"][pool["account_id"]]
    source = account["source"]
    env = channel.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        return False, "invalid CLI environment"
    profile_env, directory = source.get("profile_env"), source.get("profile_dir")
    if not profile_env or not directory or not source.get("credentials_file"):
        return False, "CLI needs a credential file and an explicit isolated profile environment"
    root = Path(directory).expanduser().resolve()
    if not env.get(profile_env) or Path(env[profile_env]).expanduser().resolve() != root:
        return False, "CLI environment does not select the account's profile"
    try:
        Path(source["credentials_file"]).expanduser().resolve().relative_to(root)
        # File identity is checked on every plan and once more before process creation.
        profiles.credentials(account, None)
    except (ValueError, CollectorError):
        return False, "CLI credential identity is missing or changed"
    run_env = execution_env(channel, pool, config)
    if not shutil.which(command[0], path=run_env.get("PATH")):
        return False, "CLI executable not found"
    return True, "configured account profile and model are bound; capacity is not reserved"


def execute(candidate: Dict[str, Any], task: str, *, db: Any,
            cwd: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT_S) -> ExecResult:
    """Execute the exact frozen channel, never an agent's ambient default account."""
    from .pools import evaluate

    started = time.time()
    process_started = False

    def failure(reason: str, safe: bool = True) -> ExecResult:
        return ExecResult(candidate["agent"], candidate["channel"]["type"], False,
                          "", reason, 1, time.time() - started, safe)

    try:
        config = profiles.load()
        pool = config["pools"].get(candidate["pool_id"])
        channel = next((c for c in config["channels"] if c["id"] == candidate["channel_id"]), None)
        if (not pool or channel != candidate["channel"]
                or pool != candidate.get("pool_config", {k: candidate[k] for k in pool if k in candidate})):
            return failure("configuration changed since planning; re-plan required")
        verdict = evaluate(db, pool, channel["model"], amount=candidate["task_amount"],
                           unit=candidate["task_unit"])
        ready, reason = channel_status(channel, pool, config)
        if verdict["state"] != "ready" or not ready:
            return failure(verdict["why"] if verdict["state"] != "ready" else reason)
        command = [v.replace("{model}", channel["model"]).replace("{task}", task)
                   for v in channel["command"]]
        env = execution_env(channel, pool, config)
        command[0] = str(Path(shutil.which(command[0], path=env.get("PATH")) or command[0]).resolve())
        # Spool output instead of retaining an unbounded CLI transcript in memory.
        # The local UI only needs a bounded completed-output tail.
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=env, start_new_session=True)
            process_started = True
            deadline = time.monotonic() + timeout
            try:
                while proc.poll() is None:
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(command, timeout)
                    if os.fstat(output.fileno()).st_size > 16 * 1024 * 1024:
                        return failure("CLI output exceeded 16 MiB; inspect workspace before retry", False)
                    time.sleep(0.05)
            finally:
                if proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait()
            size = os.fstat(output.fileno()).st_size
            output.seek(max(0, size - 262144))
            captured = output.read(262144).decode("utf-8", errors="replace")
            if size > 262144:
                captured = "[Earlier output omitted; showing last 256 KiB]\n" + captured
    except (CollectorError, KeyError):
        return failure("execution binding cannot be verified; re-plan required")
    except FileNotFoundError:
        return failure("CLI file disappeared; inspect workspace before retry" if process_started
                       else "CLI executable disappeared before launch", not process_started)
    except subprocess.TimeoutExpired:
        return failure("CLI timed out after starting; inspect workspace before retry", False)
    except OSError:
        return failure("CLI I/O failed after starting; inspect workspace before retry" if process_started
                       else "CLI could not start", not process_started)
    failed = proc.returncode != 0
    return ExecResult(candidate["agent"], "cli", not failed, captured,
                      "CLI failed after starting; inspect workspace before retry" if failed else "",
                      proc.returncode, time.time() - started, False)


def executable_agents() -> List[str]:
    """Legacy configuration remains readable, but is not an account-bound channel."""
    return sorted(_load_cli_executors())
