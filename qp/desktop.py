"""Opt-in desktop provider, reversible config editing and on-demand service startup."""
from __future__ import annotations

import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import plistlib
import secrets
import hmac
import subprocess
import sys
import time
import sqlite3

from .collectors.base import CollectorError

KEYS = ('model', 'model_provider', 'model_catalog_json', 'model_reasoning_effort')
PROVIDER = 'qpool-desktop'
OFFICIAL_COMPATIBILITY = {
    'name': 'OpenAI (restored chat compatibility)',
    'base_url': 'https://chatgpt.com/backend-api/codex', 'wire_api': 'responses',
    'requires_openai_auth': True, 'supports_websockets': False,
    'request_max_retries': 0, 'stream_max_retries': 0}


def read_state(root):
    path = Path(root) / 'desktop.json'
    return json.loads(path.read_text()) if path.exists() else None


def parser():
    try:
        import tomlkit
        return tomlkit
    except ImportError as exc:
        raise CollectorError('桌面接入需要 tomlkit，请安装最新版 qpool 及其依赖') from exc


def read_config(path):
    if path.is_symlink():
        raise CollectorError('Codex 配置是符号链接，未修改；请先使用普通配置文件')
    text = path.read_text() if path.exists() else ''
    try:
        return text, parser().parse(text)
    except ValueError as exc:
        raise CollectorError('Codex 配置不是有效 TOML，未修改') from exc


def runtime_id(root):
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:24]


def status(native):
    state = read_state(native.root)
    result = {'connected': False, 'can_restore': bool(state),
              'config_path': str(Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'config.toml'),
              'startup': '登录后自动维护后台', 'error': getattr(native, 'desktop_error', None)}
    if state:
        result.update(config_path=state['config_path'], backup_path=state['backup_path'])
        builtin = state.get('transport') == 'builtin-openai'
        result['needs_upgrade'] = not builtin or not state.get('legacy_enabled')
        try:
            _, doc = read_config(Path(state['config_path']))
            result['connected'] = (all(doc.get(k) == v for k, v in state['managed'].items()
                                       if k != 'model_reasoning_effort') and
                                   (not doc.get('model_providers', {}).get('openai') if builtin else
                                    doc.get('model_providers', {}).get(state.get('provider_id', 'qpool')) == state['provider']))
            if not result['connected']:
                result['error'] = 'Codex 配置已变化，当前未完整使用 qpool 设置'
        except (OSError, ValueError, CollectorError):
            result['error'] = '无法确认 Codex 桌面配置'
    return result


def connect(native, *, install_background=True):
    """Preserve the native provider identity required by current Codex engines."""
    from .native import atomic
    if not native.active or not native.grant:
        raise CollectorError('请先启用要用于桌面的路由方案')
    if sys.platform != 'darwin':
        raise CollectorError('当前桌面接入仅支持 macOS')
    path = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser() / 'config.toml'
    original, doc = read_config(path)
    previous = read_state(native.root)
    if previous:
        if previous.get('transport') != 'builtin-openai':
            raise CollectorError('旧版桌面接入需先点击“恢复官方配置”，再重新接入；不会自动改写聊天提供方')
        if not status(native)['connected']:
            raise CollectorError('已有桌面接入记录且配置已变化，请先恢复原配置后重新接入')
        if install_background:
            install_startup(native.root, previous)
        persist_route(native)
        return status(native)
    url = f'http://127.0.0.1:{native.port}/legacy/v1'
    if doc.get('openai_base_url') not in (None, url):
        raise CollectorError('已有其他 OpenAI 网关地址，未覆盖')
    if doc.get('profile') or doc.get('model_provider') not in (None, 'openai'):
        raise CollectorError('请先在 Codex 中恢复官方默认提供方，再接入；未覆盖当前配置')
    if 'openai' in doc.get('model_providers', {}):
        raise CollectorError('配置含保留的 openai 提供方定义，未覆盖；请先修复 Codex 配置')
    backup = native.root / 'codex-config.before-desktop.toml'
    managed = {'model': 'qpool-auto', 'model_provider': 'openai', 'openai_base_url': url,
               'model_catalog_json': str(native.root / 'catalog.json'),
               'model_reasoning_effort': doc.get('model_reasoning_effort', 'medium')}
    atomic(backup, original)
    for key, value in managed.items():
        doc[key] = value
    rendered = parser().dumps(doc)
    from . import profiles
    state = {'transport': 'builtin-openai', 'legacy_enabled': True, 'config_path': str(path),
             'backup_path': str(backup), 'managed': managed, 'provider_id': 'openai', 'provider': {},
             'python': sys.executable, 'code_root': str(Path(__file__).resolve().parent.parent),
             'db_path': str(Path(native.db_path).resolve()), 'pool_config': str(profiles.config_path().resolve()),
             'port': native.port, 'path_env': os.environ.get('PATH', '')}
    atomic(native.root / 'desktop.json', state)
    try:
        persist_route(native)
        if (path.read_text() if path.exists() else '') != original:
            raise CollectorError('Codex 配置刚被其他操作修改，未覆盖；请重试')
        atomic(path, rendered)
        if install_background:
            install_startup(native.root, state)
    except Exception:
        if path.exists() and path.read_text() == rendered:
            atomic(path, original)
        (native.root / 'desktop.json').unlink(missing_ok=True)
        (native.root / 'desktop-route.json').unlink(missing_ok=True)
        raise
    return status(native)


