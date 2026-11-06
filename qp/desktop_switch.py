"""Stable desktop transport; mode changes take effect at a user-turn boundary."""
import hashlib
import hmac
import http.client
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time
import uuid

import zstandard

from . import desktop
from .collectors.base import CollectorError

PREFIX = '/switch/v1'
OFFICIAL_HOST = 'chatgpt.com'


def native_connection():
    return http.client.HTTPSConnection(OFFICIAL_HOST, timeout=180)


def connect(native):
    from .native import atomic, read_login
    if not native.active or not native.grant:
        raise CollectorError('请先启用桌面使用的路由')
    if sys.platform != 'darwin':
        raise CollectorError('当前桌面接入仅支持 macOS')
    previous = desktop.read_state(native.root)
    if previous:
        if not previous.get('hot_switch'):
            raise CollectorError('当前为旧版桌面接入；请先恢复原配置，再接入常驻入口')
        if not desktop.status(native)['connected']:
            raise CollectorError('已有接入配置被修改，请先检查或恢复配置')
        desktop.install_startup(native.root, previous)
        desktop.persist_route(native)
        return desktop.status(native)
    home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser()
    path = home / 'config.toml'
    original, doc = desktop.read_config(path)
    if doc.get('profile') or doc.get('model_provider', 'openai') != 'openai':
        raise CollectorError('常驻入口需要原生 OpenAI 默认配置，请先退出 profile 或其他提供方')
    if doc.get('openai_base_url') is not None:
        raise CollectorError('已有其他网关地址，未覆盖')
    if str(doc.get('model', '')).startswith('qpool'):
        raise CollectorError('请先在 Codex 选择原生模型，再接入常驻入口')
    read_login(home / 'auth.json')  # Native mode always uses this login, never a pool identity.
    backup = native.root / 'codex-config.before-desktop.toml'
    managed = {'openai_base_url': f'http://127.0.0.1:{native.port}{PREFIX}', 'model_provider': 'openai'}
    for key, value in managed.items():
        doc[key] = value
    rendered = desktop.parser().dumps(doc)
    desktop.parser().parse(rendered)
    from . import profiles
    state = {'hot_switch': True, 'switch_mode': 'qpool', 'config_path': str(path),
             'backup_path': str(backup), 'managed': managed, 'provider_id': 'openai', 'provider': {},
             'python': sys.executable, 'code_root': str(Path(__file__).resolve().parent.parent),
             'db_path': str(Path(native.db_path).resolve()), 'pool_config': str(profiles.config_path().resolve()),
             'port': native.port, 'path_env': os.environ.get('PATH', '')}
    atomic(backup, original)
    atomic(native.root / 'desktop.json', state)
    try:
        desktop.persist_route(native)
        if (path.read_text() if path.exists() else '') != original:
            raise CollectorError('Codex 配置刚被其他操作修改，未覆盖')
        atomic(path, rendered)
        desktop.install_startup(native.root, state)
    except Exception:
        if path.exists() and path.read_text() == rendered:
            atomic(path, original)
        (native.root / 'desktop.json').unlink(missing_ok=True)
        (native.root / 'desktop-route.json').unlink(missing_ok=True)
        raise
    return desktop.status(native)


def set_mode(native, mode):
    from .native import atomic
    if mode not in ('native', 'qpool'):
        raise CollectorError('请选择原生或 qpool 模式')
    state = desktop.read_state(native.root)
    if not state or not state.get('hot_switch') or not desktop.status(native)['connected']:
        raise CollectorError('请先接入常驻桌面入口')
    if mode == 'qpool' and (not native.active or not native.grant):
        raise CollectorError('请先启用 qpool 路由')
    atomic(native.root / 'desktop.json', dict(state, switch_mode=mode))
    return desktop.status(native)


def identity(native, headers):
    """Authenticate only the currently signed-in native desktop, not an API key."""
    from .native import read_login
    state = desktop.read_state(native.root)
    if not state or not state.get('hot_switch'):
        raise CollectorError('常驻桌面入口未接入')
    login = read_login(Path(state['config_path']).parent / 'auth.json')
    if not hmac.compare_digest(headers.get('Authorization', '').encode(),
                               ('Bearer ' + login['access_token']).encode()):
        raise CollectorError('Codex 登录已变化，请重新加载账号登录')
    account = headers.get('ChatGPT-Account-Id')
    if account and account != login['account_id']:
        raise CollectorError('Codex 请求账号与当前登录不一致')
    return login['account_id']


