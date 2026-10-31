"""Streaming Responses bridge for the native Codex entry (loopback + bearer)."""
import hmac
import http.client
import json
import secrets
import sqlite3
import time
import io
import zstandard

from . import profiles, openai_account, desktop
from .collectors.base import CollectorError


def handle(handler, path):
    native = handler.server.console.native
    authorization = handler.headers.get('Authorization', '')
    grant = native.grant
    legacy = path.startswith('/legacy/v1/')
    legacy_account = desktop.legacy_account(native, handler.headers) if legacy and handler.allowed(False) else None
    if legacy:
        path = path.removeprefix('/legacy')
    if (not handler.allowed(False) or not native.grant or
            not (bool(legacy_account) if legacy else
                 (hmac.compare_digest(authorization.encode(), ('Bearer ' + native.grant).encode()) or
                  desktop.authorized(native, handler.headers)))):
        handler.respond(401, {'error': {'message': 'qpool 入口未授权或已停用', 'type': 'authentication_error'}})
        return
    if path not in ('/v1/responses', '/v1/responses/compact'):
        handler.respond(404, {'error': {'message': '仅支持 Codex Responses 请求'}})
        return
    if not native.slots.acquire(blocking=False):
        handler.respond(429, {'error': {'message': 'qpool 当前已有 4 个请求运行，请稍后重试'}})
        return
    adapter_lock = None
    upstream = None
    receipt = None
    started = time.time()
    committed = False
    outcome = 'failed'
    error = '请求未完成'
    usage = {}
    actual_model = None
    try:
        size = int(handler.headers.get('Content-Length', '0'))
        if not 0 < size <= 8 * 1024 * 1024 or handler.headers.get('Transfer-Encoding'):
            raise CollectorError('请求过大或缺少有效 Content-Length')
        raw = handler.rfile.read(size)
        encoding = handler.headers.get('Content-Encoding', 'identity').lower()
        if encoding == 'zstd':
            try:
                with zstandard.ZstdDecompressor(max_window_size=8 * 1024 * 1024).stream_reader(io.BytesIO(raw)) as reader:
                    raw = reader.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise CollectorError('解压后的请求超过大小限制')
            except zstandard.ZstdError as exc:
                raise CollectorError('请求压缩数据无效：' + str(exc)) from None
        elif encoding != 'identity':
            raise CollectorError('请求使用了不支持的压缩格式')
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise CollectorError('请求必须是对象')
        approval = body.get('model') == 'codex-auto-review' and not path.endswith('/compact')
        approval_account = desktop.legacy_account(native, handler.headers) if approval else None
        # Compaction may omit model and reuse an already validated session binding.
        if not approval and not legacy and body.get('model') != 'qpool-auto' and not (path.endswith('/compact') and 'model' not in body):
            raise CollectorError('当前 qpool 会话请选择 qpool auto；原模型请在 OpenAI 会话中使用')
        if approval:
            binding, session = native.approval_binding(body, handler.headers, approval_account)
        else:
            binding, session = native.select(body, handler.headers, path.endswith('/compact'), **({'legacy_account': legacy_account} if legacy else {}))
        channel = binding['channel']
        account = profiles.load()['accounts'].get(channel['account_id'])
        if not account:
            raise CollectorError('路由绑定的账号已移除，请新建会话')
        adapter = native.adapter(account)
        if channel['model'] not in adapter.models and hasattr(adapter, 'refresh_models'):
            adapter.refresh_models()
        adapter_lock = adapter.lock
        adapter_lock.acquire()
        adapter.ensure()
        if not native.grant or native.grant != grant or not ((desktop.legacy_account(native, handler.headers) == legacy_account) if legacy else (
                hmac.compare_digest(authorization.encode(), ('Bearer ' + native.grant).encode()) or
                desktop.authorized(native, handler.headers))):
            raise CollectorError('入口已停用或更新，请重新打开 Codex')
        if approval:
            if desktop.legacy_account(native, handler.headers) != approval_account:
                raise CollectorError('原生审批账号已变化')
            native.approval_binding(body, handler.headers, approval_account)
        else:
            native.validate_binding(channel)
        if channel['model'] not in adapter.models:
            raise CollectorError('所选模型已不在账号适配器目录中，请调整路由')
        body['model'] = channel['model']
        receipt = secrets.token_hex(12)
        with native.receipts() as conn:
            conn.execute('''INSERT INTO requests
                (id,session,route,account,model,decision,status,started,decision_id)
                VALUES (?,?,?,?,?,?,?,?,?)''', (receipt, session, binding['route'], account.get('label', account['id']),
                channel['model'], binding['decision'], 'running', started, binding.get('decision_id')))
        # Never forward the client's native OAuth credentials or arbitrary headers.
        headers = {'Authorization': 'Bearer ' + adapter.key, 'Content-Type': 'application/json',
                   'Accept': 'text/event-stream', 'Accept-Encoding': 'identity'}
        for name in ('session_id', 'thread-id', 'x-codex-session-id', 'originator', 'version', 'user-agent'):
            value = handler.headers.get(name)
            if value:
                headers[name] = value
        upstream = openai_account.connection(adapter, 180)
        upstream.request('POST', path, body=json.dumps(body).encode(), headers=headers)
        response = upstream.getresponse()
        if response.status != 200:
            error = f'上游返回 HTTP {response.status}；请检查账号登录、额度和模型权限'
            handler.respond(response.status if 400 <= response.status < 600 else 502,
                            {'error': {'message': error, 'type': 'upstream_error'}})
            return
        content_type = response.getheader('Content-Type', '')
        if 'text/event-stream' in content_type:
            handler.send_response(200)
            handler.send_header('Content-Type', 'text/event-stream')
            handler.send_header('Cache-Control', 'no-store')
            handler.send_header('Connection', 'close')
            handler.send_header('X-Qpool-Request-Id', receipt)
            handler.end_headers()
            handler.close_connection = True
            committed = True
            count = 0
            while True:
                line = response.readline(2 * 1024 * 1024 + 1)
                if not line:
                    break
                count += len(line)
                if len(line) > 2 * 1024 * 1024 or count > 32 * 1024 * 1024:
                    raise CollectorError('上游响应超过本地大小限制')
                if line.startswith(b'data: '):
                    try:
                        event = json.loads(line[6:])
                        if event.get('type') == 'response.completed':
                            result = event.get('response', {})
                            usage = result.get('usage') if isinstance(result.get('usage'), dict) else {}
                            actual_model = result.get('model')
                            outcome, error = 'completed', None
                        elif event.get('type') in ('response.failed', 'error'):
                            outcome, error = 'failed', '上游生成失败；未自动切换账号或重放'
                    except (ValueError, TypeError, AttributeError):
                        pass
                handler.wfile.write(line)
                handler.wfile.flush()
        else:
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise CollectorError('上游响应超过本地大小限制')
            result = json.loads(raw)
            usage = result.get('usage') if isinstance(result.get('usage'), dict) else {}
            actual_model = result.get('model')
            outcome = 'completed' if path.endswith('/compact') or result.get('status') == 'completed' else 'failed'
            error = None if outcome == 'completed' else '上游未返回完成状态'
            handler.respond(200, raw, 'application/json')
            committed = True
    except (BrokenPipeError, ConnectionResetError):
        outcome, error = 'interrupted', '客户端连接中断；未自动重放'
    except Exception as exc:
        error = str(exc) if isinstance(exc, CollectorError) else '请求失败或超时，请检查本地适配器和账号连接'
        if not committed:
            handler.respond(502, {'error': {'message': error, 'type': 'qpool_error'}})
    finally:
        try:
            if upstream:
                upstream.close()
            if receipt:
                with native.receipts() as conn:
                    conn.execute('''UPDATE requests SET status=?,duration=?,input_tokens=?,output_tokens=?,error=?,actual_model=?
                                    WHERE id=?''', (outcome, time.time() - started,
                                   usage.get('input_tokens') if type(usage.get('input_tokens')) is int else None,
                                   usage.get('output_tokens') if type(usage.get('output_tokens')) is int else None,
                                   error, actual_model if isinstance(actual_model, str) else None, receipt))
        except sqlite3.Error:
            native.audit_error = '请求记录写入失败，入口已停用；请检查磁盘与权限后重启服务'
            native.disable()
        finally:
            if adapter_lock:
                adapter_lock.release()
            native.slots.release()
