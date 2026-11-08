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
    parser.add_argument('--control', action='store_true', help='Use the real control proxy and route Sol to Astra.')
    parser.add_argument('--control-compact', action='store_true',
                        help='Force small-window model compaction with an isolated 32k catalog and injected filler.')
    parser.add_argument('--control-queued', action='store_true', help='Use the desktop queued-followup protocol for pool turns.')
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
    http_observer = None
    if args.observe_transport:
        respond = web.Handler.respond
        def observe_error(handler, status, value, *rest):
            if status >= 400:
                print(json.dumps({'http_error': status, 'detail': value}, ensure_ascii=False), flush=True)
            if status == 401:
                from qp import profiles
                from qp.collectors import codex
                checks = []
                for account in profiles.load()['accounts'].values():
                    try:
                        creds = profiles.credentials(account, codex)
                        checks.append({'id': account['id'], 'token_matches': handler.headers.get('Authorization') == 'Bearer ' + creds['access_token'],
                                       'subject_matches': handler.headers.get('ChatGPT-Account-Id') == creds['account_id']})
                    except Exception:
                        checks.append({'id': account['id'], 'credentials_readable': False})
                print(json.dumps({'auth_checks': checks}), flush=True)
            return respond(handler, status, value, *rest)
        http_observer = patch.object(web.Handler, 'respond', observe_error)
        http_observer.start()
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
        target_id = native_id if args.legacy_upgrade or args.control else next(a['id'] for a in config['accounts'] if a['id'] != native_id)
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
        pool_model = 'gpt-6-astra' if args.control else 'gpt-6.1-sol'
        route = console.native.save_route({'name': 'hot-switch live QA', 'mode': 'fixed',
            'allow_api': False, 'primary': [target_id + '-' + pool_model], 'regular': []})
        console.native.enable(route['id'])
        with patch('qp.desktop.install_startup'):
            api(root, '/api/desktop/connect' if args.legacy_upgrade else '/api/desktop/hot-connect', {})
        report.update(cross_account=target_id != native_id, native_account=native_id, pool_account=target_id)
        print(json.dumps({'root': str(root), 'cross_account': report['cross_account']}), flush=True)
        def control_binary():
            if not args.control:
                return None
            from qp.control import install
            return install(console.native)['wrapper']

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
            if args.control_compact:
                assert args.control, '--control-compact requires --control'
                catalog = json.loads((home / 'models_cache.json').read_text())
                for model in catalog['models']:
                    if model['slug'] == pool_model:
                        model['context_window'] = 32000
                        model['max_context_window'] = 32000
                atomic(home / 'compact-test-models.json', catalog)
                with (home / 'config.toml').open('a') as config_file:
                    config_file.write('\nmodel_catalog_json=' + json.dumps(str(home / 'compact-test-models.json')) + '\n')
                report['compaction_test_window'] = 32000
            client = clients['legacy'] = Client(home=home, binary=control_binary(),
                overrides={'features.code_mode_host': True} if args.control else None)
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
                clients[key] = Client(home=home, binary=control_binary(),
                    overrides={'features.code_mode_host': True} if args.control else None)
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
            if args.control_compact and label == 'upgraded-next-turn-qpool':
                filler = ' '.join('filler_%06d' % i for i in range(22000))
                client.call('thread/inject_items', {'threadId': tid, 'items': [
                    {'type': 'message', 'role': 'user', 'content': [
                        {'type': 'input_text', 'text': 'Irrelevant test padding; retain the earlier HOT_SWITCH marker. ' + filler}]}]})
            before = len(receipts(tid))
            previous_model = client.call('thread/read', {'threadId': tid})['thread']['model']
            queued = args.control_queued and mode == 'qpool'
            response = client.turn(tid, prompt, queued=queued)
            rows = receipts(tid)[before:]
            row = {'stage': label, 'mode': mode, 'thread_id': tid, 'engine_pid': client.p.pid,
                   'same_process': pids[key] == client.p.pid and client.p.poll() is None,
                   'same_thread': client.call('thread/read', {'threadId': tid, 'includeTurns': False})['thread']['id'] == tid,
                   'history_recalled': markers[key] in response['text'], 'item_types': response['item_types'],
                   'receipts': rows, 'queued': queued, 'passed': False}
            expected_mode = 'native' if mode == 'native' else None
            expected_model = 'gpt-6.1-sol' if mode == 'native' else pool_model
            if args.control:
                configured = client.call('thread/read', {'threadId': tid, 'includeTurns': False})['thread']['model']
                row['codex_configured_model'] = configured
                assert configured == expected_model, 'Codex did not adopt routed model'
                row['settings_notifications'] = response['settings_notifications']
                if previous_model != expected_model:
                    assert response['settings_notifications'] and response['settings_notifications'][-1]['model'] == expected_model, 'desktop model notification missing'
            execution_rows = rows
            if args.control_compact and label == 'upgraded-next-turn-qpool':
                assert rows[0]['entry_mode'] == 'native' and rows[0]['model'] == 'gpt-6.1-sol'
                assert rows[0]['status'] == 'completed'
                execution_rows = rows[1:]
                row['pre_switch_compaction_used_previous_model'] = True
            row['passed'] = (row['same_process'] and row['same_thread'] and row['history_recalled'] and bool(execution_rows)
                and all(r['status'] == 'completed' and r['model'] == expected_model
                        and r['entry_mode'] == expected_mode for r in execution_rows)
                and (mode == 'native' or all(r['account'] == target_label for r in execution_rows))
                and (not first or 'commandExecution' in row['item_types']))
            report['stages'].append(row)
            atomic(root / 'report.json', report)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            assert row['passed'], 'production hot-switch verification failed'
            if args.control_compact and label == 'upgraded-next-turn-qpool':
                assert 'contextCompaction' in row['item_types'], 'expected actual model-switch compaction'
            if queued:
                assert not client.call('thread/queue/list', {'threadId': tid})['data'], 'queued input was not consumed'

        if args.legacy_upgrade:
            turn('legacy', 'native', 'upgraded-native')
            turn('legacy', 'qpool', 'upgraded-next-turn-qpool')
            turn('legacy', 'native', 'upgraded-next-turn-native')
        else:
            run_round_trips(turn)
        if args.control_queued:
            import uuid
            key = 'legacy' if args.legacy_upgrade else 'native'
            client, tid = clients[key], threads[key]
            api(root, '/api/desktop/mode', {'mode': 'native'})
            before = len(receipts(tid))
            client.call('turn/start', {'threadId': tid, 'input': [{'type': 'text', 'text':
                'Use a terminal tool to run python3 -c "import time; time.sleep(8); print(123)". '
                'Wait for the tool to finish, then reply only QUEUE_GATE. Do not modify files.'}]})
            def enqueue(suffix):
                inputs = [{'type': 'text', 'text': 'Reply only with the earlier HOT_SWITCH marker followed by ' + suffix}]
                return client.call('thread/queue/add', {'threadId': tid, 'input': inputs,
                    'clientUserMessageId': str(uuid.uuid4())})['queuedSubmission']
            a, b, deleted = enqueue('QUEUE_A'), enqueue('QUEUE_B'), enqueue('SHOULD_NOT_RUN')
            client.call('thread/queue/update', {'threadId': tid, 'queuedSubmissionId': a['id'],
                'input': [{'type': 'text', 'text': 'Reply only with the earlier HOT_SWITCH marker followed by QUEUE_EDITED'}]})
            client.call('thread/queue/reorder', {'threadId': tid,
                'queuedSubmissionIds': [b['id'], a['id'], deleted['id']]})
            client.call('thread/queue/delete', {'threadId': tid, 'queuedSubmissionId': deleted['id']})
            pending = client.call('thread/queue/list', {'threadId': tid})['data']
            assert [i['id'] for i in pending] == [b['id'], a['id']], 'follow-ups did not wait behind the running turn'
            api(root, '/api/desktop/mode', {'mode': 'qpool'})
            responses = [client.wait_turn(tid) for _ in range(3)]
            assert 'QUEUE_GATE' in responses[0]['text'] and 'commandExecution' in responses[0]['item_types']
            for r, suffix in zip(responses[1:], ['QUEUE_B', 'QUEUE_EDITED']):
                assert markers[key] in r['text'] and suffix in r['text']
            assert len({r['turn_id'] for r in responses}) == 3, 'follow-up was steered into another turn'
            rows = receipts(tid)[before:]
            assert all(r['status'] == 'completed' for r in rows)
            # The deliberately tiny catalog can compact repeatedly during the
            # two follow-ups. Validate the native/pool boundary, not a fixed
            # number of HTTP requests per turn; turn IDs prove separate inputs.
            boundary = next(i for i, r in enumerate(rows) if r['actual_model'] == pool_model)
            assert boundary > 0
            assert all(r['actual_model'] == 'gpt-6.1-sol' and r['entry_mode'] == 'native' for r in rows[:boundary])
            assert len(rows[boundary:]) >= 2 and all(r['actual_model'] == pool_model and r['entry_mode'] is None for r in rows[boundary:])
            if len(rows[boundary:]) > 2:
                assert any('contextCompaction' in r['item_types'] for r in responses[1:]), 'extra requests require observed compaction'
            assert not client.call('thread/queue/list', {'threadId': tid})['data']
            report['queued_while_busy'] = {'passed': True, 'thread_id': tid, 'engine_pid': client.p.pid,
                'ordered_suffixes': ['QUEUE_GATE', 'QUEUE_B', 'QUEUE_EDITED'], 'deleted_not_run': True,
                'mode_changed_while_waiting': True, 'turn_ids': [r['turn_id'] for r in responses],
                'item_types': [r['item_types'] for r in responses], 'receipts': rows}
            print(json.dumps(report['queued_while_busy'], ensure_ascii=False), flush=True)
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
        if http_observer:
            http_observer.stop()
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