def read_body(handler):
    size = int(handler.headers.get('Content-Length', '0'))
    if not 0 < size <= 8 * 1024 * 1024 or handler.headers.get('Transfer-Encoding'):
        raise CollectorError('请求过大或缺少有效 Content-Length')
    raw = handler.rfile.read(size)
    encoding = handler.headers.get('Content-Encoding', 'identity').lower()
    if encoding == 'zstd':
        try:
            with zstandard.ZstdDecompressor(max_window_size=8 * 1024 * 1024).stream_reader(io.BytesIO(raw)) as reader:
                raw = reader.read(8 * 1024 * 1024 + 1)
        except zstandard.ZstdError:
            raise CollectorError('请求压缩数据无效') from None
    elif encoding != 'identity':
        raise CollectorError('请求压缩格式不受支持')
    if len(raw) > 8 * 1024 * 1024:
        raise CollectorError('解压后的请求过大')
    body = json.loads(raw)
    if not isinstance(body, dict) or body.get('previous_response_id'):
        raise CollectorError('需要完整 Codex 对话上下文')
    return body


def legacy_account(native, session, subject):
    """Reuse old OpenAI bindings only for the authenticated original identity."""
    from . import profiles
    from .collectors import codex
    with native.receipts() as db:
        saved = db.execute('SELECT account FROM legacy_bindings WHERE session=?', (session,)).fetchone()
    if not saved:
        return None
    account = profiles.load()['accounts'].get(saved['account'])
    if (not account or account['agent'] != 'codex' or
            profiles.credentials(account, codex).get('account_id') != subject):
        raise CollectorError('旧聊天的原账号与当前原生登录不一致，请恢复原账号后继续')
    return saved['account']


def select_mode(native, session, body, subject, compact=False, headers=None):
    """Keep tools, retries and compaction on the original mode, including after restart."""
    items = body.get('input', [])
    if not isinstance(items, list):
        raise CollectorError('需要 Codex 消息列表')
    users = [item for item in items if isinstance(item, dict) and item.get('role') == 'user']
    metadata = body.get('client_metadata') or {}
    if not isinstance(metadata, dict):
        raise CollectorError('Codex 轮次元数据格式无效')
    for key in ('thread_id', 'session_id'):
        if key in metadata and (not isinstance(metadata[key], str) or
                hashlib.sha256(metadata[key].encode()).hexdigest()[:16] != session):
            raise CollectorError('Codex 轮次元数据与聊天不一致')
    encoded = metadata.get('x-codex-turn-metadata') or (headers or {}).get('x-codex-turn-metadata')
    try:
        detail = json.loads(encoded) if encoded else {}
        if not isinstance(detail, dict):
            raise ValueError()
        turn = metadata.get('turn_id') or detail.get('turn_id')
        compact = compact or 'compaction' in detail
    except (ValueError, TypeError, AttributeError):
        raise CollectorError('Codex 轮次元数据格式无效') from None
    try:
        turn = str(uuid.UUID(turn)) if turn else None
    except (ValueError, TypeError, AttributeError):
        raise CollectorError('Codex 轮次标识无效') from None
    subject = hashlib.sha256(subject.encode()).hexdigest()
    with native.receipts() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT * FROM desktop_turns WHERE session=?', (session,)).fetchone()
        if previous and previous['subject'] != subject:
            raise CollectorError('此聊天的原生账号已变化，未自动迁移上下文')
        if previous and (compact or previous['turn'] == turn):
            # Codex's protocol turn ID survives tool execution and history
            # compaction; do not infer a new turn from a reconstructed prompt.
            return previous['mode'], previous['model'], previous['turn'], compact
        if compact or not users or not turn:
            raise CollectorError('未找到本轮模式绑定，请发送新的用户消息后重试')
        model = body.get('model')
        if not isinstance(model, str) or not model or model.startswith('qpool'):
            raise CollectorError('常驻入口请保留 Codex 原生模型选择')
        state = desktop.read_state(native.root)
        if not state or not state.get('hot_switch'):
            raise CollectorError('桌面入口已断开')
        mode = state['switch_mode']
        if mode == 'qpool' and (not native.grant or not native.active):
            raise CollectorError('qpool 路由已停用；可以切到原生模式继续')
        db.execute('INSERT OR REPLACE INTO desktop_turns VALUES (?,?,?,?,?)',
                   (session, turn, mode, model, subject))
        return mode, model, turn, compact


