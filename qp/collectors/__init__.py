"""Quota collectors: detect local agent credentials and poll vendor quota APIs.

Built on officially documented vendor APIs; some credential locations were
identified with reference to the onWatch project (GPL-3.0, external reference
only). All implementations are original Python; no third-party code is copied.
Each collector module exposes:
    AGENT: str
    detect_credentials() -> dict | None
    fetch_readings(creds) -> list[QuotaReading]

`manual` is the config-file fallback for agents without a usable quota API
(kiro, antigravity, opencode, kilo-code, continue, qoder, aider, roo-code,
acme-agent). See qp/collectors/manual.py for the format.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Dict, List

from . import (
    ark,
    claude_code,
    cline,
    codex,
    copilot,
    cursor,
    devin,
    gemini,
    kimi,
    manual,
    windsurf,
    xai,
)
from .base import CollectResult, CollectorError, QuotaReading

COLLECTORS: Dict[str, object] = {
    "codex": codex,
    "claude-code": claude_code,
    "cursor": cursor,
    "gemini-cli": gemini,
    "copilot": copilot,
    "kimi-cli": kimi,
    "cline": cline,
    "grok": xai,
    "doubao": ark,
    "windsurf": windsurf,
    "devin": devin,
    "manual": manual,
}

# Load errors encountered while scanning plugin collectors (surfaced by sync).
PLUGIN_ERRORS: List[str] = []


def plugin_dir() -> Path:
    """User-provided collectors live OUTSIDE the repo.

    Internal/proprietary collectors (company agents, private APIs) go in
    $QPOOL_COLLECTORS_DIR or ~/.qpool/collectors/ and never enter the
    open-source tree. A plugin is any *.py file exposing:
        AGENT: str
        detect_credentials() -> dict | None
        fetch_readings(creds) -> list[QuotaReading]
    """
    env = os.environ.get("QPOOL_COLLECTORS_DIR")
    return Path(env).expanduser() if env else Path.home() / ".qpool" / "collectors"


def load_plugin_collectors() -> Dict[str, object]:
    plugins: Dict[str, object] = {}
    directory = plugin_dir()
    if not directory.is_dir():
        return plugins
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"qpool_plugin_{path.stem}", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # a broken plugin must not break the CLI
            PLUGIN_ERRORS.append(f"{path.name}: {type(exc).__name__}: {exc}")
            continue
        agent = getattr(module, "AGENT", None)
        if (isinstance(agent, str) and agent
                and hasattr(module, "detect_credentials")
                and hasattr(module, "fetch_readings")):
            plugins[agent.strip().lower()] = module
        else:
            PLUGIN_ERRORS.append(f"{path.name}: missing AGENT/detect_credentials/fetch_readings")
    return plugins


COLLECTORS.update(load_plugin_collectors())

__all__ = ["COLLECTORS", "CollectResult", "CollectorError", "QuotaReading",
           "PLUGIN_ERRORS", "plugin_dir"]
