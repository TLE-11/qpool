"""Verify the public CPA subscription catalog before reconciling model removals."""
import json
import re
import time
import urllib.request

from .collectors.base import CollectorError

SOURCE = 'https://raw.githubusercontent.com/router-for-me/models/refs/heads/main/models.json'


def subscription_models(plan, deadline):
    # Match the explicitly supported mappings in CPA 7.3.10. Unknown plans
    # remain unconfirmed instead of inheriting CPA's permissive Pro default.
    plan = plan.strip().lower() if isinstance(plan, str) else ''
    section = {'pro': 'codex-pro', 'plus': 'codex-plus', 'team': 'codex-team',
               'business': 'codex-team', 'free': 'codex-free', 'go': 'codex-team'}.get(plan)
    if section is None:
        raise CollectorError('无法确认订阅类型，保留上次模型目录')
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        # Public metadata only: never attach account tokens to this request.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(SOURCE, timeout=min(10, remaining)) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError()
        items = json.loads(raw)[section]
        if not isinstance(items, list) or not items:
            raise ValueError()
        models = []
        for item in items:
            name = item['id']
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,200}', name):
                raise ValueError()
            models.append(name)
        return sorted(set(models))
    except Exception:
        raise CollectorError('无法确认适配器最新公开目录，保留上次模型列表，请稍后同步') from None