def handle(handler, path):
    native = handler.server.console.native
    if not handler.allowed(False):
        return handler.respond(403, {'error': {'message': '仅允许本机请求'}})
    if path not in (PREFIX + '/responses', PREFIX + '/responses/compact'):
        return handler.respond(404, {'error': {'message': '不支持此请求路径'}})
    try:
        subject = identity(native, handler.headers)
    except CollectorError:
        return handler.respond(401, {'error': {'message': '桌面入口未接入或 Codex 登录不匹配'}})
    try:
        if native.audit_error:
            raise CollectorError('请求记录不可用，请修复后重启服务')
        body = read_body(handler)
        raw_session = (handler.headers.get('session_id') or handler.headers.get('thread-id')
                       or handler.headers.get('x-codex-session-id') or body.get('prompt_cache_key'))
        if not isinstance(raw_session, str) or not 0 < len(raw_session) <= 512:
            raise CollectorError('缺少 Codex 聊天标识')
        session = hashlib.sha256(raw_session.encode()).hexdigest()[:16]
        compact = path.endswith('/compact')
        with native.turn_lock(('desktop-switch', session)):
            # Check before persisting a turn pin, in either mode. A rejected
            # login must not prevent the original account from retrying.
            old_account = legacy_account(native, session, subject)
            # Approval calls are owned by the native login and must never enter
            # automatic model/account selection or overwrite the user's turn pin.
            if body.get('model') == 'codex-auto-review':
                with native.receipts() as db:
                    previous = db.execute('SELECT subject FROM desktop_turns WHERE session=?', (session,)).fetchone()
                if previous and previous['subject'] != hashlib.sha256(subject.encode()).hexdigest():
                    raise CollectorError('此聊天的原生账号已变化，未迁移审批上下文')
                mode, model = 'native', 'codex-auto-review'
            else:
                mode, model, protocol_turn, compact = select_mode(native, session, body, subject, compact, handler.headers)
            body = dict(body, model=model if mode == 'native' else 'qpool-auto')
            if mode == 'native':
                return forward_native(handler, path.replace(PREFIX, '/backend-api/codex', 1),
                                      body, session, subject)
            from . import gateway
            return gateway.handle(handler, path.replace(PREFIX, '/v1', 1), switch_body=body,
                                  switch_model=model, switch_turn=protocol_turn, switch_compact=compact,
                                  switch_legacy_account=old_account)
    except sqlite3.Error:
        native.audit_error = '桌面请求记录写入失败，请检查磁盘与权限后重启服务'
        handler.respond(502, {'error': {'message': native.audit_error}})
    except (CollectorError, ValueError, OSError):
        handler.respond(502, {'error': {'message': '桌面切换请求未完成；请检查登录、入口和本轮绑定，不会自动换账号重试'}})


def validate_target(native, body, client_model, target_model):
    """Changing the model must not invalidate the loaded Codex tool contract."""
    known = native.model_metadata()
    source, target = known.get(client_model), known.get(target_model)
    if not source or not target:
        raise CollectorError('缺少模型能力目录，请同步目录后重试')
    for field in ('shell_type', 'apply_patch_tool_type', 'web_search_tool_type'):
        if source.get(field) != target.get(field):
            raise CollectorError('原生所选模型与路由模型的工具能力不兼容，请调整模型或路由')
    if not set(source.get('input_modalities', [])) <= set(target.get('input_modalities', [])):
        raise CollectorError('路由模型不支持原生所选模型的输入类型，请调整路由')
    if target.get('context_window', 0) < source.get('context_window', 0):
        raise CollectorError('路由模型上下文窗口更小，请在 Codex 选择兼容模型后开始新轮次')
    effort = (body.get('reasoning') or {}).get('effort')
    if effort and effort not in {r['effort'] for r in target.get('supported_reasoning_levels', [])}:
        raise CollectorError('路由模型不支持当前推理档位，请调整 Codex 推理档位后重试')


