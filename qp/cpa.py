"""CLIProxyAPI Management API client.

Docs: https://help.router-for.me/cn/management/api
Base: http://localhost:8317/v0/management (auth: Bearer management key)

qpool treats CLIProxyAPI as the passthrough data plane; this client is the
control-plane hook used by qp/conductor.py to read the credential pool and
push cost-aware scheduling decisions (disable/enable credentials, switch the
selection strategy, reset quota cooldowns) and to drain per-request usage
records into the ledger.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from .collectors.base import CollectorError

DEFAULT_BASE_URL = "http://localhost:8317"
ENV_BASE_URL = "QPOOL_CPA_URL"
ENV_KEY = "QPOOL_CPA_KEY"

STRATEGY_ROUND_ROBIN = "round-robin"
STRATEGY_FILL_FIRST = "fill-first"


class CpaClient:
    def __init__(self, base_url: Optional[str] = None, key: Optional[str] = None) -> None:
        self.base_url = (base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
        self.key = key if key is not None else (os.environ.get(ENV_KEY) or "").strip()

    @classmethod
    def from_env(cls) -> "CpaClient":
        return cls()

    def available(self) -> bool:
        return bool(self.key)

    # ---------- low level ----------

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Any] = None,
        query: Optional[Dict[str, str]] = None,
    ) -> Any:
        if not self.key:
            raise CollectorError(
                f"no CLIProxyAPI management key; set {ENV_KEY} (remote-management.secret-key)")
        url = f"{self.base_url}/v0/management{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.key}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read(1 << 20)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise CollectorError("CLIProxyAPI: invalid management key (401)") from exc
            if exc.code == 403:
                raise CollectorError("CLIProxyAPI: remote management disabled (403)") from exc
            if exc.code == 404:
                raise CollectorError(f"CLIProxyAPI: not found (404) at {path}") from exc
            raise CollectorError(f"CLIProxyAPI: HTTP {exc.code} at {path}") from exc
        except urllib.error.URLError as exc:
            raise CollectorError(
                f"CLIProxyAPI unreachable at {self.base_url}: {exc.reason}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CollectorError(f"CLIProxyAPI: invalid JSON from {path}") from exc

    # ---------- credential pool (read) ----------

    def list_auth_files(self) -> List[Dict[str, Any]]:
        """Full credential pool view: provider/label/status/disabled/success/failed."""
        data = self._request("GET", "/auth-files")
        files = (data or {}).get("files")
        return files if isinstance(files, list) else []

    def get_strategy(self) -> str:
        data = self._request("GET", "/routing/strategy")
        return (data or {}).get("strategy", "")

    def drain_usage_queue(self, count: int = 100) -> List[Dict[str, Any]]:
        """Pop up to `count` per-request usage records (tokens/provider/model/key)."""
        data = self._request("GET", "/usage-queue", query={"count": str(count)})
        return data if isinstance(data, list) else []

    # ---------- scheduling (write) ----------

    def set_auth_disabled(self, name: str, disabled: bool) -> None:
        self._request("PATCH", "/auth-files/status",
                      body={"name": name, "disabled": disabled})

    def set_auth_fields(self, name: str, **fields: Any) -> None:
        """Update metadata fields of a credential (dot-paths supported).

        Used to push scheduling priority. Whether the runtime honors a
        `priority` field for OAuth credentials depends on the CLIProxyAPI
        version; callers should tolerate failures gracefully.
        """
        self._request("PATCH", "/auth-files/fields", body={"name": name, **fields})

    def reset_quota(self, auth_index: str) -> List[str]:
        """Clear runtime quota/cooldown state; credential rejoins routing."""
        data = self._request("POST", "/reset-quota", body={"auth_index": auth_index})
        models = (data or {}).get("models")
        return models if isinstance(models, list) else []

    def set_strategy(self, strategy: str) -> None:
        if strategy not in (STRATEGY_ROUND_ROBIN, STRATEGY_FILL_FIRST):
            raise CollectorError(f"unknown routing strategy: {strategy}")
        self._request("PUT", "/routing/strategy", body={"value": strategy})

    # ---------- OpenAI-compatible upstream providers ----------

    def list_openai_providers(self) -> List[Dict[str, Any]]:
        """Upstream OpenAI-compatible providers (e.g. ark, openrouter)."""
        data = self._request("GET", "/openai-compatibility")
        providers = (data or {}).get("openai-compatibility")
        return providers if isinstance(providers, list) else []

    def upsert_openai_provider(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str,
        models: Optional[List[Dict[str, str]]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> str:
        """Register or update an upstream provider. Returns 'created'|'updated'."""
        entry: Dict[str, Any] = {
            "name": name,
            "base-url": base_url,
            "api-key-entries": [{"api-key": api_key}],
            "models": models or [],
        }
        if headers:
            entry["headers"] = headers
        for p in self.list_openai_providers():
            if p.get("name") == name:
                self._request("PATCH", "/openai-compatibility",
                              body={"name": name, "value": entry})
                return "updated"
        existing = self.list_openai_providers()
        self._request("PUT", "/openai-compatibility", body=existing + [entry])
        return "created"

    def delete_openai_provider(self, name: str) -> None:
        self._request("DELETE", "/openai-compatibility", query={"name": name})
