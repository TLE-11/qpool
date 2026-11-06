"""Real local HTTP paths, synthetic OAuth, and mode changes between/within turns."""
import copy
import http.client
import io
import json
import threading
import time
import unittest
import uuid
from unittest.mock import patch, Mock

from qp import desktop, desktop_switch
from qp.collectors.base import CollectorError


class HotSwitchTests(unittest.TestCase):
    def setUp(self):
        from test_native import NativeTests
        NativeTests.setUp(self)
        self.original = '# personal\nmodel="gpt-test-large"\nmodel_reasoning_effort="high"\n'
        (self.home / 'config.toml').write_text(self.original)
        # Capture the fixed official destination, but serve it locally.
        def connection():
            return http.client.HTTPConnection('127.0.0.1', self.upstream.server_port, timeout=5)
        patch('qp.desktop_switch.native_connection', side_effect=connection).start()
        desktop_switch.connect(self.native)
        self.config = (self.home / 'config.toml').read_bytes()

    def tearDown(self):
        from test_native import NativeTests
        NativeTests.tearDown(self)

    def body(self, text='翻译 hello'):
        from test_native import NativeTests
        body = NativeTests.body(self, text)
        body['model'] = 'gpt-test-large'
        body['client_metadata'] = {'turn_id': str(uuid.uuid4())}
        return body

    def request(self, body, **kwargs):
        from test_native import NativeTests
        kwargs.setdefault('token', 'synthetic-access-secret')
        kwargs.setdefault('path', '/switch/v1/responses')
        return NativeTests.request(self, body, **kwargs)

    def mode(self, mode):
        c = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        c.request('POST', '/api/desktop/mode', json.dumps({'mode': mode}),
                  {'Content-Type': 'application/json', 'X-Qpool-Request': '1'})
        r = c.getresponse(); data = json.loads(r.read()); c.close()
        self.assertEqual(200, r.status, data)
        self.assertEqual(mode, data['switch_mode'])
        self.assertEqual(self.config, (self.home / 'config.toml').read_bytes())

    def new_turn(self, body, text):
        body = copy.deepcopy(body)
        body['input'].append({'role': 'user', 'content': [{'type': 'input_text', 'text': text}]})
        body['client_metadata'] = {'turn_id': str(uuid.uuid4())}
        return body

    def completed_receipts(self):
        # The response can reach the client before the handler's audit commit.
        until = time.monotonic() + 3
        while time.monotonic() < until:
            with self.native.receipts() as db:
                rows = db.execute('SELECT * FROM requests ORDER BY started').fetchall()
            if all(r['status'] != 'running' for r in rows):
                return rows
            time.sleep(.01)
        self.fail('HTTP handler did not finish its audit')

    def assert_direct(self):
        path, body, headers = self.requests[-1]
        self.assertEqual('/backend-api/codex/responses', path)
        self.assertEqual('Bearer synthetic-access-secret', headers['Authorization'])
        self.assertEqual('test-subject', headers['ChatGPT-Account-Id'])
        self.assertEqual('gpt-test-large', body['model'])
        self.assertNotIn('X-Should-Not-Forward', headers)

    def assert_pool(self):
        path, body, headers = self.requests[-1]
        self.assertEqual('/v1/responses', path)
        self.assertEqual('Bearer private-adapter-key', headers['Authorization'])
        self.assertEqual('gpt-test-small', body['model'])
        self.assertNotIn('ChatGPT-Account-Id', headers)

    def test_both_round_trips_keep_config_model_and_account_paths(self):
        for initial in ('native', 'qpool'):
            body = self.body()
            session = {'session_id': initial}
            for index, mode in enumerate((initial, 'qpool' if initial == 'native' else 'native', initial)):
                self.mode(mode)
                if index:
                    body = self.new_turn(body, '翻译 next ' + str(index))
                self.assertEqual(200, self.request(body, headers=session)[0])
                self.assert_direct() if mode == 'native' else self.assert_pool()
                self.assertEqual(body['input'], self.requests[-1][1]['input'])
        self.assertEqual('openai', desktop.read_config(self.home / 'config.toml')[1]['model_provider'])
        self.assertEqual(6, sum(r['status'] == 'completed' for r in self.completed_receipts()))

    def test_toggle_during_turn_keeps_retries_tools_and_compact_pinned(self):
        body = self.body()
        self.mode('native')
        self.assertEqual(200, self.request(body)[0])
        self.mode('qpool')
        self.assertEqual(200, self.request(body)[0])  # same-turn retry
        self.assert_direct()
        body['input'].append({'type': 'function_call_output', 'call_id': '1', 'output': 'ok'})
        self.native.bindings.clear()  # mode pin is durable, not an in-memory toggle
        self.assertEqual(200, self.request(body)[0]); self.assert_direct()
        compact = dict(body); compact.pop('model')
        self.assertEqual(200, self.request(compact, path='/switch/v1/responses/compact')[0])
        self.assertEqual('/backend-api/codex/responses/compact', self.requests[-1][0])
        body = self.new_turn(body, '翻译 next task')
        self.assertEqual(200, self.request(body)[0]); self.assert_pool()
        self.mode('native')
        body['input'].append({'type': 'function_call_output', 'call_id': '2', 'output': 'ok'})
        self.assertEqual(200, self.request(body)[0]); self.assert_pool()
        self.assertEqual(200, self.request(self.new_turn(body, '翻译 final'))[0]); self.assert_direct()

    def test_approval_keeps_native_model_and_does_not_overwrite_turn_pin(self):
        body = self.body()
        self.assertEqual(200, self.request(body)[0]); self.assert_pool()
        approval = dict(body, model='codex-auto-review')
        self.assertEqual(200, self.request(approval)[0])
        self.assertEqual('/backend-api/codex/responses', self.requests[-1][0])
        self.assertEqual('codex-auto-review', self.requests[-1][1]['model'])
        self.mode('native')
        body['input'].append({'type': 'function_call_output', 'call_id': '1', 'output': 'ok'})
        self.assertEqual(200, self.request(body)[0]); self.assert_pool()

    def test_native_mode_survives_disabled_pool_and_rejects_wrong_identity(self):
        self.mode('native')
        self.native.disable()
        self.assertEqual(200, self.request(self.body())[0]); self.assert_direct()
        self.assertEqual(401, self.request(self.new_turn(self.body(), 'next'), token='not-native')[0])
        self.assertEqual(1, len(self.requests))
        with self.assertRaises(CollectorError):
            desktop_switch.set_mode(self.native, 'qpool')

    def test_restoration_does_not_migrate_or_mutate_chat_history(self):
        with patch('qp.codex_recovery.recover') as recover:
            desktop.restore(self.native)
            recover.assert_not_called()
        self.assertEqual(self.original, (self.home / 'config.toml').read_text())
        self.assertEqual(401, self.request(self.body())[0])

    def test_mode_change_applies_after_an_inflight_native_request(self):
        entered = threading.Event(); release = threading.Event()
        original = desktop_switch.forward_native
        def hold(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test gate timed out')
            return original(*args)
        self.mode('native')
        results = []
        with patch('qp.desktop_switch.forward_native', side_effect=hold):
            thread = threading.Thread(target=lambda: results.append(self.request(self.body())))
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.mode('qpool')
            finally:
                release.set(); thread.join(5)
        self.assertEqual(200, results[0][0]); self.assert_direct()
        self.assertEqual(200, self.request(self.new_turn(self.body(), '翻译 next'))[0]); self.assert_pool()

    def test_failure_is_not_replayed_on_other_mode_or_account(self):
        self.mode('native'); self.failure = True
        status, raw = self.request(self.body())
        self.assertEqual(429, status)
        self.assertEqual(1, len(self.requests))
        self.assertNotIn(b'secret upstream detail', raw)
        self.assertEqual('failed', self.completed_receipts()[0]['status'])

    def test_missing_continuation_binding_is_rejected_without_guessing(self):
        body = self.body()
        body.pop('client_metadata')
        body['input'].append({'type': 'function_call_output', 'call_id': 'unknown', 'output': 'ok'})
        self.assertEqual(502, self.request(body)[0])
        self.assertEqual([], self.requests)

    def test_compaction_reconstructs_history_without_changing_turn_mode(self):
        self.mode('native')
        body = self.new_turn(self.body(), 'the current task')
        self.assertEqual(200, self.request(body)[0])
        self.mode('qpool')
        self.assertEqual(200, self.request(body, path='/switch/v1/responses/compact')[0])
        reconstructed = dict(body, input=[{'role': 'developer', 'content': 'summary'}, body['input'][-1]])
        self.assertEqual(200, self.request(reconstructed)[0]); self.assert_direct()
        # Even the exact same text is a new user turn when Codex supplies a new ID.
        next_turn = self.new_turn(reconstructed, 'the current task')
        self.assertEqual(200, self.request(next_turn)[0])
        self.assertEqual('/v1/responses', self.requests[-1][0])

    def test_inline_compaction_with_its_own_turn_id_keeps_previous_mode(self):
        self.mode('native'); body = self.body()
        self.assertEqual(200, self.request(body)[0])
        self.mode('qpool')
        compact = self.new_turn(body, 'summarize history')
        compact['client_metadata']['x-codex-turn-metadata'] = json.dumps({'compaction': {}})
        self.assertEqual(200, self.request(compact)[0]); self.assert_direct()
        self.assertEqual(200, self.request(self.new_turn(body, '翻译 next'))[0]); self.assert_pool()

    def test_native_transport_preserves_protocol_negotiation_headers(self):
        self.mode('native')
        self.assertEqual(200, self.request(self.body(), headers={
            'x-openai-internal-codex-responses-lite': 'true', 'x-codex-beta-features': 'test'})[0])
        headers = {k.lower(): v for k, v in self.requests[-1][2].items()}
        self.assertEqual('true', headers['x-openai-internal-codex-responses-lite'])
        self.assertEqual('test', headers['x-codex-beta-features'])

    def test_streaming_response_without_content_type_is_not_treated_as_json(self):
        self.mode('native')
        stream = io.BytesIO(b'data: {"type":"response.completed","response":{"model":"gpt-test-large","status":"completed"}}\n\n')
        response = Mock(status=200, readline=stream.readline)
        response.getheader.return_value = None
        connection = Mock(); connection.getresponse.return_value = response
        with patch('qp.desktop_switch.native_connection', return_value=connection):
            self.assertEqual(200, self.request(self.body())[0])
        self.assertEqual('completed', self.completed_receipts()[0]['status'])

    def test_non_streaming_failed_response_is_not_a_successful_receipt(self):
        self.mode('native')
        response = Mock(status=200)
        response.getheader.return_value = 'application/json'
        response.read.return_value = b'{"status":"failed","error":{"code":"test_failure"}}'
        connection = Mock(); connection.getresponse.return_value = response
        with patch('qp.desktop_switch.native_connection', return_value=connection):
            self.assertEqual(200, self.request(dict(self.body(), stream=False))[0])
        self.assertEqual('failed', self.completed_receipts()[0]['status'])

    def test_incompatible_context_and_effort_are_rejected_before_upstream(self):
        for field, value in (('context_window', 10), ('supported_reasoning_levels', [{'effort': 'low'}])):
            known = self.native.model_metadata()
            known['gpt-test-small'][field] = value
            body = self.body(); body['reasoning'] = {'effort': 'medium'}
            with patch.object(self.native, 'model_metadata', return_value=known):
                status, _ = self.request(body, headers={'session_id': field})
            self.assertEqual(502, status)
        self.assertEqual([], self.requests)

    def test_foreign_turn_metadata_is_rejected(self):
        body = self.body(); body['client_metadata']['thread_id'] = 'other-session'
        self.assertEqual(502, self.request(body)[0])
        self.assertEqual([], self.requests)

    def test_legacy_connect_cannot_replace_the_hot_switch_provider(self):
        with self.assertRaises(CollectorError):
            desktop.connect(self.native)
        self.assertEqual(self.config, (self.home / 'config.toml').read_bytes())

    def upgrade_legacy_chat(self):
        desktop.restore(self.native)
        desktop.connect(self.native)
        body = self.body()
        body['input'].insert(0, {'type': 'reasoning', 'encrypted_content': 'synthetic-old-history'})
        self.assertEqual(200, self.request(body, path='/legacy/v1/responses')[0])
        self.assertEqual('gpt-test-large', self.requests[-1][1]['model'])
        desktop.restore(self.native)
        desktop_switch.connect(self.native)
        self.config = (self.home / 'config.toml').read_bytes()
        return body

    def test_upgraded_legacy_chat_switches_both_ways_and_keeps_account(self):
        body = self.upgrade_legacy_chat()
        self.native.bindings.clear()  # Exercise the persisted pre-upgrade binding.
        for mode in ('native', 'qpool', 'native', 'qpool'):
            self.mode(mode)
            body = self.new_turn(body, '翻译 continue after upgrade')
            status, raw = self.request(body)
            self.assertEqual(200, status, raw)
            self.assert_direct() if mode == 'native' else self.assert_pool()
            self.assertEqual(body['input'], self.requests[-1][1]['input'])
            self.native.bindings.clear()
        self.mode('native')
        body['input'].append({'type': 'function_call_output', 'call_id': 'upgrade-tool', 'output': 'ok'})
        self.assertEqual(200, self.request(body)[0]); self.assert_pool()
        self.native.bindings.clear()
        self.assertEqual(200, self.request(body, path='/switch/v1/responses/compact')[0])
        self.assertEqual('gpt-test-small', self.requests[-1][1]['model'])
        self.assertEqual(200, self.request(self.new_turn(body, 'next turn'))[0]); self.assert_direct()
        with self.native.receipts() as db:
            saved = db.execute('SELECT * FROM legacy_bindings').fetchone()
            self.assertEqual(self.aid, saved['account'])
            self.assertEqual(self.aid, json.loads(saved['binding'])['channel']['account_id'])
            self.assertEqual(0, db.execute('SELECT count(*) FROM session_bindings').fetchone()[0])

    def test_upgraded_legacy_chat_rejects_changed_identity_before_first_switch(self):
        from qp import profiles
        body = self.upgrade_legacy_chat()
        self.native.bindings.clear()
        # Keep the pool's old credentials valid: reject the different native
        # identity, not merely a stale credential reference after sign-in.
        old_login = self.root / 'old-login.json'
        old_login.write_bytes((self.home / 'auth.json').read_bytes())
        config = json.loads(profiles.config_path().read_text())
        account = next(a for a in config['accounts'] if a['id'] == self.aid)
        account['source']['credentials_file'] = str(old_login)
        profiles.config_path().write_text(json.dumps(config))
        (self.home / 'auth.json').write_text(json.dumps({'tokens': {
            'account_id': 'other-subject', 'access_token': 'other-synthetic-token'}}))
        for mode in ('qpool', 'native'):
            self.mode(mode)
            self.assertEqual(502, self.request(self.new_turn(body, 'next'), token='other-synthetic-token')[0])
        self.assertEqual(1, len(self.requests))
        with self.native.receipts() as db:
            self.assertEqual(self.aid, db.execute('SELECT account FROM legacy_bindings').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT count(*) FROM session_bindings').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT count(*) FROM desktop_turns').fetchone()[0])
        (self.home / 'auth.json').write_bytes(old_login.read_bytes())
        self.mode('qpool')
        self.assertEqual(200, self.request(self.new_turn(body, '翻译 retry with original login'))[0])
        self.assert_pool()

    def test_upgraded_legacy_chat_keeps_route_and_entry_isolation(self):
        from test_native import NativeTests
        body = self.upgrade_legacy_chat()
        # A qpool bearer alone still cannot take over this old native chat.
        self.assertEqual(502, NativeTests.request(self, dict(body, model='qpool-auto'))[0])
        changed = self.native.save_route({'name': 'other route', 'mode': 'fixed',
                                          'regular': [], 'primary': [self.large]})
        self.native.enable(changed['id'])
        self.assertEqual(502, self.request(self.new_turn(body, 'next'))[0])
        self.assertEqual(1, len(self.requests))
        with self.native.receipts() as db:
            self.assertEqual(self.route['id'], db.execute('SELECT route FROM legacy_bindings').fetchone()[0])
        self.native.enable(self.route['id'])
        self.assertEqual(200, self.request(self.new_turn(body, '翻译 retry after restoring route'))[0])
        self.assert_pool()

    def test_image_only_task_can_use_qpool_mode(self):
        body = self.body(); body['input'][0]['content'] = [{'type': 'input_image', 'image_url': 'data:image/png;base64,dGVzdA=='}]
        self.assertEqual(200, self.request(body)[0])
        self.assertEqual(body['input'], self.requests[-1][1]['input'])

    def test_qpool_inline_compaction_and_reconstruction_keep_execution_model(self):
        body = self.body('翻译 hello')
        self.assertEqual(200, self.request(body)[0]); self.assert_pool()
        self.mode('native')
        compact = self.new_turn(body, '设计复杂架构并总结历史')
        compact['client_metadata']['x-codex-turn-metadata'] = json.dumps({'compaction': {}})
        self.assertEqual(200, self.request(compact)[0]); self.assert_pool()
        # Reconstructed history would choose the primary model if reclassified.
        reconstructed = dict(body, input=[{'role': 'user', 'content': '设计复杂架构'}])
        self.assertEqual(200, self.request(reconstructed)[0]); self.assert_pool()
        self.assertEqual(200, self.request(self.new_turn(reconstructed, 'next actual user turn'))[0]); self.assert_direct()

    def test_approval_cannot_bypass_existing_native_identity_binding(self):
        body = self.body()
        self.assertEqual(200, self.request(body)[0])
        (self.home / 'auth.json').write_text(json.dumps({'tokens': {
            'account_id': 'other-subject', 'access_token': 'other-synthetic-token'}}))
        self.assertEqual(502, self.request(self.new_turn(body, 'next'), token='other-synthetic-token')[0])
        self.assertEqual(502, self.request(dict(body, model='codex-auto-review'), token='other-synthetic-token')[0])
        self.assertEqual(1, len(self.requests))