def forward_native(handler, path, body, session, subject):
    native = handler.server.console.native
    if not native.slots.acquire(blocking=False):
        return handler.respond(429, {'error': {'message': '当前已有 4 个请求运行'}})
    started = time.time()
    receipt = secrets.token_hex(12)
    upstream = None
    committed = False
    outcome, error, actual_model, usage = 'failed', '原生请求未完成；未自动重试', None, {}
    try:
        if identity(native, handler.headers) != subject:
            raise CollectorError('请求账号发生变化')
        with native.receipts() as db:
            db.execute('''INSERT INTO requests
                (id,session,route,account,model,decision,status,started,entry_mode)
                VALUES (?,?,?,?,?,?,?,?,?)''',
                (receipt, session, 'Codex 原生直通', 'Codex 当前登录', body['model'],
                 '原生账号和模型；本地仅转发', 'running', started, 'native'))
        headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream',
                   'Accept-Encoding': 'identity', 'ChatGPT-Account-Id': subject,
                   'Authorization': handler.headers['Authorization']}
        for name in ('session_id', 'thread-id', 'x-codex-session-id', 'originator', 'version', 'user-agent', 'openai-beta'):
            if handler.headers.get(name):
                headers[name] = handler.headers[name]
        # These negotiate Codex's wire format (including Responses Lite).
        # Dropping them while forwarding the body can change the upstream protocol.
        for name, value in handler.headers.items():
            if name.lower().startswith(('x-codex-', 'x-openai-internal-codex-')) or name.lower() in ('session-id', 'x-client-request-id'):
                headers[name] = value
        upstream = native_connection()
        upstream.request('POST', path, body=json.dumps(body).encode(), headers=headers)
        response = upstream.getresponse()
        if response.status != 200:
            error = f'原生上游 HTTP {response.status}；未自动重试或切换账号'
            return handler.respond(response.status if 400 <= response.status < 600 else 502,
                                   {'error': {'message': error}})
        content_type = response.getheader('Content-Type') or (
            'text/event-stream' if body.get('stream') is True else 'application/json')
        if 'text/event-stream' in content_type:
            handler.send_response(200)
            handler.send_header('Content-Type', 'text/event-stream')
            handler.send_header('Connection', 'close')
            handler.send_header('Cache-Control', 'no-store')
            handler.end_headers()
            handler.close_connection = True
            committed = True
            total = 0
            while True:
                line = response.readline(2 * 1024 * 1024 + 1)
                if not line:
                    break
                total += len(line)
                if len(line) > 2 * 1024 * 1024 or total > 32 * 1024 * 1024:
                    raise CollectorError('原生响应超过大小限制')
                if line.startswith(b'data: '):
                    try:
                        event = json.loads(line[6:])
                        if event.get('type') == 'response.completed':
                            result = event.get('response', {})
                            usage = result.get('usage') if isinstance(result.get('usage'), dict) else {}
                            actual_model = result.get('model')
                            outcome, error = 'completed', None
                        elif event.get('type') in ('response.failed', 'response.incomplete', 'error'):
                            outcome, error = 'failed', '原生上游未完成生成；未自动切换账号'
                    except (ValueError, AttributeError):
                        pass
                handler.wfile.write(line)
                handler.wfile.flush()
        else:
            raw = response.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                raise CollectorError('原生响应超过大小限制')
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise CollectorError('原生响应格式无效')
            usage = result.get('usage') if isinstance(result.get('usage'), dict) else {}
            actual_model = result.get('model')
            outcome = 'completed' if path.endswith('/compact') or result.get('status') == 'completed' else 'failed'
            error = None if outcome == 'completed' else '原生上游未返回完成状态'
            handler.respond(200, raw, 'application/json')
            committed = True
    except (CollectorError, OSError, ValueError, http.client.HTTPException):
        if not committed:
            handler.respond(502, {'error': {'message': error or '原生连接中断；未自动重试'}})
        else:
            outcome, error = 'failed', '原生连接中断；未自动重试'
    finally:
        if upstream:
            upstream.close()
        try:
            with native.receipts() as db:
                db.execute('''UPDATE requests SET status=?,duration=?,input_tokens=?,output_tokens=?,error=?,actual_model=?
                              WHERE id=?''', (outcome, time.time() - started,
                              usage.get('input_tokens') if type(usage.get('input_tokens')) is int else None,
                              usage.get('output_tokens') if type(usage.get('output_tokens')) is int else None,
                              error, actual_model if isinstance(actual_model, str) else None, receipt))
        except sqlite3.Error:
            native.audit_error = '原生请求记录写入失败，请检查磁盘与权限后重启服务'
        finally:
            native.slots.release()
