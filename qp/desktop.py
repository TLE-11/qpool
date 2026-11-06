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
import shlex
import subprocess
import sys
import time

from .collectors.base import CollectorError

KEYS = ('model', 'model_provider', 'model_catalog_json', 'model_reasoning_effort')
PROVIDER = 'qpool-desktop'
OFFICIAL_COMPATIBILITY = {
    'name': 'OpenAI (restored qpool chats)',
    'base_url': 'https://chatgpt.com/backend-api/codex',
    'wire_api': 'responses', 'requires_openai_auth': True, 'supports_websockets': False,
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
    report_path = native.root / 'desktop-recovery.json'
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text())
            result['legacy_resume_command'] = report['resume_command']
            result['legacy_recovered_count'] = len(report['thread_ids'])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    if state:
        result.update(config_path=state['config_path'], backup_path=state['backup_path'])
        result['hot_switch'] = bool(state.get('hot_switch'))
        result['switch_mode'] = state.get('switch_mode')
        result['needs_upgrade'] = not state.get('hot_switch') and (not state['provider'].get('requires_openai_auth', False) or not state.get('legacy_enabled'))
        try:
            _, doc = read_config(Path(state['config_path']))
            result['connected'] = (all(doc.get(k) == v for k, v in state['managed'].items()
                                       if k != 'model_reasoning_effort') and
                                   (state.get('hot_switch') or doc.get('model_providers', {}).get(state.get('provider_id', 'qpool')) == state['provider']))
            if not result['connected']:
                result['error'] = 'Codex 配置已变化，当前未完整使用 qpool 设置'
        except (OSError, ValueError, CollectorError):
            result['error'] = '无法确认 Codex 桌面配置'
    return result


def connect(native):
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
    if previous and previous.get('hot_switch'):
        raise CollectorError('已接入常驻入口，请使用原生/qpool切换按钮；未改写提供方')
    if previous:
        if not status(native)['connected']:
            raise CollectorError('已有桌面接入记录且配置已变化，请先恢复原配置后重新接入')
        if previous['provider'].get('requires_openai_auth'):
            install_startup(native.root, previous)
            persist_route(native)
            enable_legacy(native)
            return status(native)
    if (not previous or previous.get('provider_id', 'qpool') != PROVIDER) and PROVIDER in doc.get('model_providers', {}):
        raise CollectorError('Codex 已存在非本次管理的 qpool 提供方，未覆盖')
    if doc.get('profile'):
        raise CollectorError('Codex 正在使用配置 profile，暂不覆盖；请先在 Codex 中切回默认配置')
    managed = {'model': 'qpool-auto', 'model_provider': PROVIDER,
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
    doc.setdefault('model_providers', parser().table())[PROVIDER] = provider
    rendered = parser().dumps(doc)
    # Parse before writing; TOML editing preserves unrelated values and comments.
    parser().parse(rendered)
    state = {'legacy_enabled': True, 'config_path': str(path), 'backup_path': str(backup), 'managed': managed, 'provider_id': PROVIDER,
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
    if not state.get('hot_switch') and providers.get(provider_id) not in (None, state['provider']):
        raise CollectorError('qpool 提供方已在外部修改，未覆盖；请检查原配置备份')
    for key in state['managed']:
        if key == 'model_reasoning_effort' and doc.get(key) != state['managed'].get(key):
            continue
        if key in before:
            doc[key] = before[key]
        else:
            doc.pop(key, None)
    if not state.get('hot_switch'):
        providers.pop(provider_id, None)
    if not providers and 'model_providers' not in before:
        doc.pop('model_providers', None)
    report_path = native.root / 'desktop-recovery.json'
    previous_report = report_path.read_text() if report_path.exists() else None
    from .codex_recovery import BINARY, recover
    recovery_report = None
    if not state.get('hot_switch'):
        recovered = recover(path.parent, provider_id, before.get('model'), native.root, before.get('model_provider', 'openai'))
        compatibility_ids = getattr(recovered, 'compatibility_ids', [])
        if compatibility_ids:
            # The engine pins old IDs. Restore the default provider while giving
            # those chats an explicitly selected official OAuth transport.
            doc.setdefault('model_providers', parser().table())[provider_id] = dict(OFFICIAL_COMPATIBILITY)
            override = 'model_provider=' + json.dumps(provider_id)
            recovery_report = {'provider_id': provider_id,
                'thread_ids': compatibility_ids, 'created': time.time(),
                'resume_command': 'env ' + shlex.quote('CODEX_HOME=' + str(path.parent.resolve())) +
                    ' ' + shlex.quote(BINARY) + ' --no-daemon -c ' + shlex.quote(override) + ' resume --all --include-non-interactive',
                'account_policy': 'Uses the current Codex OAuth login, not the prior qpool routing account.'}
    # Exact restoration when unchanged; otherwise retain unrelated later edits.
    rendered = original if doc.unwrap() == before.unwrap() else parser().dumps(doc)
    if path.read_text() != current:
        raise CollectorError('Codex 配置刚被其他操作修改，未覆盖；请重试')
    report_written = False
    try:
        atomic(path, rendered)
        if recovery_report:
            atomic(report_path, recovery_report)
            report_written = True
        remove_startup(native.root)
    except Exception as exc:
        if path.read_text() == rendered:
            atomic(path, current)
        if report_written and report_path.exists() and json.loads(report_path.read_text()) == recovery_report:
            if previous_report is None:
                report_path.unlink()
            else:
                atomic(report_path, previous_report)
        detail = ('；部分聊天可能已恢复模型，请重试恢复后再继续旧聊天'
                  if not state.get('hot_switch') else '，请重试断开')
        raise CollectorError('恢复收尾失败，接入记录已保留' + detail) from exc
    (native.root / 'desktop-key').unlink(missing_ok=True)
    (native.root / 'desktop.json').unlink()
    (native.root / 'desktop-route.json').unlink(missing_ok=True)
    native.desktop_error = None
    return dict(status(native), compatibility_provider=provider_id if recovery_report else None)


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
    if not state or (not state.get('hot_switch') and not (root / 'desktop-route.json').exists()):
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
        if state.get('hot_switch') and not live.get('active'):
            # The stable native transport remains available with routing disabled.
            return None
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
