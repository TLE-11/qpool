"""Opt-in live verification of the production switch endpoints, in an isolated home.

Only existing subscription logins are used. Refresh tokens are removed from QA
copies. No LaunchAgent, original config, original chat, or API billing is changed.
"""
import argparse
import copy
import hashlib
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
    parser.add_argument('--source-root', type=Path, required=True,
                        help='Existing repository containing .qpool/console/pools.json')
    parser.add_argument('--observe-transport', action='store_true', help='Log safe response protocol metadata only.')
    parser.add_argument('--legacy-upgrade', action='store_true',
                        help='Create an old OpenAI chat through /legacy, upgrade, then switch without reloads.')
    args = parser.parse_args()
    from qp.native import atomic
    from qp import web, desktop_switch
    root = REPO / '.qpool' / ('hot-switch-live-' + str(time.time_ns()))
    home = root / 'codex-home'
    workspace = root / 'workspace'
    originals, clients, threads, pids, markers = {}, {}, {}, {}, {}
    report = {'root': str(root), 'production_endpoints': True, 'stages': []}
    server = console = None
    source_home = Path.home() / '.codex'
    observer = None
    if args.observe_transport:
        original_connection = desktop_switch.native_connection
        def observe():
            connection = original_connection()
            getresponse = connection.getresponse
            def response():
                result = getresponse()
                print(json.dumps({'http_status': result.status, 'content_type': result.getheader('Content-Type'),
                                  'content_encoding': result.getheader('Content-Encoding')}), flush=True)
                readline = result.readline
                seen = set()
                def line(*args):
                    value = readline(*args)
                    if value.startswith(b'data: '):
                        try:
                            data = json.loads(value[6:])
                            event = data.get('type')
                            if event not in seen:
                                seen.add(event)
                                print(json.dumps({'sse_type': event, 'keys': list(data),
                                    'error_code': (data.get('error') or {}).get('code') if isinstance(data.get('error'), dict) else None}), flush=True)
                        except (ValueError, AttributeError):
                            pass
                    return value
                result.readline = line
                return result
            connection.getresponse = response
            return connection
        observer = patch('qp.desktop_switch.native_connection', side_effect=observe)
        observer.start()

    def read_original(path):
        path = Path(path)
        value = path.read_bytes()
        originals[path] = hashlib.sha256(value).hexdigest()
        return value

    def copy_login(raw, destination):
        login = copy.deepcopy(raw)
        login['tokens']['refresh_token'] = ''
        atomic(destination, login)

    try:
        home.mkdir(parents=True, mode=0o700)
        workspace.mkdir()
        read_original(source_home / 'config.toml')
        auth = json.loads(read_original(source_home / 'auth.json'))
        subject = auth['tokens']['account_id']
        copy_login(auth, home / 'auth.json')
        del auth
        atomic(home / 'models_cache.json', (source_home / 'models_cache.json').read_text())
        atomic(home / 'config.toml', 'model="gpt-6.1-sol"\nmodel_reasoning_effort="low"\n')
        config = json.loads(read_original(args.source_root / '.qpool/console/pools.json'))
        config['accounts'] = [a for a in config['accounts'] if a['agent'] == 'codex']
        for account in config['accounts']:
            login = json.loads(read_original(Path(account['source']['credentials_file'])))
            account_home = root / 'accounts' / account['id']
            copy_login(login, account_home / 'auth.json')
            del login
            account['source']['credentials_file'] = str(account_home / 'auth.json')
            account['source']['profile_dir'] = str(account_home)
        native_id = next(a['id'] for a in config['accounts'] if a['source']['expected_subject'] == subject)
        target_id = native_id if args.legacy_upgrade else next(a['id'] for a in config['accounts'] if a['id'] != native_id)
        target_label = next(a.get('label', a['id']) for a in config['accounts'] if a['id'] == target_id)
        ids = {a['id'] for a in config['accounts']}
        config['channels'] = [c for c in config['channels'] if c['account_id'] in ids]
        atomic(root / 'pools.json', config)
        os.environ.update(CODEX_HOME=str(home), QPOOL_CONFIG=str(root / 'pools.json'),
                          QPOOL_DB=str(root / 'ledger.db'), QPOOL_COLLECTORS_DIR=str(root / 'no-plugins'))
        console = web.Console(root / 'ledger.db')
        server = web.Server(('127.0.0.1', 0), console)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        atomic(root / 'runtime.json', {'port': server.server_port})
        route = console.native.save_route({'name': 'hot-switch live QA', 'mode': 'fixed',
            'allow_api': False, 'primary': [target_id + '-gpt-6.1-sol'], 'regular': []})
        console.native.enable(route['id'])
        with patch('qp.desktop.install_startup'):
            api(root, '/api/desktop/connect' if args.legacy_upgrade else '/api/desktop/hot-connect', {})
        report.update(cross_account=not args.legacy_upgrade, native_account=native_id, pool_account=target_id)
        print(json.dumps({'root': str(root), 'cross_account': report['cross_account']}), flush=True)

        def receipts(tid):
            sid = hashlib.sha256(tid.encode()).hexdigest()[:16]
            until = time.monotonic() + 3
            while True:
                with console.native.receipts() as db:
                    rows = [dict(r) for r in db.execute('SELECT status,model,actual_model,account,entry_mode FROM requests WHERE session=? ORDER BY started', (sid,))]
                if all(r['status'] != 'running' for r in rows) or time.monotonic() > until:
                    return rows
                time.sleep(.01)

        if args.legacy_upgrade:
            client = clients['legacy'] = Client(home=home, overrides={'model_provider': 'openai', 'model': 'gpt-6.1-sol'})
            start = client.call('thread/start', {'cwd': str(workspace), 'approvalPolicy': 'never',
                'sandbox': 'read-only', 'ephemeral': False, 'modelProvider': 'openai', 'model': 'gpt-6.1-sol'})
            assert start['modelProvider'] == 'openai'
            tid = threads['legacy'] = start['thread']['id']
            markers['legacy'] = 'HOT_SWITCH_' + secrets.token_hex(12)
            marker_file = workspace / 'legacy.txt'
            marker_file.write_text(markers['legacy'])
            response = client.turn(tid, 'Use a tool to read ' + str(marker_file) +
                '. Remember its content and reply with that content only. Do not modify any files.')
            rows = receipts(tid)
            assert markers['legacy'] in response['text'] and 'commandExecution' in response['item_types']
            assert rows and all(r['status'] == 'completed' for r in rows)
            with console.native.receipts() as db:
                saved = db.execute('SELECT account FROM legacy_bindings WHERE session=?',
                    (hashlib.sha256(tid.encode()).hexdigest()[:16],)).fetchone()
                assert saved and saved['account'] == native_id
            report['legacy_creation'] = {'thread_id': tid, 'engine_pid': client.p.pid,
                'history_recalled': True, 'item_types': response['item_types'],
                'legacy_binding_verified': True, 'receipts': rows}
            client.close()
            del clients['legacy']
            # Initial endpoint installation needs one reload. All three turns
            # below share the resumed engine; switching itself never reloads it.
            with patch('qp.desktop.remove_startup'), patch('qp.desktop.install_startup'):
                api(root, '/api/desktop/restore', {})
                api(root, '/api/desktop/hot-connect', {})
            client = clients['legacy'] = Client(home=home)
            resumed = client.call('thread/resume', {'threadId': tid, 'excludeTurns': True})
            assert resumed['thread']['id'] == tid and resumed['modelProvider'] == 'openai'
            pids['legacy'] = client.p.pid
            report['upgrade'] = {'initial_reload': True, 'resumed_same_thread': True,
                                 'engine_pid_after_upgrade': client.p.pid}
        config_hash = hashlib.sha256((home / 'config.toml').read_bytes()).hexdigest()

        def turn(key, mode, label):
            result = api(root, '/api/desktop/mode', {'mode': mode})
            assert result['switch_mode'] == mode
            assert hashlib.sha256((home / 'config.toml').read_bytes()).hexdigest() == config_hash
            first = key not in clients
            if first:
                clients[key] = Client(home=home)
                pids[key] = clients[key].p.pid
                start = clients[key].call('thread/start', {'cwd': str(workspace), 'approvalPolicy': 'never',
                    'sandbox': 'read-only', 'ephemeral': False})
                assert start['modelProvider'] == 'openai'
                threads[key] = start['thread']['id']
                markers[key] = 'HOT_SWITCH_' + secrets.token_hex(12)
                marker_file = workspace / (key + '.txt')
                marker_file.write_text(markers[key])
                prompt = ('Use a tool to read ' + str(marker_file)
                          + '. Remember its content and reply with that content only. Do not modify any files.')
            else:
                prompt = 'Reply with the exact HOT_SWITCH marker you read earlier. Do not use tools or add other words.'
            client, tid = clients[key], threads[key]
            before = len(receipts(tid))
            response = client.turn(tid, prompt)
            rows = receipts(tid)[before:]
            row = {'stage': label, 'mode': mode, 'thread_id': tid, 'engine_pid': client.p.pid,
                   'same_process': pids[key] == client.p.pid and client.p.poll() is None,
                   'same_thread': client.call('thread/read', {'threadId': tid, 'includeTurns': False})['thread']['id'] == tid,
                   'history_recalled': markers[key] in response['text'], 'item_types': response['item_types'],
                   'receipts': rows, 'passed': False}
            expected_mode = 'native' if mode == 'native' else None
            row['passed'] = (row['same_process'] and row['same_thread'] and row['history_recalled'] and bool(rows)
                and all(r['status'] == 'completed' and r['model'] == 'gpt-6.1-sol'
                        and r['entry_mode'] == expected_mode for r in rows)
                and (mode == 'native' or all(r['account'] == target_label for r in rows))
                and (not first or 'commandExecution' in row['item_types']))
            report['stages'].append(row)
            atomic(root / 'report.json', report)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            assert row['passed'], 'production hot-switch verification failed'

        if args.legacy_upgrade:
            turn('legacy', 'native', 'upgraded-native')
            turn('legacy', 'qpool', 'upgraded-next-turn-qpool')
            turn('legacy', 'native', 'upgraded-next-turn-native')
        else:
            run_round_trips(turn)
        report['config_unchanged_during_switches'] = hashlib.sha256((home / 'config.toml').read_bytes()).hexdigest() == config_hash
    finally:
        for client in clients.values():
            client.close()
        if server:
            server.shutdown(); server.server_close()
        if console:
            console.native.close(); console.manager.close()
        if observer:
            observer.stop()
        report['original_files_unchanged'] = all(hashlib.sha256(path.read_bytes()).hexdigest() == value
                                               for path, value in originals.items())
        for path in [home / 'auth.json', *root.glob('accounts/*/auth.json'),
                     *root.glob('native/adapters/*/auth/account.json')]:
            path.unlink(missing_ok=True)
        atomic(root / 'report.json', report)
        print(json.dumps({'report': str(root / 'report.json'),
                          'original_files_unchanged': report['original_files_unchanged']}), flush=True)
    assert len(report['stages']) == (3 if args.legacy_upgrade else 9) and all(r['passed'] for r in report['stages'])
    assert report['original_files_unchanged'] and report['config_unchanged_during_switches']


def run_round_trips(turn):
    turn('native', 'native', 'native-created-with-tool')
    turn('native', 'qpool', 'native-to-qpool')
    turn('native', 'native', 'native-qpool-native')
    turn('pool', 'qpool', 'qpool-created-with-tool')
    turn('pool', 'native', 'qpool-to-native')
    turn('pool', 'qpool', 'qpool-native-qpool')
    turn('native', 'qpool', 'native-second-qpool')
    turn('native', 'native', 'native-second-native')
    turn('pool', 'native', 'qpool-second-native')


if __name__ == '__main__':
    main()
