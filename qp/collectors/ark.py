"""Volcano Engine Ark (Doubao) quota collector.

The cleanest of the officially documented usage APIs (调研: "直接接入，最优").

Docs:
  GetInferenceUsage: https://www.volcengine.com/docs/82379/2116766
  Signing V4:        https://www.volcengine.com/docs/82379/1465834

API: POST https://ark.cn-beijing.volcengineapi.com/?Action=GetInferenceUsage&Version=2024-01-01
     body {StartTime, EndTime, QueryInterval: Day|Hour, Filters[]}
     -> per-interval {InputTokens, OutputTokens, TotalTokens, ReqCnt}
Auth: Volcano signature V4 (HMAC-SHA256 chain, SigV4-style; the control plane
does not accept bearer API keys).

Credentials (env):
  VOLC_ACCESS_KEY_ID / VOLC_SECRET_ACCESS_KEY  - volcano AK/SK pair
  ARK_API_KEY_ID (optional)                    - filter usage to one API key

Note: the signature implementation follows the public docs; verify once with a
real AK/SK before relying on it (mock tests only cover request construction).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

from .base import CollectorError, QuotaReading, credential, http_json

AGENT = "doubao"
CAPABILITY_TIER = 3

BASE_URL = "https://ark.cn-beijing.volcengineapi.com"
REGION = "cn-beijing"
SERVICE = "ark"
ACTION = "GetInferenceUsage"
VERSION = "2024-01-01"


def detect_credentials() -> Optional[Dict[str, Any]]:
    ak = credential("VOLC_ACCESS_KEY_ID")
    sk = credential("VOLC_SECRET_ACCESS_KEY")
    if not ak or not sk:
        return None
    return {
        "access_key": ak,
        "secret_key": sk,
        "api_key_id": credential("ARK_API_KEY_ID") or None,
        "source": "env or ~/.qpool/credentials.json",
    }


# ---------- Volcano signature V4 ----------

def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _quote(value: str) -> str:
    return quote(value, safe="-_.~")


def sign_request(
    method: str,
    query: Dict[str, str],
    body: bytes,
    ak: str,
    sk: str,
    now: datetime,
    host: Optional[str] = None,
) -> Dict[str, str]:
    """Returns the signed headers (Host/X-Date/Authorization/Content-Type)."""
    host = host or urlparse(BASE_URL).netloc
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    short_date = now.strftime("%Y%m%d")

    canonical_query = "&".join(f"{_quote(k)}={_quote(v)}" for k, v in sorted(query.items()))
    signed_headers = "content-type;host;x-date"
    canonical_headers = (
        f"content-type:application/json\nhost:{host}\nx-date:{x_date}\n"
    )
    canonical_request = "\n".join([
        method, "/", canonical_query, canonical_headers, signed_headers, _sha256_hex(body),
    ])
    credential_scope = f"{short_date}/{REGION}/{SERVICE}/request"
    string_to_sign = "\n".join([
        "HMAC-SHA256", x_date, credential_scope, _sha256_hex(canonical_request.encode("utf-8")),
    ])

    # Volcano derives the signing key WITHOUT the "AWS4" secret prefix
    k_date = _hmac_sha256(sk.encode("utf-8"), short_date)
    k_region = _hmac_sha256(k_date, REGION)
    k_service = _hmac_sha256(k_region, SERVICE)
    k_signing = _hmac_sha256(k_service, "request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    return {
        "Content-Type": "application/json",
        "Host": host,
        "X-Date": x_date,
        "Authorization": (
            f"HMAC-SHA256 Credential={ak}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }


# ---------- usage fetch ----------

def fetch_readings(creds: Dict[str, Any]) -> List[QuotaReading]:
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    body_obj: Dict[str, Any] = {
        "StartTime": month_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "EndTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "QueryInterval": "Day",
    }
    if creds.get("api_key_id"):
        body_obj["Filters"] = [{"Key": "ApikeyID", "Values": [creds["api_key_id"]]}]
    body = json.dumps(body_obj).encode("utf-8")

    url = f"{BASE_URL}/?Action={ACTION}&Version={VERSION}"
    headers = sign_request(
        "POST", {"Action": ACTION, "Version": VERSION}, body,
        creds["access_key"], creds["secret_key"], now,
    )
    data = http_json(url, method="POST", headers=headers, body=body)

    result = data.get("Result") if isinstance(data.get("Result"), dict) else data
    items = result.get("UsageList") or result.get("Usages") or result.get("Items") or []
    if not isinstance(items, list):
        raise CollectorError("GetInferenceUsage returned an unexpected shape")

    total_tokens = 0.0
    total_requests = 0.0
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("TotalTokens", "total_tokens"):
            if isinstance(item.get(key), (int, float)):
                total_tokens += item[key]
                break
        for key in ("ReqCnt", "req_cnt"):
            if isinstance(item.get(key), (int, float)):
                total_requests += item[key]
                break

    hint = f"apikey:{creds['api_key_id'][:8]}" if creds.get("api_key_id") else None
    readings = [QuotaReading(
        agent=AGENT,
        window_key="monthly_usage",
        label="ark monthly token usage",
        unit="tokens",
        consumed_abs=total_tokens,
        resets_at=(month_start + timedelta(days=32)).replace(day=1),  # next month 1st
        plan=None,
        account_hint=hint,
        capability_tier=CAPABILITY_TIER,
    )]
    if total_requests:
        readings.append(QuotaReading(
            agent=AGENT,
            window_key="monthly_requests",
            label="ark monthly request count",
            unit="requests",
            consumed_abs=total_requests,
            resets_at=(month_start + timedelta(days=32)).replace(day=1),
            account_hint=hint,
            capability_tier=CAPABILITY_TIER,
        ))
    return readings
