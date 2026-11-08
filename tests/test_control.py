"""Control decisions and HTTP execution must agree across mode/model boundaries."""
import copy
import json
import io
import os
import subprocess
import sys
import threading
import tempfile
import unittest
import uuid
from unittest.mock import patch

from qp import control
from qp.control_proxy import Proxy, RpcError, decision_items
from qp.collectors.base import CollectorError
import test_desktop_switch as hot


class ControlTests(unittest.TestCase):
    setUp = hot.HotSwitchTests.setUp
    tearDown = hot.HotSwitchTests.tearDown
    request = hot.HotSwitchTests.request
    body = hot.HotSwitchTests.body
    mode = hot.HotSwitchTests.mode
    completed_receipts = hot.HotSwitchTests.completed_receipts

    def prepare(self, model='gpt-test-large', text='翻译 hello', **extra):
        data = {'threadId': 'native-session', 'model': model, 'effort': 'high',
                'items': self.body(text)['input'], **extra}
        return control.prepare(self.native, data, {'Authorization': 'Bearer synthetic-access-secret'})

    def controlled(self, plan):
        body = self.body()
        body.update(model=plan['model'], reasoning={'effort': plan['effort']})
        body['client_metadata']['x-codex-turn-metadata'] = json.dumps({control.METADATA_KEY: plan['token']})
        return body

    def test_native_pool_native_uses_effective_model_not_advertised_source_window(self):
        path = self.home / 'models_cache.json'
        models = json.loads(path.read_text())
        models['models'][0]['context_window'] = 100000
        path.write_text(json.dumps(models))
        self.mode('native')
        native = self.prepare()
        self.assertEqual('gpt-test-large', native['model'])
        self.assertEqual(200, self.request(self.controlled(native))[0])
        self.mode('qpool')
        pool = self.prepare()
        self.assertEqual('gpt-test-small', pool['model'])
        self.assertEqual(200, self.request(self.controlled(pool))[0])
        self.assertEqual('gpt-test-small', self.requests[-1][1]['model'])
        self.mode('native')
        restored = self.prepare(model=pool['model'])
        self.assertEqual('gpt-test-large', restored['model'])
        self.assertEqual(200, self.request(self.controlled(restored))[0])
        self.assertEqual('/backend-api/codex/responses', self.requests[-1][0])

    def test_prepared_plan_pins_mode_tools_retry_and_compact(self):
        plan = self.prepare()
        self.mode('native')
        body = self.controlled(plan)
        for request in [body, dict(body, input=body['input'] + [{'type': 'function_call_output', 'output': 'ok'}])]:
            self.assertEqual(200, self.request(request)[0])
            self.assertEqual('gpt-test-small', self.requests[-1][1]['model'])
        compact = copy.deepcopy(body)
        compact.pop('model')
        self.assertEqual(200, self.request(compact, path='/switch/v1/responses/compact')[0])
        self.assertEqual('gpt-test-small', self.requests[-1][1]['model'])
        body['client_metadata']['turn_id'] = str(uuid.uuid4())
        self.assertEqual(502, self.request(body)[0])

    def test_plan_rejects_wrong_model_session_and_changed_route(self):
        plan = self.prepare()
        body = self.controlled(plan)
        wrong = dict(body, model='gpt-test-large')
        self.assertEqual(502, self.request(wrong)[0])
        self.assertEqual(502, self.request(body, headers={'session_id': 'another-thread'})[0])
        self.native.active = dict(self.native.active, name='changed')
        self.assertEqual(502, self.request(body)[0])
        self.assertEqual([], self.requests)

    def test_preference_change_survives_pool_execution(self):
        self.prepare()
        self.prepare(model='gpt-test-small', explicit=True, preferenceOnly=True)
        self.mode('native')
        self.assertEqual('gpt-test-small', self.prepare(model='gpt-test-small')['model'])

    def test_prepare_requires_native_auth(self):
        with self.assertRaises(CollectorError):
            control.prepare(self.native, {}, {'Authorization': 'Bearer wrong'})

    def test_model_switch_precompaction_uses_previous_native_execution(self):
        self.mode('native')
        old = self.prepare()
        self.assertEqual(200, self.request(self.controlled(old))[0])
        self.mode('qpool')
        plan = self.prepare(currentModel='gpt-test-large')
        body = self.controlled(plan)
        compact = copy.deepcopy(body)
        compact['model'] = 'gpt-test-large'
        self.assertEqual(200, self.request(compact, path='/switch/v1/responses/compact')[0])
        self.assertEqual('/backend-api/codex/responses/compact', self.requests[-1][0])
        self.assertEqual('gpt-test-large', self.requests[-1][1]['model'])
        self.assertEqual(200, self.request(body)[0])
        self.assertEqual('gpt-test-small', self.requests[-1][1]['model'])
        self.assertNotIn(control.METADATA_KEY, self.requests[-1][1]['client_metadata']['x-codex-turn-metadata'])

    def test_effort_only_preference_is_not_lost(self):
        path = self.home / 'models_cache.json'
        models = json.loads(path.read_text())
        for model in models['models']:
            model['supported_reasoning_levels'] = [{'effort': 'high'}, {'effort': 'low'}, {'effort': 'medium'}]
        path.write_text(json.dumps(models))
        self.mode('native')
        self.assertEqual('high', self.prepare()['effort'])
        self.assertEqual('low', self.prepare(effort='low')['effort'])
        self.prepare(model=None, effort='high', effortExplicit=True, preferenceOnly=True)
        self.assertEqual('high', self.prepare()['effort'])

    def test_route_change_during_selection_does_not_stamp_old_binding_with_new_grant(self):
        select = self.native.select
        def changed(*args, **kwargs):
            result = select(*args, **kwargs)
            self.native.grant = 'changed-grant'
            self.native.active = dict(self.native.active, name='changed')
            return result
        with patch.object(self.native, 'select', side_effect=changed):
            with self.assertRaisesRegex(CollectorError, '准备期间路由已变化'):
                self.prepare()
        with self.native.receipts() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM control_plans').fetchone()[0])

    def test_failed_preparation_does_not_change_previous_execution_snapshot(self):
        from qp.native import digest
        self.assertEqual(200, self.request(self.body())[0])  # Old HTTP-only qpool turn.
        sid = digest('native-session')[:16]
        subject = digest('test-subject')
        source = control.previous_execution(self.native, sid, subject, 'gpt-test-small')
        with self.native.receipts() as db:
            saved = db.execute('SELECT binding FROM session_bindings WHERE session=?', (sid,)).fetchone()
            moved = json.loads(saved['binding'])
            moved['channel']['account_id'] = 'migration-target'
            db.execute('UPDATE session_bindings SET binding=? WHERE session=?', (json.dumps(moved), sid))
        again = control.previous_execution(self.native, sid, subject, 'gpt-test-small')
        self.assertEqual(source['binding']['channel']['account_id'], again['binding']['channel']['account_id'])

    def test_preference_update_does_not_wait_for_stream_lock(self):
        from qp.native import digest
        done = threading.Event()
        errors = []
        def preference():
            try:
                self.prepare(explicit=True, preferenceOnly=True)
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()
        with self.native.turn_lock(('desktop-switch', digest('native-session')[:16])):
            worker = threading.Thread(target=preference)
            worker.start()
            self.assertTrue(done.wait(2), 'preference update waited on HTTP stream')
        worker.join()
        self.assertEqual([], errors)

    def test_queue_unknown_claim_is_not_replayed_and_success_is_durable(self):
        headers = {'Authorization': 'Bearer synthetic-access-secret'}
        data = {'threadId': 'native-session', 'queuedSubmissionId': 'queue-1'}
        control.queue_claim(self.native, dict(data, action='claim'), headers)
        with self.assertRaises(CollectorError):
            control.queue_claim(self.native, dict(data, action='claim'), headers)
        result = {'turn': {'id': 'accepted-turn', 'status': 'inProgress', 'items': []}}
        control.queue_claim(self.native, dict(data, action='accepted', result=result), headers)
        self.assertEqual(result, control.queue_claim(self.native, data, headers)['result'])

    def test_install_is_opt_in_and_preserves_config(self):
        result = control.install(self.native, binary='/usr/bin/true')
        from pathlib import Path
        self.assertTrue(Path(result['wrapper']).is_file())
        self.assertTrue(Path(result['launcher']).is_file())
        self.assertEqual(self.config, (self.home / 'config.toml').read_bytes())