def _connect_custom_provider(native):
    """Pre-0.166 transport retained for compatibility tests and recovery only."""
    from .native import atomic
    if not native.active or not native.grant:
        raise CollectorError('请先启用要用于桌面的路由方案')
    if sys.platform != 'darwin':
        raise CollectorError('当前桌面接入仅支持 macOS')
    path = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser() / 'config.toml'
    original, doc = read_config(path)
    legacy_url = f'http://127.0.0.1:{native.port}/legacy/v1'
    if doc.get('openai_base_url') not in (None, legacy_url):
        raise CollectorError('已有其他 OpenAI 网关地址，未覆盖；旧聊天入口未启用')
    previous = read_state(native.root)
    if previous:
        if not status(native)['connected']:
            raise CollectorError('已有桌面接入记录且配置已变化，请先恢复原配置后重新接入')
        if previous['provider'].get('requires_openai_auth'):
            install_startup(native.root, previous)
            persist_route(native)
            enable_legacy(native)
            return status(native)
    provider_id = PROVIDER
    existing = doc.get('model_providers', {}).get(PROVIDER)
    if (not previous or previous.get('provider_id', 'qpool') != PROVIDER) and existing is not None:
        if existing == OFFICIAL_COMPATIBILITY:
            # Restored chats must keep their official transport. A new opt-in
            # gets a new provider ID instead of redirecting those old chats.
            provider_id = PROVIDER + '-' + secrets.token_hex(6)
        else:
            raise CollectorError('Codex 已存在非本次管理的 qpool 提供方，未覆盖')
    if doc.get('profile'):
        raise CollectorError('Codex 正在使用配置 profile，暂不覆盖；请先在 Codex 中切回默认配置')
    managed = {'model': 'qpool-auto', 'model_provider': provider_id,
               'openai_base_url': legacy_url,
               'model_catalog_json': str(native.root / 'catalog.json'),
               'model_reasoning_effort': doc.get('model_reasoning_effort', 'medium')}
    local_key = secrets.token_urlsafe(32)
    provider = {'name': 'qpool auto', 'base_url': f'http://127.0.0.1:{native.port}/v1',
                'wire_api': 'responses', 'supports_websockets': False,
                'request_max_retries': 0, 'stream_max_retries': 0,
                'requires_openai_auth': True,
                'http_headers': {'X-Qpool-Desktop-Token': local_key}}
    # Only a local gateway secret enters TOML. Native OAuth remains owned by Codex.
    code_root = str(Path(__file__).resolve().parent.parent)
    backup = native.root / 'codex-config.before-desktop.toml'
    if not previous:
        atomic(backup, original)
    for key, value in managed.items():
        doc[key] = value
    if previous and previous.get('provider_id', 'qpool') != PROVIDER:
        # Upgrade the original shared provider without leaving auth behind to
        # deep-merge with the CLI wrapper's independent provider configuration.
        doc.get('model_providers', {}).pop(previous.get('provider_id', 'qpool'), None)
    doc.setdefault('model_providers', parser().table())[provider_id] = provider
    rendered = parser().dumps(doc)
    # Parse before writing; TOML editing preserves unrelated values and comments.
    parser().parse(rendered)
    state = {'legacy_enabled': True, 'config_path': str(path), 'backup_path': str(backup), 'managed': managed, 'provider_id': provider_id,
             'provider': provider, 'python': sys.executable, 'code_root': code_root,
             'db_path': str(Path(native.db_path).resolve()), 'pool_config': str(native.root.parent / 'pools.json'),
             'port': native.port, 'path_env': os.environ.get('PATH', '')}
    from . import profiles
    state['pool_config'] = str(profiles.config_path().resolve())
    old_route = (native.root / 'desktop-route.json').read_text() if previous else None
    atomic(native.root / 'desktop.json', state)
    try:
        atomic(native.root / 'desktop-key', local_key)
        persist_route(native)
        if (path.read_text() if path.exists() else '') != original:
            raise CollectorError('Codex 配置刚被其他操作修改，未覆盖；请重试')
        atomic(path, rendered)
        install_startup(native.root, state)
    except Exception:
        # Revert only our own write, never a concurrent user's edit.
        if path.exists() and path.read_text() == rendered:
            atomic(path, original)
        if previous:
            atomic(native.root / 'desktop.json', previous)
            if old_route is not None:
                atomic(native.root / 'desktop-route.json', old_route)
        else:
            (native.root / 'desktop.json').unlink(missing_ok=True)
            (native.root / 'desktop-route.json').unlink(missing_ok=True)
        (native.root / 'desktop-key').unlink(missing_ok=True)
        raise
    return status(native)


