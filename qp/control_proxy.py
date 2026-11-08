"""Transparent stdio App Server proxy, with routing before turn/start.

All permissions, tools, approvals and notifications remain owned by Codex.
Private RPC ids never escape to the desktop. No prompts or credentials are logged.
"""
import copy
import http.client
import json
import os
from pathlib import Path
import queue
import select
import subprocess
import sys
import threading

from . import desktop
from .collectors.base import CollectorError
from .control import METADATA_KEY
from .native import read_login


class RpcError(CollectorError):
    def __init__(self, error):
        self.error = error
        super().__init__(error.get('message', 'Codex 控制请求失败'))


class RpcDisconnected(CollectorError):
    pass


def api(root, action, data):
    state = desktop.read_state(root)
    if not state or not state.get('hot_switch'):
        raise CollectorError('控制代理需要常驻桌面入口')
    login = read_login(Path(state['config_path']).parent / 'auth.json')
    conn = http.client.HTTPConnection('127.0.0.1', state['port'], timeout=90)
    try:
        conn.request('POST', '/api/desktop/control/' + action, json.dumps(data),
                     {'Content-Type': 'application/json', 'X-Qpool-Request': '1',
                      'Authorization': 'Bearer ' + login['access_token'],
                      'ChatGPT-Account-Id': login['account_id']})
        response = conn.getresponse()
        result = json.loads(response.read(131073))
        if response.status != 200:
            raise CollectorError(result.get('error', '控制代理准备失败'))
        return result
    finally:
        conn.close()


def user_item(inputs):
    content = []
    for item in inputs:
        if item.get('type') == 'text':
            text = item.get('text', '')
            clipped = text.encode()[:10000].decode(errors='ignore')
            content.append({'type': 'input_text', 'text': clipped})
            if clipped != text:
                content.append({'type': 'unavailable', 'source_kind': 'truncated_text'})
        elif item.get('type') in ('image', 'localImage'):
            # Classification sees only that non-text context exists, not image data.
            content.append({'type': 'input_image', 'image_url': '[image supplied to Codex]'})
    return {'role': 'user', 'content': content}