class ProxyTests(unittest.TestCase):
    def test_desktop_policy_bootstrap_executes_native_without_proxy(self):
        from qp.control_proxy import main
        args = ['codex-control', '-c', 'features.code_mode_host=true', 'app-server',
                '--print-startup-config', '--startup-client-info', '{"name":"codex_desktop"}']
        with patch('sys.argv', args), patch.dict(os.environ), \
                patch('qp.control_proxy.os.execv', side_effect=SystemExit(0)) as execute, \
                patch('qp.control_proxy.desktop.token') as token, \
                patch('qp.control_proxy.subprocess.Popen') as spawn:
            with self.assertRaises(SystemExit):
                main('/unused/root', '/real/codex')
        execute.assert_called_once_with('/real/codex', ['/real/codex'] + args[1:])
        token.assert_not_called()
        spawn.assert_not_called()

    def test_only_rpc_server_invocations_are_intercepted(self):
        from qp.control_proxy import is_rpc_server
        for command in ('help', 'daemon', 'proxy', 'generate-ts', 'generate-json-schema'):
            with self.subTest(command=command):
                self.assertFalse(is_rpc_server(['app-server', command]))
        for value in ('proxy', 'daemon', 'app-server', '--print-startup-config'):
            with self.subTest(snapshot=value):
                self.assertTrue(is_rpc_server(['-c', 'features.code_mode_host=true',
                    'app-server', '--cloud-skills-snapshot', value, '--listen=stdio://']))
        self.assertTrue(is_rpc_server(['app-server', '--analytics-default-enabled']))
        self.assertFalse(is_rpc_server(['--config', 'app-server', 'login', 'status']))

    def proxy(self, active=False):
        p = Proxy(None, '/tmp/unused')
        def rpc(method, params):
            if method == 'thread/read':
                return {'thread': {'modelProvider': 'openai', 'model': 'sol', 'reasoningEffort': 'high',
                                   'status': {'type': 'active' if active else 'idle'}}}
            return {'data': []}
        p.rpc = rpc
        return p

    def test_plan_mode_and_top_level_model_are_changed_together(self):
        params = {'threadId': 't', 'input': [{'type': 'text', 'text': 'hello'}],
                  'collaborationMode': {'mode': 'plan', 'settings': {'model': 'sol', 'reasoning_effort': 'high',
                                                                  'developer_instructions': 'keep'}},
                  'responsesapiClientMetadataPatch': {'existing': 'keep'}}
        with patch('qp.control_proxy.api', return_value={'token': 'opaque', 'model': 'astra', 'effort': 'medium'}):
            result = self.proxy().prepare_turn(params)
        self.assertEqual('astra', result['model'])
        self.assertEqual('astra', result['collaborationMode']['settings']['model'])
        self.assertEqual('keep', result['collaborationMode']['settings']['developer_instructions'])
        self.assertEqual('keep', result['responsesapiClientMetadataPatch']['existing'])
        self.assertEqual('sol', params['collaborationMode']['settings']['model'])

    def test_active_turn_start_is_steering_without_rerouting(self):
        with patch('qp.control_proxy.api') as api:
            result = self.proxy(active=True).prepare_turn({'threadId': 't', 'model': 'different', 'input': []})
        api.assert_not_called()
        self.assertNotIn('model', result)

    def test_routing_view_keeps_old_chat_distinct_from_fresh_chat(self):
        items = decision_items([{'items': []}], [{'type': 'text', 'text': 'hello'}])
        self.assertEqual('assistant', items[0]['role'])
        self.assertEqual('hello', items[-1]['content'][0]['text'])

    def test_large_paste_is_bounded_only_for_classification(self):
        inputs = [{'type': 'text', 'text': '测试' * 100000}]
        items = decision_items([], inputs)
        self.assertLess(len(json.dumps(items).encode()), 30000)
        self.assertEqual(200000, len(inputs[0]['text']))

    def test_unmaterialized_thread_is_a_valid_first_turn(self):
        proxy = self.proxy()
        original = proxy.rpc
        def rpc(method, params):
            if method == 'thread/turns/list':
                raise RpcError({'code': -32600, 'message': 'thread is not materialized yet'})
            return original(method, params)
        proxy.rpc = rpc
        with patch('qp.control_proxy.api', return_value={'token': 't', 'model': 'sol', 'effort': 'high'}):
            result = proxy.prepare_turn({'threadId': 't', 'input': [{'type': 'text', 'text': 'hello'}]})
        self.assertEqual('sol', result['model'])

    def queued_proxy(self):
        from qp.control_queue import StoredQueue
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store = StoredQueue(temp.name, 'test-subject')
        proxy = self.proxy()
        proxy.output = io.StringIO()
        proxy.queue_store = lambda: store
        item = store.operate('add', {'threadId': 't', 'clientUserMessageId': 'user-id',
                                    'input': [{'type': 'text', 'text': 'task'}]})['queuedSubmission']
        return proxy, store, {'threadId': 't', 'queuedSubmissionId': item['id']}

    def test_queued_start_routes_once_and_preserves_user_message_identity(self):
        proxy, store, params = self.queued_proxy()
        calls = []
        result = {'turn': {'id': 'turn-1'}}
        def rpc(method, data):
            calls.append((method, data))
            if method == 'thread/read':
                return {'thread': {'status': {'type': 'idle'}}}
            if method == 'thread/queue/list':
                return {'data': []}
            return result
        proxy.rpc = rpc
        proxy.prepare_turn = lambda p, **kwargs: dict(p, model='astra')
        self.assertEqual(result, proxy.start_queued(params))
        self.assertEqual(result, proxy.start_queued(params))  # Durable accepted retry.
        starts = [p for m, p in calls if m == 'turn/start']
        self.assertEqual(1, len(starts))
        self.assertEqual('astra', starts[0]['model'])
        self.assertEqual('user-id', starts[0]['clientUserMessageId'])
        self.assertEqual([], store.operate('list', params)['data'])

    def test_accepted_queue_retry_returns_success_even_if_engine_unavailable(self):
        proxy, store, params = self.queued_proxy()
        result = {'turn': {'id': 'already-accepted'}}
        store.operate('claim', params)
        store.operate('accepted', dict(params, result=result))
        proxy.rpc = lambda *args: (_ for _ in ()).throw(RpcError({'code': -32600, 'message': 'not found'}))
        self.assertEqual(result, proxy.start_queued(params))

    def test_queue_unknown_dispatch_is_not_replayed_after_reopen(self):
        from qp.control_proxy import RpcDisconnected
        from qp.control_queue import StoredQueue
        proxy, store, params = self.queued_proxy()
        proxy.idle_for_queue = lambda _: True
        proxy.prepare_turn = lambda p, **kwargs: p
        proxy.rpc = lambda *a: (_ for _ in ()).throw(RpcDisconnected('disconnected'))
        with self.assertRaises(RpcDisconnected):
            proxy.start_queued(params)
        reopened = StoredQueue(store.path.parent, 'test-subject')
        self.assertIsNone(reopened.operate('peek', params))
        with self.assertRaisesRegex(CollectorError, '未确认'):
            reopened.operate('claim', params)
        second = reopened.operate('add', {'threadId': 't', 'clientUserMessageId': 'next',
            'input': [{'type': 'text', 'text': 'next'}]})['queuedSubmission']
        with self.assertRaisesRegex(CollectorError, '越序'):
            reopened.operate('claim', dict(params, queuedSubmissionId=second['id']))

    def test_queue_crud_persists_and_isolates_identity(self):
        from qp.control_queue import StoredQueue
        proxy, store, params = self.queued_proxy()
        second = store.operate('add', {'threadId': 't', 'clientUserMessageId': 'second',
                                      'input': [{'type': 'text', 'text': 'second'}]})['queuedSubmission']
        inputs = [{'type': 'text', 'text': 'edited'}]
        store.operate('update', dict(params, input=inputs))
        ids = [second['id'], params['queuedSubmissionId']]
        store.operate('reorder', {'threadId': 't', 'queuedSubmissionIds': ids})
        reopened = StoredQueue(store.path.parent, 'test-subject')
        first = reopened.operate('list', {'threadId': 't', 'limit': 1})
        self.assertEqual(second['id'], first['data'][0]['id'])
        last = reopened.operate('list', {'threadId': 't', 'cursor': first['nextCursor']})
        self.assertEqual(inputs, last['data'][0]['input'])
        self.assertTrue(reopened.operate('delete', params)['deleted'])
        self.assertFalse(reopened.operate('delete', params)['deleted'])
        with self.assertRaisesRegex(CollectorError, '登录已变化'):
            StoredQueue(store.path.parent, 'other-subject').operate('list', {'threadId': 't'})

    def test_old_native_queue_blocks_managed_dispatch(self):
        proxy, store, params = self.queued_proxy()
        proxy.rpc = lambda method, data: ({'data': [{'id': 'native-old'}]} if method == 'thread/queue/list'
                                         else {'thread': {'status': {'type': 'idle'}}})
        self.assertIsNone(proxy.start_queued(params, automatic=True))
        self.assertIsNotNone(store.operate('peek', params))

    def test_explicit_queue_rejection_requires_manual_retry(self):
        proxy, store, params = self.queued_proxy()
        proxy.idle_for_queue = lambda _: True
        proxy.prepare_turn = lambda p, **kwargs: p
        proxy.rpc = lambda *a: (_ for _ in ()).throw(RpcError({'code': -32600, 'message': 'rejected'}))
        with self.assertRaises(RpcError):
            proxy.start_queued(params)
        self.assertIsNone(store.operate('peek', params))
        self.assertEqual(1, len(store.operate('list', params)['data']))
        store.operate('claim', params)  # Explicit user retry is allowed.

    def test_settings_are_published_before_turn_without_changing_permissions(self):
        proxy = self.proxy()
        original = proxy.rpc
        calls = []
        proxy.rpc = lambda m, p: (calls.append((m, p)), original(m, p))[1]
        with patch('qp.control_proxy.api', return_value={'token': 'plan', 'model': 'astra', 'effort': 'low'}):
            prepared = proxy.prepare_turn({'threadId': 't', 'input': [{'type': 'text', 'text': 'task'}],
                                          'approvalPolicy': 'never', 'sandboxPolicy': {'type': 'readOnly'}})
        update = [p for m, p in calls if m == 'thread/settings/update']
        self.assertEqual([{'threadId': 't', 'model': 'astra', 'effort': 'low'}], update)
        self.assertEqual('never', prepared['approvalPolicy'])
        self.assertEqual({'type': 'readOnly'}, prepared['sandboxPolicy'])

    def test_queue_becoming_active_during_prepare_is_deferred_not_steered(self):
        proxy, store, params = self.queued_proxy()
        reads = []
        calls = []
        def rpc(method, data):
            calls.append(method)
            if method == 'thread/queue/list':
                return {'data': []}
            reads.append(1)
            return {'thread': {'status': {'type': 'idle' if len(reads) == 1 else 'active'}}}
        proxy.rpc = rpc
        self.assertIsNone(proxy.start_queued(params, automatic=True))
        self.assertNotIn('turn/start', calls)
        self.assertIsNotNone(store.operate('peek', params))

    def test_invalid_crud_does_not_change_queue_head(self):
        proxy, store, params = self.queued_proxy()
        before = store.operate('list', params)
        for action in ('delete', 'update'):
            with self.assertRaises(CollectorError):
                store.operate(action, {'threadId': 't', 'input': [{'type': 'text', 'text': 'bad'}]})
        self.assertEqual(before, store.operate('list', params))

    def test_accepted_turn_blocks_queue_before_native_started_event(self):
        proxy = self.proxy()
        proxy.rpc = lambda *a: {'turn': {'id': 'accepted'}}
        proxy.submit_turn({'threadId': 't'})
        self.assertFalse(proxy.idle_for_queue('t'))

    def test_native_settings_replayed_after_desktop_optimistic_turn_event(self):
        from types import SimpleNamespace
        settings = {'method': 'thread/settings/updated', 'params': {'threadId': 't',
                    'threadSettings': {'model': 'astra', 'approvalPolicy': 'never',
                                       'collaborationMode': {'mode': 'plan'}}}}
        started = {'method': 'turn/started', 'params': {'threadId': 't', 'turn': {'id': 'turn-1'}}}
        output = io.StringIO()
        proxy = Proxy(SimpleNamespace(stdout=io.StringIO('\n'.join(json.dumps(x) for x in [settings, started]))),
                      '/tmp/unused', output=output)
        proxy.read()
        self.assertEqual([settings, started, settings], [json.loads(x) for x in output.getvalue().splitlines()])

    def test_automatic_queue_failure_uses_thread_warning_without_fake_turn(self):
        proxy = self.proxy()
        proxy.output = io.StringIO()
        done = threading.Event()
        proxy.start_queued = lambda *a, **k: (_ for _ in ()).throw(CollectorError('unavailable'))
        emit = proxy.emit
        proxy.emit = lambda event: (emit(event), done.set())
        proxy.schedule_queue('t')
        self.assertTrue(done.wait(2))
        value = json.loads(proxy.output.getvalue())
        self.assertEqual('warning', value['method'])
        self.assertEqual({'threadId', 'message'}, set(value['params']))

    def test_child_exit_closes_proxy_without_waiting_for_desktop_input(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass'], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True)
        readfd, writefd = os.pipe()
        source = os.fdopen(readfd)
        proxy = Proxy(child, '/tmp/unused', output=io.StringIO())
        worker = threading.Thread(target=proxy.run, args=(source,), daemon=True)
        try:
            worker.start()
            worker.join(3)
            self.assertFalse(worker.is_alive(), 'proxy did not propagate upstream exit')
        finally:
            os.close(writefd)
            worker.join(3)
            source.close()
            child.stdin.close()
            child.stdout.close()