def enable_legacy(native):
    """Use the supported built-in endpoint override; never edit thread databases."""
    from .native import atomic
    state = read_state(native.root)
    path = Path(state['config_path'])
    original, doc = read_config(path)
    url = f'http://127.0.0.1:{native.port}/legacy/v1'
    if doc.get('openai_base_url') not in (None, url):
        raise CollectorError('已有其他 OpenAI 网关地址，未覆盖；旧聊天入口未启用')
    doc['openai_base_url'] = url
    state['managed'] = dict(state['managed'], openai_base_url=url)
    state['legacy_enabled'] = True
    rendered = parser().dumps(doc)
    if path.read_text() != original:
        raise CollectorError('Codex 配置刚被其他操作修改，未覆盖')
    atomic(path, rendered)
    try:
        atomic(native.root / 'desktop.json', state)
    except Exception:
        if path.read_text() == rendered:
            atomic(path, original)
        raise


def recovery_sessions(root):
    """Find owned aliases even if selection failed before its first receipt."""
    path = Path(root) / 'sessions.db'
    if not path.exists():
        raise CollectorError('会话账本缺失，保留接入配置与备份')
    sessions = {}
    try:
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'requests', 'session_bindings', 'legacy_bindings'} <= tables:
                raise ValueError('Incomplete session ledger')
            sessions.update(conn.execute('SELECT session,model FROM requests ORDER BY started'))
            bound = set()
            for table in ('session_bindings', 'legacy_bindings'):
                for sid, raw in conn.execute(f'SELECT session,binding FROM {table}'):
                    if sid in bound:
                        raise ValueError('Conflicting entry bindings')
                    bound.add(sid)
                    if raw is None:
                        if sid not in sessions:
                            raise ValueError('No model for owned session')
                        continue
                    # A binding may be newer than the latest request receipt.
                    sessions[sid] = json.loads(raw)['channel']['model']
        if any(not isinstance(model, str) or not model.strip() or model.startswith('qpool')
               for model in sessions.values()):
            raise ValueError('No real model for owned session')
    except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        raise CollectorError('无法确认已绑定聊天的恢复模型，保留接入配置与备份') from exc
    return sessions


