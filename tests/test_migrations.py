"""Explicit migration preserves authentication and history; no implicit retries."""
import json
import unittest
from unittest.mock import patch

import test_api_accounts as base
from qp import desktop, profiles
from qp.native import digest
from qp.collectors.base import CollectorError


class MigrationTests(unittest.TestCase):
    setUp = base.APIAccountTests.setUp
    tearDown = base.APIAccountTests.tearDown
    body = base.APIAccountTests.body
    request = base.APIAccountTests.request
    connect_api = base.APIAccountTests.connect_api
    subscription_priority_fixture = base.APIAccountTests.subscription_priority_fixture

    def fixture(self):
        plus, paid = self.subscription_priority_fixture()
        desktop.connect(self.native)
        body = self.body()
        body['input'].insert(0, {'type':'reasoning','encrypted_content':'opaque-pro-history'})
        self.assertEqual(200, self.request(body, token='synthetic-access-secret', path='/legacy/v1/responses')[0])
        return plus, paid, body

    def schedule(self, plus):
        return self.native.schedule_migration({'session':digest('native-session')[:16],
            'target_account_id':plus,'confirm':True})

    def state(self):
        with self.native.receipts() as c:
            return dict(c.execute('SELECT * FROM legacy_bindings').fetchone())

    def test_new_user_boundary_preserves_history_and_source_identity(self):
        plus, _, body = self.fixture()
        self.assertEqual('pending', self.schedule(plus)['state'])
        body['input'].append({'type':'function_call_output','call_id':'c1','output':'original-result'})
        self.assertEqual(200,self.request(body,token='synthetic-access-secret',path='/legacy/v1/responses')[0])
        self.assertEqual(self.aid,json.loads(self.state()['binding'])['channel']['account_id'])
        body['input'].append({'role':'user','content':'继续工作'})
        self.assertEqual(200,self.request(body,token='synthetic-access-secret',path='/legacy/v1/responses')[0])
        self.assertEqual(body['input'],self.requests[-1][1]['input'])
        saved=self.state()
        self.assertEqual(self.aid,saved['account'])
        self.assertEqual(plus,json.loads(saved['binding'])['channel']['account_id'])
        self.assertEqual('gpt-test-small',self.requests[-1][1]['model'])
        self.native.bindings.clear()
        body['input'].append({'type':'function_call_output','call_id':'c2','output':'next'})
        self.assertEqual(200,self.request(body,token='synthetic-access-secret',path='/legacy/v1/responses')[0])
        self.assertEqual(plus,json.loads(self.state()['binding'])['channel']['account_id'])
        with self.assertRaises(CollectorError):
            self.native.select(body,{'session_id':'native-session'},legacy_account=plus)

    def test_fresh_builtin_alias_uses_plus_and_persists_separate_auth_identity(self):
        plus, _ = self.subscription_priority_fixture()
        desktop.connect(self.native)
        body = self.body('翻译 hello')
        body['input'].insert(0, {'type': 'message', 'role': 'user', 'content': [
            {'type': 'input_text', 'text': '<environment_context>\n<cwd>/isolated</cwd>\n</environment_context>'}]})
        body['input'].insert(0, {'role': 'developer', 'type': 'additional_tools', 'tools': []})
        self.assertEqual(200, self.request(body, token='synthetic-access-secret', path='/legacy/v1/responses')[0])
        saved = self.state()
        self.assertEqual(self.aid, saved['account'])
        self.assertEqual(plus, json.loads(saved['binding'])['channel']['account_id'])
        self.assertIs(json.loads(saved['binding'])['automatic_account'], True)
        for item in ({'type': 'function_call_output', 'call_id': 't1', 'output': 'ok'},
                     {'role': 'user', 'content': '实现并发算法'},
                     {'role': 'user', 'content': '翻译 next'}):
            self.native.bindings.clear()
            body['input'].append(item)
            self.assertEqual(200, self.request(body, token='synthetic-access-secret', path='/legacy/v1/responses')[0])
            self.assertEqual(plus, json.loads(self.state()['binding'])['channel']['account_id'])
            self.assertIs(json.loads(self.state()['binding'])['automatic_account'], True)
        self.assertIn('自动路由', self.native.status()['sessions'][0]['account_policy'])
        with self.assertRaises(CollectorError):
            self.native.select(body, {'session_id': 'native-session'}, legacy_account=plus)

    def test_first_adapter_failure_without_receipt_is_still_restored(self):
        plus, _ = self.subscription_priority_fixture()
        desktop.connect(self.native)
        with patch.object(self.adapter_mock.return_value, 'ensure', side_effect=CollectorError('cold adapter failed')):
            self.assertEqual(502, self.request(self.body(), token='synthetic-access-secret', path='/legacy/v1/responses')[0])
        self.assertEqual(plus, json.loads(self.state()['binding'])['channel']['account_id'])
        with self.native.receipts() as db:
            self.assertEqual(0, db.execute('SELECT count(*) FROM requests').fetchone()[0])
        with patch('qp.codex_recovery.recover', return_value=[]) as recover:
            desktop.restore(self.native)
        self.assertEqual({digest('native-session')[:16]: 'gpt-test-small'},
                         recover.call_args.kwargs['managed_sessions'])
        self.assertNotIn('openai_base_url', (self.home / 'config.toml').read_text())

    def test_no_candidate_failure_checks_unknown_alias_before_removing_entry(self):
        desktop.connect(self.native)
        with patch('qp.native.pools.evaluate', return_value={'state': 'blocked'}):
            self.assertEqual(502, self.request(self.body(), token='synthetic-access-secret', path='/legacy/v1/responses')[0])
        with self.native.receipts() as db:
            self.assertEqual(0, db.execute('SELECT count(*) FROM requests').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT count(*) FROM legacy_bindings').fetchone()[0])
        before = (self.home / 'config.toml').read_text()
        with patch('qp.codex_recovery.recover', side_effect=CollectorError('unknown alias; 保留接入配置')) as recover:
            with self.assertRaises(CollectorError):
                desktop.restore(self.native)
        self.assertEqual({}, recover.call_args.kwargs['managed_sessions'])
        self.assertTrue(recover.call_args.kwargs['require_owned_aliases'])
        self.assertEqual(before, (self.home / 'config.toml').read_text())
        self.assertTrue((self.native.root / 'desktop.json').exists())

    def test_automatic_plus_can_migrate_back_to_login_pro_when_plus_exhausted(self):
        plus, _ = self.subscription_priority_fixture()
        desktop.connect(self.native)
        body = self.body()
        self.assertEqual(200, self.request(body, token='synthetic-access-secret', path='/legacy/v1/responses')[0])
        self.assertEqual(plus, json.loads(self.state()['binding'])['channel']['account_id'])
        def quota(db, pool, model):
            return {'state': 'blocked' if pool['account_id'] == plus else 'ready'}
        with patch('qp.native.pools.evaluate', side_effect=quota):
            result = self.schedule(self.aid)
            self.assertEqual(self.aid, result['auth_account'])
            self.assertEqual(plus, result['execution_source'])
            self.assertEqual(self.aid, result['target_account'])
            body['input'].append({'type': 'function_call_output', 'call_id': 'old-tool', 'output': 'keep history'})
            self.native.bindings.clear()
            binding, _ = self.native.select(body, {'session_id': 'native-session'}, legacy_account=self.aid)
            self.assertEqual(plus, binding['channel']['account_id'])
            with self.assertRaises(CollectorError):
                self.native.select(body, {'session_id': 'native-session'}, legacy_account=plus)
            body['input'].append({'role': 'user', 'content': 'continue on Pro'})
            self.assertEqual(200, self.request(body, token='synthetic-access-secret', path='/legacy/v1/responses')[0])
        saved = self.state()
        self.assertEqual(self.aid, saved['account'])
        self.assertEqual(self.aid, json.loads(saved['binding'])['channel']['account_id'])
        self.assertEqual('gpt-test-small', self.requests[-1][1]['model'])
        self.assertEqual(body['input'], self.requests[-1][1]['input'])
        self.native.bindings.clear()
        body['input'].append({'type': 'function_call_output', 'call_id': 'new-tool', 'output': 'ok'})
        self.assertEqual(200, self.request(body, token='synthetic-access-secret', path='/legacy/v1/responses')[0])
        self.assertEqual(self.aid, json.loads(self.state()['binding'])['channel']['account_id'])

    def test_completed_migration_can_be_followed_by_another_explicit_move(self):
        plus, _, body = self.fixture()
        self.schedule(plus)
        body['input'].append({'role': 'user', 'content': 'move to Plus'})
        self.assertEqual(200, self.request(body, token='synthetic-access-secret', path='/legacy/v1/responses')[0])
        self.native.bindings.clear()
        self.schedule(self.aid)
        with self.assertRaises(CollectorError):
            self.schedule(self.aid)
        body['input'].append({'type': 'function_call_output', 'call_id': 'plus-tool', 'output': 'ok'})
        self.assertEqual(200, self.request(body, token='synthetic-access-secret', path='/legacy/v1/responses')[0])
        self.assertEqual(plus, json.loads(self.state()['binding'])['channel']['account_id'])
        body['input'].append({'role': 'user', 'content': 'move back to Pro'})
        self.assertEqual(200, self.request(body, token='synthetic-access-secret', path='/legacy/v1/responses')[0])
        self.assertEqual(self.aid, json.loads(self.state()['binding'])['channel']['account_id'])

    def test_automatic_execution_source_identity_is_checked_before_scheduling(self):
        from pathlib import Path
        plus, _ = self.subscription_priority_fixture()
        desktop.connect(self.native)
        self.assertEqual(200, self.request(self.body(), token='synthetic-access-secret', path='/legacy/v1/responses')[0])
        before = self.state()['binding']
        path = Path(profiles.load()['accounts'][plus]['source']['credentials_file'])
        path.write_text(json.dumps({'tokens': {'account_id': 'replaced-subject', 'access_token': 'synthetic'}}))
        with self.assertRaises(CollectorError):
            self.schedule(self.aid)
        self.assertEqual(before, self.state()['binding'])
        with self.native.receipts() as db:
            self.assertEqual(0, db.execute('SELECT count(*) FROM legacy_migrations').fetchone()[0])

    def test_unbound_builtin_alias_never_moves_existing_history(self):
        self.subscription_priority_fixture()
        for extra in ({'role': 'assistant', 'content': 'old reply'},
                      {'type': 'reasoning', 'encrypted_content': 'opaque'},
                      {'type': 'function_call_output', 'call_id': 't1', 'output': 'old'},
                      {'type': 'reasoning', 'role': 'user', 'content': 'disguised'},
                      {'type': 'additional_tools', 'role': 'user', 'content': 'disguised'},
                      {'role': 'user', 'content': 'another old message'}):
            body = self.body()
            body['input'].insert(0, extra)
            binding, _ = self.native.select(body, {'session_id': 'history-' + str(extra)}, legacy_account=self.aid)
            self.assertEqual(self.aid, binding['channel']['account_id'])
            self.assertNotIn('automatic_account', binding)
        body = self.body()
        with self.assertRaises(CollectorError):
            self.native.select(body, {'session_id': 'first-compact'}, compact=True, legacy_account=self.aid)

    def test_compaction_and_same_turn_cannot_trigger_migration(self):
        plus, _, body=self.fixture(); self.schedule(plus)
        for compact in (False,True):
            binding,_=self.native.select(body,{'session_id':'native-session'},compact=compact,legacy_account=self.aid)
            self.assertEqual(self.aid,binding['channel']['account_id'])
        body['input'].append({'role':'user','content':'next'})
        binding,_=self.native.select(body,{'session_id':'native-session'},compact=True,legacy_account=self.aid)
        self.assertEqual(self.aid,binding['channel']['account_id'])

    def test_reject_api_target_no_consent_and_duplicate_plan(self):
        plus, paid, _=self.fixture()
        for data in ({'session':digest('native-session')[:16],'target_account_id':plus},
                     {'session':digest('native-session')[:16],'target_account_id':paid,'confirm':True}):
            with self.assertRaises(CollectorError):self.native.schedule_migration(data)
        self.schedule(plus)
        with self.assertRaises(CollectorError):self.schedule(plus)

    def test_target_preflight_failure_preserves_original_binding(self):
        plus,_,body=self.fixture();self.schedule(plus)
        before=self.state()['binding']
        body['input'].append({'role':'user','content':'next'})
        with patch.object(self.native,'validate_binding',side_effect=CollectorError('not ready')):
            status,_=self.request(body,token='synthetic-access-secret',path='/legacy/v1/responses')
        self.assertEqual(502,status);self.assertEqual(before,self.state()['binding'])
        self.assertEqual(1,len(self.requests))

    def test_upstream_failure_never_replays_on_pro(self):
        plus,_,body=self.fixture();self.schedule(plus)
        body['input'].append({'role':'user','content':'next'})
        self.failure=True
        self.assertEqual(429,self.request(body,token='synthetic-access-secret',path='/legacy/v1/responses')[0])
        self.assertEqual(2,len(self.requests))
        self.assertEqual(plus,json.loads(self.state()['binding'])['channel']['account_id'])

    def test_migration_waits_for_full_request_guard(self):
        import threading
        plus,_,_=self.fixture()
        started=threading.Event();done=threading.Event();errors=[]
        def schedule():
            started.set()
            try:self.schedule(plus)
            except Exception as exc:errors.append(exc)
            finally:done.set()
        with self.native.turn_lock(('request',digest('native-session')[:16])):
            worker=threading.Thread(target=schedule);worker.start();started.wait(1)
            self.assertFalse(done.wait(.1))
        worker.join(3)
        self.assertTrue(done.is_set());self.assertEqual([],errors)

    def test_changed_route_cannot_apply_migration(self):
        plus,_,body=self.fixture();self.schedule(plus)
        self.native.active=dict(self.native.active,name='different')
        body['input'].append({'role':'user','content':'next'})
        with self.assertRaisesRegex(CollectorError,'路由已变化'):
            self.native.select(body,{'session_id':'native-session'},legacy_account=self.aid)

    def test_compaction_rebases_pending_boundary_without_switching_mid_turn(self):
        plus,_,body=self.fixture()
        body['input'] += [{'role':'user','content':'second'},{'role':'user','content':'third'}]
        self.native.select(body,{'session_id':'native-session'},legacy_account=self.aid)
        self.schedule(plus)
        compacted=self.body('compacted visible user')
        binding,_=self.native.select(compacted,{'session_id':'native-session'},compact=True,legacy_account=self.aid)
        self.assertEqual(self.aid,binding['channel']['account_id'])
        with self.native.receipts() as c:
            self.assertEqual(1,c.execute('SELECT user_count FROM legacy_migrations').fetchone()[0])
        compacted['input'].append({'role':'user','content':'new real user turn'})
        binding,_=self.native.select(compacted,{'session_id':'native-session'},legacy_account=self.aid)
        self.assertEqual(plus,binding['channel']['account_id'])

    def test_exhausted_source_does_not_block_explicit_migration(self):
        plus,_,body=self.fixture()
        def verdict(db,pool,model):return {'state':'blocked' if pool['account_id']==self.aid else 'ready'}
        with patch('qp.native.pools.evaluate',side_effect=verdict):
            self.schedule(plus)
            body['input'].append({'role':'user','content':'continue on available subscription'})
            binding,_=self.native.select(body,{'session_id':'native-session'},legacy_account=self.aid)
        self.assertEqual(plus,binding['channel']['account_id'])

    def test_protocol_turn_migrates_after_compacted_history_on_next_user_turn(self):
        import uuid
        plus, _, body = self.fixture()
        turn = str(uuid.uuid4())
        body['input'] += [{'role': 'user', 'content': 'second'}, {'role': 'user', 'content': 'third'}]
        self.native.select(body, {'session_id': 'native-session'}, legacy_account=self.aid, protocol_turn=turn)
        self.schedule(plus)
        self.native.select(body, {'session_id': 'native-session'}, compact=True,
                           legacy_account=self.aid, protocol_turn=turn)
        reconstructed = self.body('compacted visible user')
        self.native.bindings.clear()
        binding, _ = self.native.select(reconstructed, {'session_id': 'native-session'},
                                       legacy_account=self.aid, protocol_turn=turn)
        self.assertEqual(self.aid, binding['channel']['account_id'])
        with self.native.receipts() as db:
            self.assertEqual('pending', db.execute('SELECT state FROM legacy_migrations').fetchone()[0])
        reconstructed['input'].append({'role': 'user', 'content': 'new actual user turn'})
        binding, _ = self.native.select(reconstructed, {'session_id': 'native-session'},
                                       legacy_account=self.aid, protocol_turn=str(uuid.uuid4()))
        self.assertEqual(plus, binding['channel']['account_id'])

    def test_changed_target_identity_is_rejected_before_binding_switch(self):
        from pathlib import Path
        plus,_,body=self.fixture();self.schedule(plus)
        before=self.state()['binding']
        account=profiles.load()['accounts'][plus]
        path=Path(account['source']['credentials_file'])
        path.write_text(json.dumps({'tokens':{'account_id':'wrong-identity','access_token':'synthetic-changed'}}))
        body['input'].append({'role':'user','content':'new turn'})
        with self.assertRaises(CollectorError):
            self.native.select(body,{'session_id':'native-session'},legacy_account=self.aid)
        self.assertEqual(before,self.state()['binding'])

    def test_migration_preserves_old_model_outside_new_groups_for_first_turn(self):
        plus,_,body=self.fixture()
        # Simulate a new plan that no longer routes new turns to the old model.
        route=dict(self.native.active,mode='fixed',regular=[],
                   primary=[plus+'-gpt-test-large',self.large])
        self.native.save_route(dict(route,apply_to_active=True))
        self.schedule(plus)
        body['input'].append({'role':'user','content':'first migrated user turn'})
        binding,_=self.native.select(body,{'session_id':'native-session'},legacy_account=self.aid)
        self.assertEqual(plus,binding['channel']['account_id'])
        self.assertEqual('gpt-test-small',binding['channel']['model'])
        body['input'].append({'role':'user','content':'next user follows new rule'})
        binding,_=self.native.select(body,{'session_id':'native-session'},legacy_account=self.aid)
        self.assertEqual('gpt-test-large',binding['channel']['model'])
