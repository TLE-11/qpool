"""Explicit live subscription QA, in a fresh isolated home; never installs startup.

Run with --live. Only synthetic chats are sent; paid API accounts are excluded.
The report records route receipts and history recall, never credentials.
"""
import argparse
import copy
import hashlib
import hmac
import http.client
import io
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time
from unittest.mock import patch

from isolated_qa_probe import Client, api

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--explicit-provider', action='store_true',
                        help='Diagnostic only: explicitly resume using each original provider ID.')
    parser.add_argument('--hot', action='store_true', help='Keep each engine and chat loaded while switching.')
    parser.add_argument('--bridge', action='store_true',
                        help='Prototype only: use a constant local endpoint and switch inside the bridge.')
    parser.add_argument('--tools', action='store_true', help='Read a synthetic marker with a tool before switching.')
    args = parser.parse_args()
    if args.tools and not args.bridge:
        parser.error('--tools requires --bridge')
    if args.bridge:
        args.hot = True
    from qp.native import atomic
    root = REPO / '.qpool' / ('switch-qa-' + str(time.time_ns()))
    home = root / 'codex-home'
    home.mkdir(parents=True, mode=0o700)
    workspace = root / 'workspace'
    workspace.mkdir()
    source_home = Path.home() / '.codex'
    originals = {}

    def read_original(path):
        path = Path(path)
        value = path.read_bytes()
        originals[path] = hashlib.sha256(value).hexdigest()
        return value

    read_original(source_home / 'config.toml')
    auth = json.loads(read_original(source_home / 'auth.json'))
    subject = auth['tokens']['account_id']

    def isolated_login(value):
        value = copy.deepcopy(value)
        value['tokens']['refresh_token'] = ''
        return value

    atomic(home / 'auth.json', isolated_login(auth))
    del auth
    atomic(home / 'models_cache.json', (source_home / 'models_cache.json').read_text())
    atomic(home / 'config.toml', 'model="gpt-6.1-sol"\nmodel_reasoning_effort="low"\n')
    config = json.loads(read_original(REPO / '.qpool/console/pools.json'))
    config['accounts'] = [a for a in config['accounts'] if a['agent'] == 'codex']
    for account in config['accounts']:
        login = json.loads(read_original(Path(account['source']['credentials_file'])))
        account_home = root / 'accounts' / account['id']
        atomic(account_home / 'auth.json', isolated_login(login))
        del login
        account['source']['credentials_file'] = str(account_home / 'auth.json')
        account['source']['profile_dir'] = str(account_home)
    ids = {a['id'] for a in config['accounts']}
    config['channels'] = [c for c in config['channels'] if c['account_id'] in ids]
    native_id = next(a['id'] for a in config['accounts'] if a['source']['expected_subject'] == subject)
    target_id = next((a['id'] for a in config['accounts'] if a['id'] != native_id), native_id)
    atomic(root / 'pools.json', config)
    os.environ.update(CODEX_HOME=str(home), QPOOL_CONFIG=str(root / 'pools.json'),
                      QPOOL_DB=str(root / 'ledger.db'), QPOOL_COLLECTORS_DIR=str(root / 'no-plugins'))
    from qp import web
    console = web.Console(root / 'ledger.db')
    server = web.Server(('127.0.0.1', 0), console)
    bridge_hits = []
    if args.bridge:
        server.bridge_mode = 'native'
        class BridgeHandler(web.Handler):
            """QA-only single-model transport probe, not a production gateway."""
            def do_GET(self):
                if self.path == '/v1/responses':
                    self.protocol_version = 'HTTP/1.1'
                    self.close_connection = True
                    return self.respond(426, {'error': 'Use HTTP Responses streaming'})
                return super().do_GET()

            def do_POST(self):
                if self.path == '/qa/mode':
                    if not self.allowed(True):
                        return self.respond(403, {'error': 'local QA control only'})
                    data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
                    if data.get('mode') not in ('native', 'qpool'):
                        return self.respond(400, {'error': 'invalid mode'})
                    self.server.bridge_mode = data['mode']
                    return self.respond(200, {'mode': data['mode']})
                if self.path != '/v1/responses':
                    return super().do_POST()
                login = json.loads((home / 'auth.json').read_text())['tokens']
                if not self.allowed(False) or not hmac.compare_digest(
                        self.headers.get('Authorization', '').encode(),
                        ('Bearer ' + login['access_token']).encode()):
                    return self.respond(401, {'error': {'message': 'QA identity mismatch'}})
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                mode = self.server.bridge_mode  # Snapshot once at the request boundary.
                sid = self.headers.get('session_id') or self.headers.get('thread-id')
                hit = {'mode': mode, 'session': hashlib.sha256((sid or '').encode()).hexdigest()[:16]}
                bridge_hits.append(hit)
                if mode == 'qpool':
                    import zstandard
                    from qp import gateway
                    if self.headers.get('Content-Encoding') == 'zstd':
                        with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)) as reader:
                            raw = reader.read(8 * 1024 * 1024 + 1)
                    body = json.loads(raw)
                    hit['client_model'] = body['model']
                    body['model'] = 'qpool-auto'
                    raw = json.dumps(body).encode()
                    self.headers.replace_header('Content-Length', str(len(raw)))
                    if 'Content-Encoding' in self.headers:
                        del self.headers['Content-Encoding']
                    self.headers.replace_header('Authorization', 'Bearer ' + console.native.grant)
                    self.rfile = io.BytesIO(raw)
                    # Exercise the real account selection, binding, adapter and audit path.
                    return gateway.handle(self, '/v1/responses')
                hit.update(account=native_id, destination='chatgpt.com/backend-api/codex/responses')
                upstream = http.client.HTTPSConnection('chatgpt.com', timeout=90)
                headers = {name: value for name, value in self.headers.items()
                           if name.lower() in ('authorization', 'chatgpt-account-id', 'content-type',
                               'content-encoding', 'accept', 'session_id', 'thread-id', 'originator',
                               'version', 'user-agent', 'openai-beta')}
                headers['Accept-Encoding'] = 'identity'
                try:
                    upstream.request('POST', '/backend-api/codex/responses', body=raw, headers=headers)
                    response = upstream.getresponse()
                    hit['http_status'] = response.status
                    self.send_response(response.status)
                    self.send_header('Content-Type', response.getheader('Content-Type', 'text/event-stream'))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.close_connection = True
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                finally:
                    upstream.close()
        server.RequestHandlerClass = BridgeHandler
        atomic(home / 'config.toml', 'model="gpt-6.1-sol"\nmodel_reasoning_effort="low"\n'
               + f'openai_base_url="http://127.0.0.1:{server.server_port}/v1"\n')
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    atomic(root / 'runtime.json', {'port': server.server_port})
    route = console.native.save_route({'name': 'switch QA', 'mode': 'fixed', 'allow_api': False,
        'primary': [target_id + '-gpt-6.1-sol'], 'regular': []})
    console.native.enable(route['id'])
    report = {'root': str(root), 'cross_account': native_id != target_id,
              'explicit_provider': args.explicit_provider, 'constant_bridge_prototype': args.bridge,
              'restarts_engine_between_turns': not args.hot, 'stages': []}
    threads = {}
    providers = {}
    clients = {}
    loaded = {}
    pids = {}
    markers = {}
    bridge_config = (home / 'config.toml').read_bytes()
    print(json.dumps({'root': str(root), 'cross_account': report['cross_account']}), flush=True)

    def count(tid):
        sid = hashlib.sha256(tid.encode()).hexdigest()[:16]
        with console.native.receipts() as db:
            rows = db.execute('SELECT status,account,model,actual_model FROM requests WHERE session=? ORDER BY started', (sid,)).fetchall()
        return [dict(r) for r in rows]

    def turn(label, key, expect_qpool, model=None):
        c = None
        row = {'stage': label, 'expected_route': 'qpool' if expect_qpool else 'native'}
        first = key not in threads
        marker = markers.setdefault(key, 'SWITCH_QA_' + secrets.token_hex(12))
        try:
            c = clients.get(key)
            if c is None:
                overrides = {'model_provider': providers[key]} if args.explicit_provider and key in providers else None
                c = Client(home=home, overrides=overrides)
                pids.setdefault(key, c.p.pid)
                if args.hot:
                    clients[key] = c
            if args.hot and key in loaded:
                r = loaded[key]
                if model:
                    c.call('thread/settings/update', {'threadId': threads[key], 'model': model})
                    r['model'] = model
                prompt = 'Reply with the exact SWITCH_QA marker from earlier. No tools, no other words.'
            elif key in threads:
                params = {'threadId': threads[key], 'excludeTurns': True}
                if args.explicit_provider:
                    params['modelProvider'] = providers[key]
                if model:
                    params['model'] = model
                r = c.call('thread/resume', params)
                prompt = 'Reply with the exact SWITCH_QA marker from earlier. No tools, no other words.'
            else:
                r = c.call('thread/start', {'cwd': str(workspace), 'approvalPolicy': 'never',
                    'sandbox': 'read-only', 'ephemeral': False})
                threads[key] = r['thread']['id']
                providers[key] = r['modelProvider']
                prompt = 'Remember ' + marker + '. Reply with that marker only. Do not use tools.'
            loaded[key] = r
            tid = threads[key]
            if args.tools and first:
                marker_path = workspace / (key + '-marker.txt')
                marker_path.write_text(marker)
                prompt = ('Use a tool to read this local file: ' + str(marker_path)
                          + '. Remember its exact content and reply with that content only. Do not modify any files.')
            before = count(tid)
            hit_start = len(bridge_hits)
            result = c.turn(tid, prompt)
            after = count(tid)
            row.update(provider=r.get('modelProvider'), model=r.get('model'),
                same_thread=c.call('thread/read', {'threadId': tid, 'includeTurns': False})['thread']['id'] == tid,
                history_recalled=marker in result['text'], item_types=result['item_types'],
                receipt_delta=len(after) - len(before), status=result['status'])
            if args.bridge:
                row.update(engine_pid=c.p.pid, thread_id=tid, transport_hits=bridge_hits[hit_start:],
                           qpool_receipts=after[len(before):], same_process=c.p.pid == pids[key] and c.p.poll() is None)
            row['passed'] = (row['same_thread'] and row['history_recalled'] and
                (len(after) > len(before) if expect_qpool else len(after) == len(before)) and
                (not expect_qpool or after[-1]['status'] == 'completed'))
            if args.bridge:
                hits = row['transport_hits']
                row['passed'] = row['passed'] and row['same_process'] and bool(hits) and all(
                    h['mode'] == ('qpool' if expect_qpool else 'native')
                    and h['session'] == hashlib.sha256(tid.encode()).hexdigest()[:16] for h in hits)
                if not expect_qpool:
                    row['passed'] = row['passed'] and all(h.get('http_status') == 200 for h in hits)
                else:
                    expected_label = next(a.get('label', a['id']) for a in config['accounts'] if a['id'] == target_id)
                    row['passed'] = row['passed'] and all(r['account'] == expected_label for r in row['qpool_receipts'])
                if args.tools and first:
                    row['tool_executed'] = any(t in result['item_types'] for t in
                                              ('commandExecution', 'mcpToolCall', 'dynamicToolCall'))
                    row['passed'] = row['passed'] and row['tool_executed']
        except Exception as exc:
            # Client already redacts credential-sized blobs. Avoid raw protocol objects.
            row.update(passed=False, error=str(exc)[:500])
        finally:
            if c and not args.hot:
                c.close()
        report['stages'].append(row)
        atomic(root / 'report.json', report)
        print(json.dumps(row, ensure_ascii=False), flush=True)

    try:
        if args.bridge:
            def mode(value):
                api(root, '/qa/mode', {'mode': value})
            mode('native')
            turn('native-created', 'native', False)
            mode('qpool')
            turn('native-to-qpool', 'native', True)
            mode('native')
            turn('native-qpool-native', 'native', False)
            mode('qpool')
            turn('qpool-created', 'pool', True)
            mode('native')
            turn('qpool-to-native', 'pool', False)
            mode('qpool')
            turn('qpool-native-qpool', 'pool', True)
            if not args.tools:
                turn('native-second-qpool', 'native', True)
                mode('native')
                turn('native-second-native', 'native', False)
                turn('qpool-second-native', 'pool', False)
        else:
            with patch('qp.desktop.install_startup'), patch('qp.desktop.remove_startup'):
                turn('native-created', 'native', False)
                api(root, '/api/desktop/connect', {})
                turn('native-to-qpool-manual', 'native', True)
                if not args.hot:
                    turn('native-to-qpool-auto', 'native', True, model='qpool-auto')
                turn('qpool-created-other-account', 'pool', True)
                api(root, '/api/desktop/restore', {})
                turn('native-qpool-native', 'native', False)
                turn('qpool-to-native-other-account', 'pool', False)
                api(root, '/api/desktop/connect', {})
                turn('qpool-native-qpool', 'pool', True)
                turn('native-qpool-native-qpool', 'native', True)
                api(root, '/api/desktop/restore', {})
                turn('qpool-second-native', 'pool', False)
                turn('native-second-native', 'native', False)
    except Exception as exc:
        row = {'stage': 'switch-operation', 'passed': False, 'error': str(exc)[:500]}
        report['stages'].append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        for c in clients.values():
            c.close()
        server.shutdown()
        server.server_close()
        console.native.close()
        console.manager.close()
        report['original_files_unchanged'] = all(
            hashlib.sha256(path.read_bytes()).hexdigest() == value for path, value in originals.items())
        report['bridge_config_unchanged'] = (home / 'config.toml').read_bytes() == bridge_config if args.bridge else None
        atomic(root / 'report.json', report)
        # Delete only QA-owned login copies and generated adapter credential copies.
        for path in [home / 'auth.json', *root.glob('accounts/*/auth.json'),
                     *root.glob('native/adapters/*/auth/account.json')]:
            path.unlink(missing_ok=True)
        print(json.dumps({'report': str(root / 'report.json'),
                          'original_files_unchanged': report['original_files_unchanged']}), flush=True)
    if not report['original_files_unchanged'] or not all(r['passed'] for r in report['stages']):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