def restore(native):
    from .native import atomic
    state = read_state(native.root)
    if not state:
        return status(native)
    path = Path(state['config_path'])
    current, doc = read_config(path)
    original, before = read_config(Path(state['backup_path']))
    # Preserve settings edited after connection, but never overwrite conflicting edits.
    for key, value in state['managed'].items():
        if key == 'model_reasoning_effort':
            continue  # Codex writes the user's chosen effort back to this preference.
        if doc.get(key) not in (value, before.get(key)):
            raise CollectorError('Codex 的模型设置已在外部修改，未覆盖；原配置备份保留在接入状态中')
    providers = doc.get('model_providers', {})
    provider_id = state.get('provider_id', 'qpool')
    builtin = state.get('transport') == 'builtin-openai'
    if (builtin and provider_id in providers) or (not builtin and providers.get(provider_id) not in (None, state['provider'])):
        raise CollectorError('qpool 提供方已在外部修改，未覆盖；请检查原配置备份')
    for key in state['managed']:
        if key == 'model_reasoning_effort' and doc.get(key) != state['managed'].get(key):
            continue
        if key in before:
            doc[key] = before[key]
        else:
            doc.pop(key, None)
    if not builtin:
        providers.pop(provider_id, None)
    if not providers and 'model_providers' not in before:
        doc.pop('model_providers', None)
    # Exact restoration when unchanged; otherwise retain unrelated later edits.
    from .codex_recovery import recover
    # Built-in transport changed no provider IDs, but qpool-auto is persisted
    # per chat. Bindings exist before receipts, including failed first turns.
    sessions = recovery_sessions(native.root) if builtin else None
    recovered = recover(path.parent, provider_id, before.get('model'), native.root,
                        before.get('model_provider', 'openai'),
                        **({'managed_sessions': sessions, 'require_owned_aliases': True} if builtin else {}))
    compatibility = getattr(recovered, 'compatibility_provider', False)
    if compatibility:
        # Keep only the ID required by older chats, not the local gateway or its
        # token. Defaults remain the original official provider. Real OAuth
        # inference against this endpoint is covered by the isolated QA probe.
        doc.setdefault('model_providers', parser().table())[provider_id] = dict(OFFICIAL_COMPATIBILITY)
    rendered = original if doc.unwrap() == before.unwrap() else parser().dumps(doc)
    if path.read_text() != current:
        raise CollectorError('Codex 配置刚被其他操作修改，未覆盖；请重试')
    atomic(path, rendered)
    remove_startup(native.root)
    (native.root / 'desktop-key').unlink(missing_ok=True)
    (native.root / 'desktop.json').unlink()
    (native.root / 'desktop-route.json').unlink(missing_ok=True)
    native.desktop_error = None
    return dict(status(native), compatibility_provider=provider_id if compatibility else None)


def authorized(native, headers):
    """Desktop OAuth is not a gateway credential; require a separate local secret."""
    supplied = headers.get('X-Qpool-Desktop-Token', '')
    if not supplied or not native.grant:
        return False
    try:
        expected = (native.root / 'desktop-key').read_text()
        return len(expected) >= 32 and hmac.compare_digest(supplied.encode(), expected.encode())
    except OSError:
        return False


def legacy_account(native, headers):
    """Authenticate the built-in provider against an imported subscription identity."""
    from . import profiles
    from .collectors import codex
    state = read_state(native.root)
    if not state or not state.get('legacy_enabled') or not native.grant:
        return None
    supplied = headers.get('Authorization', '').encode()
    workspace = headers.get('ChatGPT-Account-Id') or headers.get('chatgpt-account-id')
    matches = []
    for account in profiles.load()['accounts'].values():
        if account['agent'] != 'codex':
            continue
        try:
            creds = profiles.credentials(account, codex)
            if hmac.compare_digest(supplied, ('Bearer ' + creds['access_token']).encode()):
                if not workspace or workspace == creds.get('account_id'):
                    matches.append(account['id'])
        except (CollectorError, KeyError):
            continue
    return matches[0] if len(matches) == 1 else None


def startup_path(root):
    label = 'com.qpool.desktop.' + runtime_id(root)
    return Path.home() / 'Library' / 'LaunchAgents' / (label + '.plist'), label


def install_startup(root, state):
    """A user LaunchAgent replaces command auth, which disables ChatGPT account UI."""
    from .native import atomic
    root = Path(root)
    helper = root / 'desktop-service.py'
    atomic(helper, 'import sys\nsys.path.insert(0, ' + repr(state['code_root']) + ')\n' +
           'from qp.desktop import maintain\nmaintain(' + repr(str(root)) + ')\n')
    path, label = startup_path(root)
    definition = {'Label': label, 'ProgramArguments': [state['python'], str(helper)],
                  'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
                  'WorkingDirectory': state['code_root']}
    rendered = plistlib.dumps(definition).decode()
    if path.is_symlink() or (path.exists() and path.read_text() != rendered):
        raise CollectorError('qpool 自动启动文件已在外部修改，未覆盖')
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    atomic(path, rendered)
    domain = f'gui/{os.getuid()}'
    if subprocess.run(['launchctl', 'print', domain + '/' + label], capture_output=True).returncode == 0:
        return
    result = subprocess.run(['launchctl', 'bootstrap', domain, str(path)], capture_output=True)
    if result.returncode:
        if not existed:
            path.unlink(missing_ok=True)
        raise CollectorError('无法启用 qpool 用户后台服务，桌面配置已回滚')


