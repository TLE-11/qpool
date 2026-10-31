"""Official OpenAI API transport. Credentials never enter account snapshots."""
import hashlib
import http.client
import json
from pathlib import Path
import re
import threading
import time

from .collectors.base import CollectorError


def fingerprint(key):
    return hashlib.sha256(key.encode()).hexdigest()


def validate_key(key):
    if not isinstance(key, str) or not re.fullmatch(r'sk-[A-Za-z0-9_-]{16,512}', key):
        raise CollectorError('请输入有效的 OpenAI API Key，不要输入订阅登录令牌')
    return key


def connection(adapter, timeout):
    if isinstance(adapter, APIAdapter):
        return http.client.HTTPSConnection('api.openai.com', timeout=timeout)
    return http.client.HTTPConnection('127.0.0.1', adapter.port, timeout=timeout)


def list_models(key, deadline=None):
    # Fixed destination, no redirects or user-controlled base URL.
    remaining = min(15, deadline-time.monotonic()) if deadline is not None else 15
    if remaining <= 0:
        raise CollectorError('模型目录同步超时')
    conn = http.client.HTTPSConnection('api.openai.com', timeout=remaining)
    try:
        conn.request('GET', '/v1/models', headers={'Authorization': 'Bearer ' + key})
        response = conn.getresponse()
        if response.status != 200:
            raise CollectorError(f'OpenAI 模型目录返回 HTTP {response.status}，请检查 Key 权限和项目状态')
        raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise CollectorError('OpenAI 模型目录超过大小限制')
        data = json.loads(raw)
        models = sorted({m['id'] for m in data['data'] if isinstance(m, dict)
                         and isinstance(m.get('id'), str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,200}', m['id'])})
        if not models:
            raise CollectorError('此 API Key 未返回可用模型')
        return models
    except CollectorError:
        raise
    except Exception:
        raise CollectorError('无法读取 OpenAI 模型目录，请检查网络后重试') from None
    finally:
        conn.close()


class APIAdapter:
    def __init__(self, root, account):
        self.account = account
        self.lock = threading.RLock()
        self.key = ''
        self.models = account.get('model_inventory', {}).get('upstream', [])

    def ensure(self, deadline=None):
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError()
        try:
            path = Path(self.account['source']['credentials_file'])
            if path.is_symlink() or path.stat().st_mode & 0o077:
                raise ValueError()
            key = validate_key(json.loads(path.read_text())['api_key'])
            if fingerprint(key) != self.account['source']['expected_subject']:
                raise ValueError()
            self.key = key
        except Exception:
            raise CollectorError('API 凭据不可用或已变化，请在页面重新接入') from None
        return self

    def refresh_models(self, deadline=None):
        self.ensure(deadline=deadline)
        models = list_models(self.key, deadline=deadline)
        if not self.lock.acquire(timeout=max(0, deadline-time.monotonic()) if deadline is not None else -1):
            raise CollectorError('账号忙，保留上次模型目录')
        try:
            if deadline is not None and time.monotonic() >= deadline:
                raise CollectorError('模型目录同步超时')
            self.models = models
            return models
        finally:
            self.lock.release()

    def close(self):
        self.key = ''
