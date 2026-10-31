"""Managed Codex onboarding and per-account local adapters.

Only access tokens are borrowed from native login files. Refresh tokens remain
owned by Codex, preventing two clients from rotating the same credential.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone

from . import ledger, pools, profiles, routing, decisions, openai_account, model_catalog, desktop
from .collectors import codex
from .collectors.base import CollectorError
from .db import Database


def atomic(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise CollectorError("托管文件不能是符号链接")
    fd, name = tempfile.mkstemp(prefix='.qpool-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            out.write(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def claims(token):
    try:
        part = token.split('.')[1]
        return json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
    except Exception:
        return {}


def read_login(path, expected=None):
    try:
        tokens = json.loads(Path(path).read_text())['tokens']
        if not tokens.get('access_token') or not tokens.get('account_id'):
            raise ValueError()
        if expected and tokens['account_id'] != expected:
            raise ValueError()
        return tokens
    except Exception as exc:
        raise CollectorError("Codex 登录缺失或账号已变化，请重新发现并接入账号") from exc


def metadata(tokens):
    info = claims(tokens.get('id_token', ''))
    auth = info.get('https://api.openai.com/auth', {})
    email = info.get('email', '')
    # Do not expose a full provider identifier or email to the frontend.
    hint = (email[:2] + '…@' + email.split('@')[-1]) if '@' in email else '本机 Codex'
    return {'hint': hint, 'plan': auth.get('chatgpt_plan_type', 'subscription'),
            'identity': digest(tokens['account_id'])[:10]}


class Adapter:
    def __init__(self, root, account):
        self.root = Path(root)
        self.account = account
        self.process = None
        self.signature = None
        self.key = secrets.token_urlsafe(32)
        self.port = None
        self.models = []
        self.lock = threading.RLock()

    def ensure(self, deadline=None):
        with self.lock:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError()
            source = self.account['source']
            tokens = read_login(source['credentials_file'], source['expected_subject'])
            signature = digest(tokens['access_token'])
            if self.process and self.process.poll() is None and signature == self.signature:
                return self
            self.close(deadline=deadline)
            binary = shutil.which('CLIProxyAPI') or shutil.which('cliproxyapi')
            if not binary:
                raise CollectorError("未找到 CLIProxyAPI，请先安装本地 Codex 适配器")
            # Each instance contains exactly one identity, no fallback sources.
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            authdir = self.root / 'auth'
            authdir.mkdir(exist_ok=True, mode=0o700)
            info = claims(tokens.get('id_token', ''))
            atomic(authdir / 'account.json', {'type': 'codex', 'account_id': tokens['account_id'],
                   'access_token': tokens['access_token'], 'id_token': tokens.get('id_token', ''),
                   'email': info.get('email', ''), 'refresh_token': ''})
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                self.port = sock.getsockname()[1]
            atomic(self.root / 'config.yaml', {
                'host': '127.0.0.1', 'port': self.port, 'auth-dir': str(authdir),
                'api-keys': [self.key], 'remote-management': {'secret-key': '', 'disable-control-panel': True},
                'request-retry': 0, 'max-retry-credentials': 1, 'max-retry-interval': 0,
                'quota-exceeded': {'switch-project': False, 'switch-preview-model': False},
                'debug': False, 'request-log': False, 'logging-to-file': False,
                'error-logs-max-files': 1, 'usage-statistics-enabled': False,
                'discovery': {'enabled': False}, 'disable-image-generation': True})
            self.process = subprocess.Popen([binary, '-config', str(self.root / 'config.yaml')],
                                            cwd=self.root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(60):
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if self.process.poll() is not None:
                    break
                try:
                    request = urllib.request.Request(f'http://127.0.0.1:{self.port}/v1/models',
                                                     headers={'Authorization': 'Bearer ' + self.key})
                    with opener.open(request, timeout=min(1, max(.001, deadline-time.monotonic())) if deadline is not None else 1) as response:
                        data = json.loads(response.read(262144))
                    self.models = sorted({m['id'] for m in data.get('data', [])
                                          if re.fullmatch(r'gpt-[a-zA-Z0-9.\-]+', m.get('id', ''))
                                          and 'image' not in m['id']})
                    if self.models:
                        self.signature = signature
                        return self
                except Exception:
                    pass
                time.sleep(.1)
            self.close(deadline=deadline)
            raise CollectorError("账号适配器未能加载模型，请检查 CLIProxyAPI 版本和 Codex 登录")

    def refresh_models(self, deadline=None):
        deadline = deadline if deadline is not None else time.monotonic() + 20
        tokens = read_login(self.account['source']['credentials_file'], self.account['source']['expected_subject'])
        # Public network lookup must not occupy an execution account lock.
        expected = model_catalog.subscription_models(metadata(tokens)['plan'], deadline)
        if not self.lock.acquire(timeout=max(0, deadline-time.monotonic())):
            raise CollectorError('账号忙，保留上次模型目录')
        try:
            self.ensure(deadline=deadline)
            request = urllib.request.Request(f'http://127.0.0.1:{self.port}/v1/models',
                                             headers={'Authorization': 'Bearer ' + self.key})
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                while time.monotonic() < deadline:
                    with opener.open(request, timeout=min(1, max(.001, deadline-time.monotonic()))) as response:
                        raw = response.read(1048577)
                    if len(raw) > 1048576:
                        raise ValueError()
                    models = {m['id'] for m in json.loads(raw)['data']}
                    if set(expected) <= models:
                        self.models = expected
                        return expected
                    time.sleep(min(.2, max(0, deadline-time.monotonic())))
            except Exception:
                raise CollectorError('读取账号模型目录失败，保留上次同步结果') from None
            raise CollectorError('适配器尚未加载最新目录，保留上次列表，请稍后同步')
        finally:
            self.lock.release()

    def close(self, deadline=None):
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=min(3, max(0, deadline-time.monotonic())) if deadline is not None else 3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process = None
            # CPA error files can contain request data. Never retain adapter files.
            if self.root.exists():
                shutil.rmtree(self.root)


class Native:
    def __init__(self, db_path, lock):
        self.db_path = Path(db_path)
        self.root = profiles.config_path().resolve().parent / 'native'
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.service_lock = open(self.root / 'service.lock', 'a')
        os.chmod(self.root / 'service.lock', 0o600)
        try:
            fcntl.flock(self.service_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.service_lock.close()
            raise CollectorError("此配置目录已有 qpool 原生网关运行，请使用现有控制台") from exc
        self.path = self.root / 'routes.json'
        self.lock = lock
        self.adapter_lock = threading.Lock()
        self.adapters = {}
        self.discovered = {}
        self.port = None
        self.logins = {}
        self.model_sync_lock = threading.Lock()
        self.models_stop = threading.Event()
        self.models_thread = None
        self.slots = threading.BoundedSemaphore(4)
        self.selection_lock = threading.Lock()
        self.bindings = {}
        self.turn_locks = {}
        self.decision_engine = decisions.Engine(self)
        self.receipt_path = str(self.root / 'sessions.db')
        with self.receipts() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS requests
              (id TEXT PRIMARY KEY, session TEXT, route TEXT, account TEXT, model TEXT,
               decision TEXT, status TEXT, started REAL, duration REAL, input_tokens INTEGER,
               output_tokens INTEGER, error TEXT, actual_model TEXT)''')
            if 'decision_id' not in {r['name'] for r in conn.execute('PRAGMA table_info(requests)')}:
                conn.execute('ALTER TABLE requests ADD COLUMN decision_id TEXT')
            conn.execute('''CREATE TABLE IF NOT EXISTS decisions
                (id TEXT PRIMARY KEY, created REAL, route TEXT, session TEXT, scope TEXT, detail TEXT)''')
            conn.execute('CREATE INDEX IF NOT EXISTS requests_session ON requests(session)')
            conn.execute('CREATE TABLE IF NOT EXISTS legacy_bindings (session TEXT PRIMARY KEY, account TEXT NOT NULL, route TEXT NOT NULL, binding TEXT)')
            conn.execute('CREATE TABLE IF NOT EXISTS session_bindings (session TEXT PRIMARY KEY, route TEXT NOT NULL, binding TEXT NOT NULL)')
            conn.execute("UPDATE requests SET status='interrupted', error='服务重启，结果未确认' WHERE status='running'")
        os.chmod(self.receipt_path, 0o600)
        # Grants are invalidated on restart until user enables a route again.
        self.grant = None
        self.active = None
        self.audit_error = None
        self.desktop_error = None

    @contextmanager
    def receipts(self):
        conn = sqlite3.connect(self.receipt_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def data(self):
        return json.loads(self.path.read_text()) if self.path.exists() else {'routes': []}

    def adapter(self, account, *, initialize=True, deadline=None):
        wait = max(0, deadline-time.monotonic()) if deadline is not None else -1
        if not self.adapter_lock.acquire(timeout=wait):
            raise TimeoutError()
        try:
            aid = account['id']
            if aid in self.adapters and self.adapters[aid].account['source'] != account['source']:
                if not initialize:
                    raise CollectorError('账号引用已变化，请重新关联登录')
                self.adapters.pop(aid).close()
            if aid not in self.adapters:
                cls = openai_account.APIAdapter if account['agent'] == 'openai_api' else Adapter
                self.adapters[aid] = cls(self.root / 'adapters' / digest(aid)[:16], account)
            adapter = self.adapters[aid]
        finally:
            self.adapter_lock.release()
        return adapter.ensure() if initialize else adapter

    def connect_api(self, data):
        if set(data) - {'api_key', 'label', 'consent'} or data.get('consent') is not True:
            raise CollectorError('请确认在本机保存 Key，并理解 API 调用单独计费')
        key = openai_account.validate_key(data.get('api_key'))
        label = data.get('label', 'OpenAI API')
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 60 or 'sk-' in label:
            raise CollectorError('请填写 1–60 字的账号名称，不要把密钥填入名称')
        models = openai_account.list_models(key)
        identity = openai_account.fingerprint(key)
        aid = 'openai-' + identity[:16]
        path = self.root / 'api-keys' / (aid + '.json')
        account = {'id': aid, 'agent': 'openai_api', 'label': label.strip(), 'paid_enabled': True,
                   'source': {'credentials_file': str(path), 'subject_field': 'identity',
                              'expected_subject': identity, 'fields': {'api_key': 'api_key', 'identity': 'identity'}},
                   'pools': [{'id': aid + '-api', 'kind': 'payg', 'unit': 'tokens', 'billing': 'provider',
                              'cost_per_unit': 0, 'capability_tier': 5, 'windows': []}]}
        with self.lock:
            raw = json.loads(profiles.config_path().read_text()) if profiles.config_path().exists() else {'accounts': [], 'channels': []}
            self.merge_models(raw, account, models)
            raw['accounts'] = [a for a in raw['accounts'] if a['id'] != aid] + [account]
            config = profiles.load(raw)
            atomic(path, {'api_key': key, 'identity': identity})
            db = Database(self.db_path)
            try:
                pools.register(db, config)
                atomic(profiles.config_path(), raw)
            finally:
                db.close()
        return {'connected': aid, 'models': account['model_inventory']['selectable']}

    def merge_models(self, raw, account, upstream):
        known = self.model_metadata()
        is_api = account['agent'] == 'openai_api'
        selectable = sorted(m for m in upstream if m in known and known[m].get('visibility', 'list') == 'list'
                            and (not is_api or known[m].get('supported_in_api', True)))
        available = set(selectable)
        # Keep IDs referenced by routes, but revoke eligibility when a model disappears.
        for channel in raw['channels']:
            if channel['account_id'] == account['id'] and channel.get('type') == 'native':
                channel['available'] = channel['model'] in available
        for model in selectable:
            cid = account['id'] + '-' + model
            if any(c['id'] == cid for c in raw['channels']):
                continue
            regular = any(word in model for word in ('luna', 'spark', 'mini'))
            raw['channels'].append({'id': cid, 'type': 'native', 'account_id': account['id'],
                'pool_id': account['pools'][0]['id'], 'model': model, 'available': True,
                'group': 'regular' if regular else 'primary', 'capability_tier': 2 if regular else 5})
        missing = sorted(set(known) - set(upstream))
        excluded = [{'model': m, 'reason': '上游目录未提供' if m in missing else
                     'Codex 隐藏模型' if m in known and known[m].get('visibility') == 'hide' else
                     '当前 Codex 无匹配能力信息或不支持 API'}
                    for m in sorted((set(upstream) | set(missing)) - available)]
        account['model_inventory'] = {'upstream': upstream, 'selectable': selectable, 'excluded': excluded,
                                      'synced_at': time.time(), 'error': None}

    def sync_models(self, aid=None):
        if not self.model_sync_lock.acquire(blocking=False):
            raise CollectorError('模型目录正在同步，请稍候')
        results, errors = [], []
        try:
            accounts = profiles.load()['accounts']
            if aid and aid not in accounts:
                raise CollectorError('账号不存在')
            for account in accounts.values():
                if self.models_stop.is_set():
                    break
                if account['agent'] not in ('codex', 'openai_api') or aid and account['id'] != aid:
                    continue
                try:
                    adapter = self.adapter(account)
                    models = adapter.refresh_models()
                    with self.lock:
                        if self.models_stop.is_set():
                            break
                        raw = json.loads(profiles.config_path().read_text())
                        current = next(a for a in raw['accounts'] if a['id'] == account['id'])
                        if current['source'] != account['source']:
                            raise CollectorError('登录引用已变化，请重新同步')
                        self.merge_models(raw, current, models)
                        profiles.load(raw)
                        atomic(profiles.config_path(), raw)
                    results.append({'account_id': account['id'], 'models': current['model_inventory']['selectable']})
                except Exception:
                    errors.append({'account_id': account['id'], 'error': '同步失败，保留上次目录；请检查登录、网络或 Key 权限'})
            if results:
                with self.lock:
                    if self.active and not self.models_stop.is_set():
                        try:
                            self.catalog(self.active)
                        except CollectorError:
                            # A removed route model is already marked unavailable. Keep
                            # the last valid alias metadata until the route is repaired.
                            pass
            return {'synced': results, 'errors': errors}
        finally:
            self.model_sync_lock.release()

    def start_model_sync(self):
        def worker():
            while not self.models_stop.wait(30):
                try:
                    self.sync_models()
                except Exception:
                    pass
                if self.models_stop.wait(270):
                    break
        self.models_thread = threading.Thread(target=worker, daemon=True)
        self.models_thread.start()

    def discover(self):
        homes = {Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser()}
        homes.update((self.root / 'logins').glob('*'))
        for account in profiles.load()['accounts'].values():
            if account['agent'] == 'codex' and account['source'].get('credentials_file'):
                homes.add(Path(account['source']['credentials_file']).expanduser().parent)
        configured = {a['source']['expected_subject']: a for a in profiles.load()['accounts'].values()
                      if a['agent'] == 'codex'}
        result = []
        for home in sorted(homes):
            path = home / 'auth.json'
            try:
                tokens = read_login(path)
            except CollectorError:
                continue
            sid = digest(str(path.resolve()) + tokens['account_id'])[:20]
            self.discovered[sid] = (path.resolve(), tokens['account_id'])
            existing = configured.get(tokens['account_id'])
            linked = bool(existing and Path(existing['source'].get('credentials_file', '')).expanduser().resolve() == path.resolve())
            result.append({'id': sid, **metadata(tokens), 'location': str(home),
                           'connected': linked, 'reconnect': bool(existing and not linked)})
        return {'sources': result}

    def connect(self, data):
        sid = data.get('source_id')
        if sid not in self.discovered:
            raise CollectorError("请先点击发现本机账号，再选择接入")
        path, subject = self.discovered[sid]
        tokens = read_login(path, subject)
        meta = metadata(tokens)
        aid = 'codex-' + digest(subject)[:12]
        account = {'id': aid, 'agent': 'codex', 'label': meta['hint'] + ' · ' + meta['plan'],
                   'source': {'credentials_file': str(path), 'subject_field': 'account_id',
                              'expected_subject': subject, 'fields': {'access_token': 'tokens.access_token',
                              'account_id': 'tokens.account_id'}, 'profile_env': 'CODEX_HOME', 'profile_dir': str(path.parent)},
                   'pools': []}
        observed = ledger.now_utc()
        readings = codex.fetch_readings({'access_token': tokens['access_token'], 'account_id': subject})
        limits = [r for r in readings if r.used_percent is not None and r.window_key != 'credits']
        if not limits:
            raise CollectorError("未取得订阅额度窗口，暂不能启用账号")
        adapter = self.adapter(account)
        known_models = self.model_metadata()
        available_models = [m for m in adapter.models if m in known_models]
        if not available_models:
            raise CollectorError("尚无匹配的 Codex 模型能力目录，请先启动原 Codex 更新目录，再重新接入")
        read_login(path, subject)  # login must remain bound throughout onboarding
        pid = aid + '-subscription'
        account['pools'] = [{'id': pid, 'kind': 'subscription', 'unit': 'requests',
                             'capability_tier': 5, 'max_age_seconds': 180,
                             'windows': [{'key': r.window_key} for r in limits]}]
        with self.lock:
            configpath = profiles.config_path()
            raw = json.loads(configpath.read_text()) if configpath.exists() else {'accounts': [], 'channels': []}
            existing = next((a for a in raw['accounts'] if a['agent'] == 'codex'
                             and a['source']['expected_subject'] == subject), None)
            if existing:
                if existing['id'] != aid:
                    raise CollectorError("此账号已有旧版配置，请使用其已绑定的登录目录")
                if existing['source'] == account['source']:
                    raise CollectorError("此账号已经接入，可直接配置路由")
                raw['accounts'] = [a for a in raw['accounts'] if a['id'] != aid]
            raw['accounts'].append(account)
            self.merge_models(raw, account, adapter.models)
            config = profiles.load(raw)
            db = Database(self.db_path)
            try:
                pools.register(db, config)
                outcome = pools.apply_snapshot(db, config, aid, readings, observed)
                if outcome.get('incomplete'):
                    raise CollectorError("额度窗口不完整，请重新接入")
                atomic(configpath, raw)
            finally:
                db.close()
        return {'connected': aid, 'models': account['model_inventory']['selectable']}

    def start_login(self):
        binary = shutil.which('codex')
        if not binary:
            raise CollectorError("未找到 Codex CLI")
        with self.lock:
            if any(p.poll() is None for p, _ in self.logins.values()):
                raise CollectorError("已有登录窗口等待完成，请先完成该登录")
            ident = secrets.token_hex(8)
            home = self.root / 'logins' / ident
            home.mkdir(parents=True, mode=0o700)
            # Native browser login; never collect passwords, cookies or MFA in our UI.
            process = subprocess.Popen([binary, 'login'], env={**os.environ, 'CODEX_HOME': str(home)},
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.logins[ident] = (process, time.monotonic())
        return {'id': ident, 'status': 'waiting', 'message': '请在 Codex 打开的浏览器中完成登录，然后点击重新发现。'}

    def save_route(self, data):
        name = data.get('name', '').strip()
        mode = data.get('mode')
        if not name or len(name) > 60 or mode not in ('fixed', 'local', 'decider', 'model'):
            raise CollectorError("填写路由名称并选择有效策略")
        config = profiles.load()
        channels = {c['id']: c for c in config['channels'] if c.get('type') == 'native' and c.get('available', True)}
        groups = {}
        for key in ('regular', 'primary'):
            ids = data.get(key, [])
            if not isinstance(ids, list) or any(not isinstance(i, str) or i not in channels for i in ids):
                raise CollectorError("请选择已经接入的账号和模型")
            if len(set(ids)) != len(ids):
                raise CollectorError("模型不可重复")
            groups[key] = ids
        if not groups['primary'] or mode != 'fixed' and not groups['regular']:
            raise CollectorError("请选择主力模型；自动路由还需选择常规模型")
        if set(groups['primary']) & set(groups['regular']):
            raise CollectorError("同一账号模型不能同时属于两组")
        decision_settings = decisions.validate(data.get('decision')) if mode == 'model' else None
        allow_api = data.get('allow_api', False)
        if type(allow_api) is not bool:
            raise CollectorError('请选择是否允许 API 计费')
        chosen = groups['regular'] + groups['primary']
        if decision_settings and decision_settings['provider'] == 'account':
            chosen = chosen + [decision_settings['channel_id']]
        if not allow_api and any(config['accounts'][channels[c]['account_id']]['agent'] == 'openai_api' for c in chosen):
            raise CollectorError('此方案包含 API 模型，请勾选允许 API 计费后保存')
        if mode == 'decider' and not config['routing']['endpoint']:
            raise CollectorError("尚未配置决策服务，请先使用本地规则或固定模型")
        if mode != 'fixed':
            regular_accounts = {channels[c]['account_id'] for c in groups['regular']}
            primary_accounts = {channels[c]['account_id'] for c in groups['primary']}
            if not regular_accounts & primary_accounts:
                raise CollectorError("自动路由的两组至少需要一个共同账号，避免会话中途迁移账号")
        with self.lock:
            raw = self.data()
            ident = data.get('id') or secrets.token_hex(6)
            if data.get('id') and ident not in {r['id'] for r in raw['routes']}:
                raise CollectorError("路由不存在")
            route = {'id': ident, 'name': name, 'mode': mode, 'allow_api': allow_api, **groups}
            if decision_settings is not None:
                route['decision'] = decision_settings
            raw['routes'] = [r for r in raw['routes'] if r['id'] != ident] + [route]
            self.catalog(route, write=False)
            atomic(self.path, raw)
            # Editing an active route revokes its entry until explicitly enabled.
            if self.active and self.active['id'] == ident and self.active != route:
                self.disable()
        return route

    def model_metadata(self):
        config = profiles.load()
        homes = {Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser()}
        homes.update(Path(a['source']['profile_dir']) for a in config['accounts'].values()
                     if a['agent'] == 'codex' and a['source'].get('profile_dir'))
        known = {}
        # Deterministic precedence: use the newest local catalog per model.
        def freshness(home):
            try:
                return (home / 'models_cache.json').stat().st_mtime
            except OSError:
                return 0
        for home in sorted(homes, key=lambda p: (freshness(p), str(p))):
            try:
                known.update({m['slug']: m for m in json.loads((home / 'models_cache.json').read_text())['models']})
            except (OSError, ValueError, KeyError):
                continue
        return known

    def catalog(self, route, write=True):
        config = profiles.load()
        ids = set(route['primary'] + (route['regular'] if route['mode'] != 'fixed' else []))
        if any(c.get('available') is False for c in config['channels'] if c['id'] in ids):
            raise CollectorError('方案含已从上游目录移除的模型，请重新选择')
        models = {c['model'] for c in config['channels'] if c['id'] in ids}
        known = self.model_metadata()
        if models - known.keys():
            raise CollectorError("所选模型缺少本机 Codex 能力信息，请选择当前 Codex 模型目录中的模型")
        entries = [known[m] for m in sorted(models)]
        if not entries:
            raise CollectorError("路由中没有可用模型")
        for field in ('shell_type', 'apply_patch_tool_type', 'web_search_tool_type'):
            if len({str(e.get(field)) for e in entries}) > 1:
                raise CollectorError("所选模型的工具协议不一致，请分别建立固定路由")
        common = set.intersection(*[{r['effort'] for r in e['supported_reasoning_levels']} for e in entries])
        if 'medium' not in common:
            raise CollectorError("所选模型不支持共同的 medium 推理档位")
        model = dict(entries[0], slug='qpool-auto', display_name='qpool auto',
                     description='Automatic model routing. Existing OpenAI chats keep their original account through the desktop gateway.', upgrade=None,
                     context_window=min(e['context_window'] for e in entries),
                     max_context_window=min(e.get('max_context_window', e['context_window']) for e in entries),
                     supported_reasoning_levels=[r for r in entries[0]['supported_reasoning_levels'] if r['effort'] in common],
                     input_modalities=sorted(set.intersection(*[set(e['input_modalities']) for e in entries])),
                     default_reasoning_level='medium', visibility='list', additional_speed_tiers=[], service_tiers=[])
        if write:
            # This is a global Codex catalog, also used by existing OpenAI threads.
            # Route membership must only constrain routing, never the original picker.
            atomic(self.root / 'catalog.json', {'models': [model] +
                   [entry for slug, entry in known.items() if slug != 'qpool-auto']})

    def enable(self, ident):
        if self.audit_error:
            raise CollectorError(self.audit_error)
        with self.lock:
            route = next((r for r in self.data()['routes'] if r['id'] == ident), None)
            if not route or not self.port:
                raise CollectorError("请选择已保存的路由")
            if route['mode'] == 'model':
                decisions.validate(route.get('decision'))
            self.catalog(route)
            # Enabling an unchanged plan is idempotent, including its grant.
            # Persisted bindings are history, not disposable entry credentials:
            # retain them even when switching plans so restoring a plan restores
            # its sessions. select() checks the saved route fingerprint.
            if self.active == route and self.grant:
                return self.status()
            self.active = route
            self.grant = secrets.token_urlsafe(32)
            self.write_entry()
            desktop.persist_route(self)
            self.desktop_error = None
        return self.status()

    def write_entry(self):
        atomic(self.root / 'entry.json', {'token': self.grant, 'port': self.port, 'route': self.active['id'],
                                        'codex': shutil.which('codex'), 'home': os.environ.get('CODEX_HOME')})
        atomic(self.root / 'codex-qpool', ENTRY_SCRIPT, 0o700)

    def launch(self, data):
        if not self.active:
            raise CollectorError("请先启用路由")
        cwd = Path(data.get('cwd', '')).expanduser().resolve()
        if not cwd.is_dir():
            raise CollectorError("工作目录不存在")
        if sys.platform != 'darwin':
            raise CollectorError("此系统请复制启动命令到终端执行")
        path = self.root / 'launch-codex.command'
        atomic(path, '#!/bin/sh\ncd -- ' + shlex.quote(str(cwd)) + '\nexec ' +
               shlex.quote(str(self.root / 'codex-qpool')) + '\n', 0o700)
        subprocess.run(['/usr/bin/open', '-a', 'Terminal', str(path)], check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {'opened': True}

    def disable(self):
        self.grant = None
        self.active = None
        self.bindings.clear()
        (self.root / 'entry.json').unlink(missing_ok=True)
        (self.root / 'desktop-route.json').unlink(missing_ok=True)
        return {'disabled': True}

    def status(self):
        with self.receipts() as conn:
            rows = [dict(row) for row in conn.execute('SELECT * FROM requests ORDER BY started DESC LIMIT 100')]
            legacy_sessions = {row['session'] for row in conn.execute('SELECT session FROM legacy_bindings')}
            judgments = [dict(json.loads(row['detail']), id=row['id'], created=row['created'],
                         route=row['route'], session=row['session'], scope=row['scope']) for row in
                         conn.execute('SELECT * FROM decisions ORDER BY created DESC LIMIT 100')]
        for row in rows:
            row['account_policy'] = ('原生审批：保留登录账号，不参与 Plus 优先' if row['route'] == 'Codex native approval'
                                     else '原生聊天：保留原账号，不参与 Plus 优先' if row['session'] in legacy_sessions
                                     else '自动路由：首次按方案顺序选账号，后续保持该账号')
        for ident, (process, since) in list(self.logins.items()):
            if process.poll() is None and time.monotonic() - since > 600:
                process.terminate()
        return {'routes': self.data()['routes'], 'active': self.active, 'audit_error': self.audit_error,
                'entry_command': shlex.quote(str(self.root / 'codex-qpool')) if self.active else None,
                'codex_installed': bool(shutil.which('codex')),
                'adapter_installed': bool(shutil.which('CLIProxyAPI') or shutil.which('cliproxyapi')),
                'api_key_directory': str(self.root / 'api-keys'),
                'desktop': desktop.status(self),
                'sessions': rows, 'decisions': judgments}

    def refresh(self, aid, *, deadline=None):
        db = Database(self.db_path)
        try:
            result = pools.sync_accounts(db, apply=True, account_id=aid, deadline=deadline)
            if result['errors']:
                raise CollectorError("账号额度刷新失败，请在账号页重新同步或登录")
        finally:
            db.close()

    def record_decision(self, result, route, session, scope='route'):
        ident = secrets.token_hex(12)
        try:
            with self.receipts() as conn:
                conn.execute('INSERT INTO decisions VALUES (?,?,?,?,?,?)',
                    (ident, time.time(), route, session, scope, json.dumps(result, ensure_ascii=False)))
        except sqlite3.Error as exc:
            self.audit_error = '决策记录写入失败，请检查磁盘与权限后重启服务'
            self.disable()
            raise CollectorError(self.audit_error) from exc
        return dict(result, id=ident)

    def test_decision(self, data):
        text = data.get('task')
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 16000:
            raise CollectorError('测试任务须为 1 至 16000 字节的文字')
        settings = decisions.validate(data.get('decision'))
        if settings['provider'] == 'account':
            config = profiles.load()
            channel = next(c for c in config['channels'] if c['id'] == settings['channel_id'])
            if config['accounts'][channel['account_id']]['agent'] == 'openai_api' and data.get('allow_api') is not True:
                raise CollectorError('测试此决策模型会产生 API 用量，请先勾选允许 API 计费')
        result = self.decision_engine.run(settings, text,
                                         [{'role': 'user', 'content': text}])
        return self.record_decision(result, '页面测试', '', 'test')

    @contextmanager
    def turn_lock(self, key):
        # Serialize duplicate requests of one session without blocking other sessions.
        with self.selection_lock:
            slot = self.turn_locks.setdefault(key, [threading.Lock(), 0])
            slot[1] += 1
        try:
            with slot[0]:
                yield
        finally:
            with self.selection_lock:
                slot[1] -= 1
                if slot[1] == 0:
                    self.turn_locks.pop(key, None)

    def select(self, body, headers, compact=False, legacy_account=None):
        """Pin tool continuations; never silently replay a request on another model."""
        route = self.active
        if not route:
            raise CollectorError("Codex 入口已停用，请在控制台启用路由")
        if body.get('previous_response_id'):
            raise CollectorError("此入口需要完整对话上下文，不支持 previous_response_id")
        session = headers.get('session_id') or headers.get('thread-id') or headers.get('x-codex-session-id') or body.get('prompt_cache_key')
        if not isinstance(session, str) or not session or len(session) > 512:
            raise CollectorError("请求缺少 Codex 会话标识")
        sid = digest(session)[:16]
        requested = body.get('model')
        manual = legacy_account and requested not in (None, 'qpool-auto')
        items = body.get('input', [])
        if not isinstance(items, list):
            raise CollectorError("需要原生 Codex 的消息列表")
        users = [i for i in items if isinstance(i, dict) and i.get('role') == 'user']
        text = ''
        if users:
            content = users[-1].get('content', '')
            text = content if isinstance(content, str) else '\n'.join(c.get('text', '') for c in content
                      if isinstance(c, dict) and c.get('type') in ('input_text', 'text'))
        key = (self.grant, route['id'], sid)
        turn = digest(str(len(users)) + ':' + text)
        # Desktop may append tool catalogs or runtime instructions after tool
        # output; those metadata messages do not start a new user turn.
        tail = next((i for i in reversed(items) if isinstance(i, dict)
                     and i.get('role') not in ('developer', 'system')), None)
        continuation = compact or bool(tail and
            (tail.get('type') in ('function_call_output', 'custom_tool_call_output',
                                 'computer_call_output', 'tool_search_output') or tail.get('role') == 'tool'))
        with self.turn_lock(key):
            verified_legacy = False
            if not legacy_account:
                with self.receipts() as conn:
                    if conn.execute('SELECT 1 FROM legacy_bindings WHERE session=?', (sid,)).fetchone():
                        raise CollectorError('此聊天已绑定原生入口，请使用原入口继续')
            binding = self.bindings.get(key)
            if not legacy_account and not binding:
                with self.receipts() as conn:
                    saved = conn.execute('SELECT * FROM session_bindings WHERE session=?', (sid,)).fetchone()
                if saved:
                    if saved['route'] != digest(json.dumps(route, sort_keys=True)):
                        raise CollectorError('此聊天的路由设置已变化，请恢复原路由后继续')
                    binding = json.loads(saved['binding'])
            if legacy_account:
                if binding and binding['channel']['account_id'] != legacy_account:
                    raise CollectorError('此聊天已绑定其他账号，拒绝跨账号继续')
                with self.receipts() as conn:
                    routed = conn.execute('SELECT binding FROM session_bindings WHERE session=?', (sid,)).fetchone()
                    if routed:
                        raise CollectorError('此聊天已绑定 qpool 专用入口，请使用原入口继续')
                    saved = conn.execute('SELECT * FROM legacy_bindings WHERE session=?', (sid,)).fetchone()
                    if saved:
                        if saved['account'] != legacy_account or (saved['binding'] and saved['route'] != route['id']):
                            raise CollectorError('旧聊天已绑定原账号与路由，请恢复对应账号和路由后继续')
                        if not binding and saved['binding']:
                            binding = json.loads(saved['binding'])
                        if manual and binding and 'user_count' not in binding:
                            if requested != binding['channel']['model'] and (compact or binding['turn'] == turn):
                                raise CollectorError('旧绑定的同轮或压缩请求不能更换模型')
                            # Old versions saved no turn boundary. Re-establish
                            # only the authenticated account and explicit model.
                            binding = None
                            verified_legacy = True
                    else:
                        if conn.execute('SELECT 1 FROM requests WHERE session=? LIMIT 1', (sid,)).fetchone():
                            raise CollectorError('此聊天已有其他入口的执行记录，不能自动重新绑定')
            if not binding:
                with self.receipts() as conn:
                    seen = conn.execute('SELECT 1 FROM requests WHERE session=? LIMIT 1', (sid,)).fetchone()
                if seen and not verified_legacy:
                    raise CollectorError('此会话的路由绑定已失效，请新建会话；不会跨账号迁移已有上下文')
            if binding and (continuation or binding['turn'] == turn) and (not manual or requested == binding['channel']['model']):
                if binding.get('user_count') != len(users) or binding['turn'] != turn:
                    binding = dict(binding, turn=turn, user_count=len(users))
                    self.store_binding(key, route, sid, binding, legacy_account)
                return binding, sid
            # A resumed native chat may end with historical browser/tool results.
            # Only an authenticated native account and an explicit real model can
            # seed this first binding; an automatic alias cannot infer that model.
            native_resume = legacy_account and manual and not binding and bool(text)
            native_model_change = (legacy_account and manual and binding and not compact
                                   and len(users) > binding.get('user_count', len(users)))
            if (continuation and not native_resume and not native_model_change) or not text:
                raise CollectorError("未找到此轮会话的路由绑定，请新建会话后重试")
            judgment = None
            if manual:
                group, why = 'primary', '旧聊天：保持原账号与指定模型'
            elif route['mode'] == 'fixed':
                group, why = 'primary', '固定模型'
            elif route['mode'] == 'model':
                judgment = self.record_decision(self.decision_engine.run(route['decision'], text, items), route['name'], sid)
                group, why = judgment['group'], judgment['reason']
            elif route['mode'] == 'decider':
                decision = routing.decide(text, profiles.load()['routing'])
                group, why = decision['group'], decision['reason']
            else:
                # Conservative local fallback, explicitly not an LLM classifier.
                simple = len(text) <= 800 and bool(re.search(r'翻译|translate|总结|摘要|summari[sz]e|润色|改写|拼写|格式化|你好|hello|只回复|reply.*only', text, re.I))
                complex_task = bool(re.search(r'架构|重构|安全|并发|迁移|实现|修复|debug|implement|refactor|security|design|terminal|shell|tool|运行|执行|文件|代码|算法', text, re.I))
                group = 'regular' if simple and not complex_task else 'primary'
                why = '本地规则：简单文字任务' if group == 'regular' else '本地规则：复杂或不确定任务走主力'
            config = profiles.load()
            ids = route['primary'] if group == 'primary' else route['regular'] + route['primary']
            if manual:
                ids = [c['id'] for c in config['channels'] if c['account_id'] == legacy_account and c['model'] == requested]
            by_id = {c['id']: c for c in config['channels']}
            account_order = list(dict.fromkeys(by_id[i]['account_id'] for i in ids if i in by_id))
            channels = [c for c in config['channels'] if c['id'] in ids and c.get('type') == 'native' and c.get('available', True)]
            primary_accounts = {c['account_id'] for c in channels if c['id'] in route['primary']}
            channels = [c for c in channels if (manual or c['account_id'] in primary_accounts)
                        and (not legacy_account or c['account_id'] == legacy_account)]
            candidates = []
            subscription_unknown = False
            refreshed = set()
            db = Database(self.db_path)
            try:
                for c in channels:
                    pool = config['pools'][c['pool_id']]
                    account = config['accounts'][c['account_id']]
                    if account['agent'] == 'openai_api':
                        if route.get('allow_api') and account.get('paid_enabled') is True:
                            candidates.append(c)
                        continue
                    if pool['kind'] != 'subscription':
                        continue
                    verdict = pools.evaluate(db, pool, c['model'])
                    if verdict['state'] == 'unknown' and c['account_id'] not in refreshed:
                        refreshed.add(c['account_id'])
                        try:
                            self.refresh(c['account_id'])
                        except CollectorError:
                            subscription_unknown = True
                            continue
                        verdict = pools.evaluate(db, pool, c['model'])
                    if verdict['state'] == 'unknown':
                        subscription_unknown = True
                    if verdict['state'] == 'ready':
                        candidates.append(c)
            finally:
                db.close()
            if not candidates:
                raise CollectorError("没有符合路由的可用账号，请同步额度、模型或检查 API 计费许可")
            if binding:
                candidates = [c for c in candidates if c['account_id'] == binding['channel']['account_id']]
                if not candidates:
                    raise CollectorError("当前会话的账号额度或模型不可用，请新建会话以重新选择账号")
            # Account priority wins over regular-vs-primary preference. A usable
            # Plus primary must precede Pro regular when Plus was ordered first.
            candidates.sort(key=lambda c: (config['accounts'][c['account_id']]['agent'] == 'openai_api',
                                           account_order.index(c['account_id']), ids.index(c['id'])))
            selected = candidates[0]
            if config['accounts'][selected['account_id']]['agent'] == 'openai_api' and subscription_unknown:
                raise CollectorError('订阅额度尚未确认，暂不转入付费 API；请稍后重试额度同步')
            binding = {'turn': turn, 'user_count': len(users), 'channel': selected, 'decision': why, 'route': route['name'],
                       'decision_id': judgment['id'] if judgment else None}
            self.store_binding(key, route, sid, binding, legacy_account)
            return binding, sid

    def store_binding(self, key, route, sid, binding, legacy_account):
        with self.selection_lock:
            if key[0] == self.grant:
                if legacy_account:
                    with self.receipts() as conn:
                        conn.execute('INSERT INTO legacy_bindings VALUES (?,?,?,?) ON CONFLICT(session) DO UPDATE SET route=excluded.route, binding=excluded.binding',
                                     (sid, legacy_account, route['id'], json.dumps(binding)))
                else:
                    with self.receipts() as conn:
                        conn.execute('INSERT OR REPLACE INTO session_bindings VALUES (?,?,?)',
                                     (sid, digest(json.dumps(route, sort_keys=True)), json.dumps(binding)))
                if len(self.bindings) >= 1024:
                    self.bindings.pop(next(iter(self.bindings)))
                self.bindings[key] = binding

    def approval_binding(self, body, headers, account_id):
        """Native approval keeps its exact hidden model and signed-in account."""
        account = profiles.load()['accounts'].get(account_id)
        model = 'codex-auto-review'
        if not account or account['agent'] != 'codex' or body.get('model') != model:
            raise CollectorError('原生审批账号或模型不匹配')
        if model not in account.get('model_inventory', {}).get('upstream', []):
            raise CollectorError('原账号未提供原生审批模型，请同步模型目录')
        session = headers.get('session_id') or headers.get('thread-id') or headers.get('x-codex-session-id') or body.get('prompt_cache_key')
        if not isinstance(session, str) or not 0 < len(session) <= 512:
            raise CollectorError('原生审批缺少会话标识')
        return {'channel': {'account_id': account_id, 'model': model},
                'route': 'Codex native approval', 'decision': '原账号原模型审批'}, digest('approval:' + session)[:16]

    def validate_binding(self, channel, *, allow_refresh=True, deadline=None):
        config = profiles.load()
        current = next((c for c in config['channels'] if c['id'] == channel['id']), None)
        if (current is None or current.get('available') is False or
                dict(current, available=current.get('available', True)) !=
                dict(channel, available=channel.get('available', True))):
            raise CollectorError("路由模型配置已变化，请新建会话")
        account = config['accounts'][channel['account_id']]
        if account['agent'] == 'openai_api':
            if account.get('paid_enabled') is not True:
                raise CollectorError('此 API 账号未启用付费调用')
            self.adapter(account, initialize=False, deadline=deadline).ensure(deadline=deadline)
            return
        pool = config['pools'][channel['pool_id']]
        db = Database(self.db_path)
        try:
            result = pools.evaluate(db, pool, channel['model'])
            if result['state'] == 'unknown' and allow_refresh:
                self.refresh(channel['account_id'], deadline=deadline)
                result = pools.evaluate(db, pool, channel['model'])
            if result['state'] != 'ready':
                raise CollectorError("会话绑定账号的额度不可用，请同步额度或新建会话")
        finally:
            db.close()

    def close(self):
        self.models_stop.set()
        if self.models_thread:
            # Do not release the process/file lock while a worker can still mutate
            # config or construct an adapter. Each transport has its own timeout.
            self.models_thread.join()
        # Desktop opt-in alone permits recovery of the active route after a restart.
        # Explicit disable/edit/audit failure still removes the persistent grant.
        if (self.root / 'desktop-route.json').exists():
            self.active = self.grant = None
            self.bindings.clear()
        else:
            self.disable()
        for process, _ in self.logins.values():
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for adapter in self.adapters.values():
            adapter.close()
        self.service_lock.close()


ENTRY_SCRIPT = '''#!/usr/bin/env python3
import json, os, pathlib, sys, urllib.request
root = pathlib.Path(__file__).resolve().parent
try:
    entry = json.loads((root / 'entry.json').read_text())
except (OSError, ValueError):
    sys.exit('qpool 入口未启用，请在控制台选择路由并启用。')
if not entry.get('codex'):
    sys.exit('未找到 Codex CLI，请安装后重新启用。')
env = dict(os.environ, QPOOL_ACCESS_TOKEN=entry['token'])
if entry.get('home'):
    env['CODEX_HOME'] = entry['home']
provider = {'name':'qpool', 'base_url':'http://127.0.0.1:%s/v1' % entry['port'],
            'wire_api':'responses', 'requires_openai_auth':False, 'supports_websockets':False,
            'env_key':'QPOOL_ACCESS_TOKEN', 'request_max_retries':0, 'stream_max_retries':0}
# JSON strings are also TOML strings; inline table syntax differs for separators.
value = '{' + ','.join(k + '=' + json.dumps(v) for k,v in provider.items()) + '}'
args = [entry['codex'], '--no-daemon', '-c', 'model_provider="qpool"', '-c', 'model_providers.qpool='+value,
        '-c', 'model="qpool-auto"', '-c', 'model_reasoning_effort="medium"',
        '-c', 'model_catalog_json='+json.dumps(str(root / 'catalog.json'))]
# Options stay on the native CLI; no shell interpolation or prompt reconstruction.
os.execve(entry['codex'], args + sys.argv[1:], env)
'''