def remove_startup(root):
    path, label = startup_path(root)
    if not path.exists():
        return
    result = subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}/' + label], capture_output=True)
    if result.returncode and subprocess.run(
            ['launchctl', 'print', f'gui/{os.getuid()}/' + label], capture_output=True).returncode == 0:
        raise CollectorError('无法停止 qpool 自动启动服务，请重试恢复')
    path.unlink()


def maintain(root):
    """Keep the authorized local service available; never enable a disabled route."""
    while read_state(root):
        try:
            token(root)
        except (CollectorError, OSError, ValueError, KeyError):
            pass  # Retry after transient service shutdown; never log credentials.
        time.sleep(5)


def persist_route(native):
    from .native import atomic
    if read_state(native.root):
        atomic(native.root / 'desktop-route.json', {'route': native.active, 'token': native.grant,
                                                   'port': native.port})


def resume(native):
    path = native.root / 'desktop-route.json'
    if not path.exists():
        return
    try:
        if not status(native)['connected']:
            raise CollectorError('桌面配置不匹配，请恢复或重新接入')
        saved = json.loads(path.read_text())
        if saved['port'] != native.port or saved['route'] not in native.data()['routes']:
            raise CollectorError('路由或端口已变化，请在页面重新启用')
        if not isinstance(saved['token'], str) or len(saved['token']) < 32:
            raise CollectorError('桌面入口凭据无效，请重新启用')
        native.catalog(saved['route'])
        native.active, native.grant = saved['route'], saved['token']
        native.write_entry()
    except (OSError, ValueError, KeyError, CollectorError):
        native.active = native.grant = None
        native.desktop_error = '桌面路由未自动恢复，请在控制台检查并重新启用路由'


def health(root, port):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
    try:
        conn.request('GET', '/api/desktop/health', headers={'X-Qpool-Request': '1'})
        response = conn.getresponse()
        body = response.read(4096)
        if response.status != 200:
            raise CollectorError('qpool 端口上运行的服务版本不匹配，请更新并重启后台')
        data = json.loads(body)
        if data.get('runtime') != runtime_id(root):
            raise CollectorError('qpool 端口已被其他配置占用，未发送凭据')
        return data
    finally:
        conn.close()


def token(root):
    root = Path(root)
    state = read_state(root)
    if not state or not (root / 'desktop-route.json').exists():
        raise CollectorError('qpool 桌面路由已停用，请在控制台启用')
    # Serialize simultaneous desktop auth refreshes without starting duplicate servers.
    with open(root / 'desktop-start.lock', 'a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            live = health(root, state['port'])
        except (ConnectionError, TimeoutError, OSError):
            env = dict(os.environ, QPOOL_CONFIG=state['pool_config'], QPOOL_DB=state['db_path'],
                       CODEX_HOME=str(Path(state['config_path']).parent), PATH=state['path_env'])
            log_path = root / 'desktop-service.log'
            if log_path.is_symlink():
                raise CollectorError('qpool 后台日志路径不能是符号链接')
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'a') as log:
                process = subprocess.Popen([state['python'], '-m', 'qp', 'web', '--port', str(state['port'])],
                    cwd=state['code_root'], env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    start_new_session=True)
            until = time.monotonic() + 20
            while True:
                try:
                    live = health(root, state['port'])
                    break
                except (ConnectionError, TimeoutError, OSError):
                    if process.poll() is not None or time.monotonic() >= until:
                        raise CollectorError('qpool 后台未能启动，请打开本地控制台检查')
                    time.sleep(0.1)
        if not live.get('active'):
            raise CollectorError('qpool 路由未启用，请在本地控制台启用')
        entry = json.loads((root / 'entry.json').read_text())
        return entry['token']


def print_token(root):
    try:
        print(token(root))
    except Exception:
        # The protocol requires a token on stdout; errors must never include credentials.
        print('qpool 未就绪，请打开本地控制台检查路由与桌面接入状态。', file=sys.stderr)
        raise SystemExit(1)