def decision_items(turns, inputs):
    items = []
    for turn in reversed(turns):
        for item in turn.get('items', []):
            if item.get('type') == 'userMessage':
                items.append(user_item(item.get('content', [])))
            elif item.get('type') == 'agentMessage':
                items.append({'role': 'assistant', 'content': item.get('text', '')[:6000]})
    # This is a bounded classification view, never the model's actual history.
    if turns:
        items.insert(0, {'role': 'assistant', 'content': '[Earlier history omitted from routing view]'})
    while len(json.dumps(items).encode()) > 48000:
        items.pop(1 if len(items) > 1 else 0)
    items.append(user_item(inputs))
    while len(json.dumps(items, ensure_ascii=False).encode()) > 80000 and len(items) > 1:
        items.pop(0)
    # Multiple pasted text blocks can exceed the classifier budget as well.
    while len(json.dumps(items[-1], ensure_ascii=False).encode()) > 12000:
        texts = [p for p in items[-1]['content'] if p.get('type') == 'input_text']
        if not texts:
            items[-1]['content'] = items[-1]['content'][:1]
            break
        longest = max(texts, key=lambda p: len(p['text']))
        if len(longest['text']) < 2:
            items[-1]['content'] = items[-1]['content'][:1]
            break
        longest['text'] = longest['text'][:len(longest['text']) // 2]
    return items


class Proxy:
    def __init__(self, process, root, output=None):
        self.process, self.root = process, Path(root)
        self.output = output or sys.stdout
        self.write_lock = threading.Lock()
        self.output_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.pending = {}
        self.counter = 0
        self.thread_locks = {}
        self.workers = []
        self.closed = threading.Event()

    def emit(self, message):
        with self.output_lock:
            self.output.write(json.dumps(message) + '\n')
            self.output.flush()

    def write(self, message):
        with self.write_lock:
            self.process.stdin.write(json.dumps(message) + '\n')
            self.process.stdin.flush()

    def rpc(self, method, params):
        result = queue.Queue(maxsize=1)
        with self.pending_lock:
            if self.closed.is_set():
                raise CollectorError('Codex 控制连接已关闭')
            self.counter += 1
            ident = 'qpool:' + str(self.counter)
            self.pending[ident] = result
        try:
            self.write({'id': ident, 'method': method, 'params': params})
            try:
                value = result.get(timeout=120)
            except queue.Empty:
                raise CollectorError('Codex 控制请求超时；未自动重发') from None
            if 'error' in value:
                if value.get('_qpoolDisconnected'):
                    raise RpcDisconnected('Codex 控制连接已关闭；请求结果未确认')
                raise RpcError(value['error'])
            return value['result']
        finally:
            with self.pending_lock:
                self.pending.pop(ident, None)

    def read(self):
        try:
            for line in self.process.stdout:
                value = json.loads(line)
                if 'id' in value and 'method' not in value:
                    with self.pending_lock:
                        pending = self.pending.get(value['id'])
                    if pending:
                        pending.put(value)
                    # Late private RPC responses must not collide with desktop ids.
                else:
                    self.emit(value)
        finally:
            self.closed.set()
            with self.pending_lock:
                for pending in self.pending.values():
                    try:
                        pending.put_nowait({'error': {'message': 'Codex 控制连接已关闭'}, '_qpoolDisconnected': True})
                    except queue.Full:
                        pass

    def prepare_turn(self, params):
        thread = params['threadId']
        snapshot = self.rpc('thread/read', {'threadId': thread, 'includeTurns': False})['thread']
        if snapshot.get('status', {}).get('type') == 'active':
            # turn/start on an active thread is steering, not a new model boundary.
            params = dict(params)
            for key in ('model', 'effort', 'collaborationMode', 'responsesapiClientMetadata',
                        'responsesapiClientMetadataPatch'):
                params.pop(key, None)
            return params
        if snapshot.get('modelProvider') != 'openai':
            raise CollectorError('控制代理仅支持原生 OpenAI 聊天')
        if not params.get('input') or params.get('toolOutput'):
            return params
        try:
            turns = self.rpc('thread/turns/list', {'threadId': thread, 'limit': 6,
                             'sortDirection': 'desc', 'itemsView': 'full'}).get('data', [])
        except RpcError as exc:
            if exc.error.get('code') == -32600 and 'not materialized yet' in str(exc):
                turns = []  # Codex has no persisted turns before the first input.
            else:
                raise
        settings = (params.get('collaborationMode') or {}).get('settings', {})
        plan = api(self.root, 'prepare', {'threadId': thread,
                   'currentModel': snapshot.get('model'),
                   'model': settings.get('model') or params.get('model') or snapshot.get('model'),
                   'effort': settings.get('reasoning_effort') or params.get('effort') or snapshot.get('reasoningEffort'),
                   'items': decision_items(turns, params['input'])})
        params = copy.deepcopy(params)
        params.update(model=plan['model'], effort=plan['effort'])
        if params.get('collaborationMode'):
            params['collaborationMode']['settings'].update(model=plan['model'], reasoning_effort=plan['effort'])
        patch = params.get('responsesapiClientMetadataPatch') or {}
        params['responsesapiClientMetadataPatch'] = patch
        patch[METADATA_KEY] = plan['token']
        return params

    def start_queued(self, params):
        thread = params['threadId']
        queued_id = params.get('queuedSubmissionId')
        claim = {'threadId': thread, 'queuedSubmissionId': queued_id}
        if queued_id:
            old = api(self.root, 'queue', claim)
            if old['claimed']:
                if old['result']:
                    try:
                        self.rpc('thread/queue/delete', claim)
                    except (CollectorError, OSError):
                        pass  # Already accepted; cleanup cannot turn it into failure.
                    return old['result']
                raise CollectorError('该排队消息的提交结果未确认；请检查聊天后删除或重新发送，未自动重放')
        snapshot = self.rpc('thread/read', {'threadId': thread, 'includeTurns': False})['thread']
        if snapshot.get('status', {}).get('type') == 'active':
            raise CollectorError('当前回复尚未结束，排队消息保持等待')
        cursor = None
        selected = None
        while True:
            page = self.rpc('thread/queue/list', {'threadId': thread, 'limit': 100, 'cursor': cursor})
            selected = next((q for q in page['data'] if not queued_id or q['id'] == queued_id), None)
            cursor = page.get('nextCursor')
            if selected or not cursor:
                break
        if not selected:
            raise CollectorError('未找到排队消息')
        claim['queuedSubmissionId'] = selected['id']
        prepared = self.prepare_turn({'threadId': thread, 'input': selected['input'],
                                     'clientUserMessageId': selected['clientUserMessageId']})
        api(self.root, 'queue', dict(claim, action='claim'))
        try:
            result = self.rpc('turn/start', prepared)
        except RpcError:
            api(self.root, 'queue', dict(claim, action='rejected'))
            raise
        # Once Codex accepts the turn, return success even if bookkeeping fails.
        # The durable pending claim prevents another process from replaying it.
        try:
            api(self.root, 'queue', dict(claim, action='accepted', result=result))
        except (CollectorError, OSError, ValueError):
            print('qpool: 排队消息已提交，确认记录未完成；不会自动重发。', file=sys.stderr)
        try:
            self.rpc('thread/queue/delete', claim)
        except (CollectorError, OSError):
            print('qpool: 排队消息已提交，待清除队列记录。', file=sys.stderr)
        return result

    def request(self, message):
        ident, method = message['id'], message['method']
        params = message.get('params') or {}
        thread = params.get('threadId')
        lock = None
        try:
            if method == 'turn/start' or method.startswith('thread/queue/'):
                with self.pending_lock:
                    lock = self.thread_locks.setdefault(thread, threading.Lock())
                if not lock.acquire(blocking=False):
                    lock = None
                    raise CollectorError('此聊天正在准备下一轮，请稍后重试')
                if method == 'turn/start':
                    params = self.prepare_turn(params)
            result = self.start_queued(params) if method == 'thread/queue/start' else self.rpc(method, params)
            if method == 'thread/settings/update' and (params.get('model') or params.get('effort')):
                snapshot = self.rpc('thread/read', {'threadId': thread, 'includeTurns': False})['thread']
                api(self.root, 'prepare', {'threadId': thread, 'model': params.get('model'),
                    'effort': params.get('effort') or snapshot.get('reasoningEffort'),
                    'explicit': bool(params.get('model')), 'effortExplicit': bool(params.get('effort')),
                    'preferenceOnly': True, 'currentModel': snapshot.get('model')})
            self.emit({'id': ident, 'result': result})
        except RpcError as exc:
            self.emit({'id': ident, 'error': exc.error})
        except (CollectorError, OSError, ValueError, KeyError, TypeError) as exc:
            self.emit({'id': ident, 'error': {'code': -32000,
                      'message': str(exc) if isinstance(exc, CollectorError) else 'qpool 控制代理请求失败；未自动重发'}})
        finally:
            if lock:
                lock.release()

    def run(self, source=None):
        reader = threading.Thread(target=self.read, daemon=True)
        reader.start()
        try:
            source = source or sys.stdin
            buffer = b''
            while not self.closed.is_set():
                if not select.select([source], [], [], 0.2)[0]:
                    continue
                chunk = os.read(source.fileno(), 65536)
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > 64 * 1024 * 1024:
                    raise CollectorError('桌面控制消息过大')
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    message = json.loads(line)
                    if 'id' in message and 'method' in message:
                        self.workers = [w for w in self.workers if w.is_alive()]
                        if len(self.workers) >= 64:
                            self.emit({'id': message['id'], 'error': {'code': -32000, 'message': '控制请求过多'}})
                            continue
                        worker = threading.Thread(target=self.request, args=(message,), daemon=True)
                        self.workers.append(worker)
                        worker.start()
                    else:
                        self.write(message)
        finally:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            reader.join(timeout=3)
            for worker in self.workers:
                worker.join(timeout=1)


def is_rpc_server(args):
    # Skip option values before identifying the command: a snapshot file named
    # "proxy" is not the app-server proxy subcommand. Native Codex still owns
    # validation of the options themselves.
    valued = {'-c', '--config', '--enable', '--disable', '-p', '--profile',
              '--startup-client-info', '--cloud-skills-snapshot', '--code-mode-host',
              '--listen', '--ws-auth', '--ws-token-file', '--ws-token-sha256',
              '--ws-shared-secret-file', '--ws-issuer', '--ws-audience',
              '--ws-max-clock-skew-seconds'}
    server = False
    tokens = iter(args)
    for arg in tokens:
        if arg in valued:
            next(tokens, None)
        elif arg in ('--help', '-h', '--version', '-V', '--print-startup-config'):
            return False
        elif arg.startswith('-'):
            continue
        elif not server and arg == 'app-server':
            server = True
        else:
            return False  # Other commands and all app-server subcommands.
    return server


def main(root, binary):
    args = sys.argv[1:]
    # The desktop locates this shim through CODEX_CLI_PATH; the real engine and
    # its runtime helpers must resolve the bundled binary, never recurse here.
    os.environ['CODEX_CLI_PATH'] = binary
    os.environ['CODEX_APP_RUNTIME_CLI_PATH'] = binary
    # Desktop policy bootstrap is a one-shot CLI invocation with stdin closed,
    # not a JSON-RPC session. Its JSON output and exit status must pass through
    # untouched; terminating it on stdin EOF prevents the desktop from starting.
    if not is_rpc_server(args):
        os.execv(binary, [binary] + args)
    if not desktop.read_state(root):
        os.execv(binary, [binary] + args)  # Explicit restore returns to the native entry.
    if '--listen' in args and args[args.index('--listen') + 1] != 'stdio://':
        raise SystemExit('qpool 控制代理仅支持桌面 stdio 接入')
    desktop.token(root)  # Start the existing managed HTTP service if necessary.
    process = subprocess.Popen([binary] + args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=sys.stderr, text=True, bufsize=1)
    Proxy(process, root).run()
