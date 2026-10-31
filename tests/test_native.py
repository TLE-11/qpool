"""Native account/route/Responses integration with isolated synthetic credentials."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from qp import profiles, web
from qp.collectors import codex
from qp.collectors.base import CollectorError, QuotaReading


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / 'codex'
        self.home.mkdir()
        (self.home / 'auth.json').write_text(json.dumps({'tokens': {'account_id': 'test-subject',
            'access_token': 'synthetic-access-secret', 'refresh_token': 'never-copy-refresh-secret'}}))
        models = [{'slug': m, 'context_window': 200000, 'input_modalities': ['text'],
                  'supported_reasoning_levels': [{'effort': 'medium'}]} for m in ('gpt-test-small', 'gpt-test-large')]
        (self.home / 'models_cache.json').write_text(json.dumps({'models': models}))
        self.env = patch.dict(os.environ, {'CODEX_HOME': str(self.home),
            'QPOOL_CONFIG': str(self.root / 'pools.json'), 'QPOOL_DB': str(self.root / 'ledger.db')})
        self.env.start()
        patch('qp.desktop.install_startup').start()
        patch('qp.desktop.remove_startup').start()
        self.requests = []
        self.failure = False
        self.bad_usage = False
        outer = self
        class Upstream(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.requests.append((self.path, body, dict(self.headers)))
                if outer.failure:
                    self.send_response(429);self.end_headers();self.wfile.write(b'secret upstream detail');return
                self.send_response(200);self.send_header('Content-Type', 'text/event-stream');self.end_headers()
                events = [{'type':'response.output_text.delta','delta':'ok'},
                          {'type':'response.completed','response':{'model':body['model'],'status':'completed',
                           'usage':'invalid' if outer.bad_usage else {'input_tokens':19,'output_tokens':2}}}]
                for event in events:
                    self.wfile.write(('data: '+json.dumps(event)+'\n\n').encode())
            def log_message(self, *args): pass
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.upthread = threading.Thread(target=self.upstream.serve_forever, daemon=True);self.upthread.start()
        self.console = web.Console(self.root / 'ledger.db')
        self.native = self.console.native
        class FakeAdapter:
            models = ['gpt-test-small', 'gpt-test-large']
            port = self.upstream.server_port
            key = 'private-adapter-key'
            lock = threading.RLock()
            def ensure(self, deadline=None): return self
        self.adapter_mock = patch.object(self.native, 'adapter', return_value=FakeAdapter()).start()
        self.readings = patch('qp.collectors.codex.fetch_readings', return_value=[QuotaReading(
             agent='codex', window_key='seven_day', label='weekly', used_percent=10, window_seconds=604800)]).start()
        source = self.native.discover()['sources'][0]
        self.connected = self.native.connect({'source_id':source['id']})
        self.aid = self.connected['connected']
        self.small, self.large = [self.aid+'-'+m for m in FakeAdapter.models]
        self.route = self.native.save_route({'name':'test','mode':'local','regular':[self.small],'primary':[self.large]})
        self.server = web.Server(('127.0.0.1', 0), self.console)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True);self.thread.start()
        self.native.enable(self.route['id'])

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
        self.upstream.shutdown();self.upstream.server_close();self.upthread.join()
        self.native.close();self.console.manager.close()
        patch.stopall();self.tmp.cleanup()

    def request(self, body, token=None, path='/v1/responses', headers=None):
        connection = http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        h = {'Content-Type':'application/json','Authorization':'Bearer '+(token or self.native.grant),
             'session_id':'native-session','X-Should-Not-Forward':'private-client-data'}
        h.update(headers or {})
        connection.request('POST',path,json.dumps(body),h)
        result=connection.getresponse();status=result.status;raw=result.read();connection.close()
        return status,raw

    def body(self, text='翻译 hello'):
        return {'model':'qpool-auto','stream':True,'input':[{'role':'user','content':[{'type':'input_text','text':text}]}]}

    def test_legacy_native_auth_preserves_history_and_survives_restart(self):
        (self.native.root/'desktop.json').write_text(json.dumps({'legacy_enabled':True}))
        body=self.body()
        body['input'].insert(0,{'type':'reasoning','encrypted_content':'synthetic-old-reasoning'})
        status,_=self.request(body,token='synthetic-access-secret',path='/legacy/v1/responses')
        self.assertEqual(200,status)
        self.assertEqual(body['input'],self.requests[-1][1]['input'])
        self.native.bindings.clear()
        body['input'].append({'type':'function_call_output','call_id':'c1','output':'ok'})
        status,_=self.request(body,token='synthetic-access-secret',path='/legacy/v1/responses')
        self.assertEqual(200,status)
        self.assertEqual('gpt-test-small',self.requests[-1][1]['model'])
        with self.assertRaises(CollectorError):
            self.native.select(body,{'session_id':'native-session'},legacy_account='different-account')
        self.native.disable()
        status,_=self.request(body,token='synthetic-access-secret',path='/legacy/v1/responses')
        self.assertEqual(401,status)

    def test_legacy_explicit_model_is_preserved_and_oauth_required(self):
        (self.native.root/'desktop.json').write_text(json.dumps({'legacy_enabled':True}))
        body=self.body();body['model']='gpt-test-large'
        status,_=self.request(body,path='/legacy/v1/responses')
        self.assertEqual(401,status)
        status,_=self.request(body,token='synthetic-access-secret',path='/legacy/v1/responses')
        self.assertEqual(200,status)
        self.assertEqual('gpt-test-large',self.requests[-1][1]['model'])
        body['model']='missing-model'
        status,_=self.request(body,token='synthetic-access-secret',path='/legacy/v1/responses')
        self.assertEqual(502,status)
        self.assertEqual(1,len(self.requests))

    def test_first_native_browser_history_keeps_explicit_model_and_account(self):
        (self.native.root/'desktop.json').write_text(json.dumps({'legacy_enabled':True}))
        body=self.body();body['model']='gpt-test-large'
        body['input'].extend([{'type':'function_call_output','call_id':'browser1','output':'old screenshot'},
                              {'role':'developer','content':'updated browser tools'},
                              {'type':'function_call_output','call_id':'browser2','output':'old browser state'}])
        status,_=self.request(body,token='synthetic-access-secret',path='/legacy/v1/responses')
        self.assertEqual(200,status)
        self.assertEqual('gpt-test-large',self.requests[-1][1]['model'])
        self.assertEqual(body['input'],self.requests[-1][1]['input'])
        body['model']='qpool-auto'
        body['input'].append({'role':'developer','content':'trailing runtime tool metadata'})
        status,_=self.request(body,token='synthetic-access-secret',path='/legacy/v1/responses',headers={'session_id':'unbound-auto'})
        self.assertEqual(502,status)
        status,_=self.request(body,headers={'session_id':'unbound-direct'})
        self.assertEqual(502,status)
        self.assertEqual(1,len(self.requests))

    def test_native_explicit_model_change_requires_new_user_turn(self):
        (self.native.root/'desktop.json').write_text(json.dumps({'legacy_enabled':True}))
        body=self.body();body['model']='gpt-test-small'
        self.assertEqual(200,self.request(body,token='synthetic-access-secret',path='/legacy/v1/responses')[0])
        body['model']='gpt-test-large'
        body['input'].append({'type':'function_call_output','call_id':'same-turn','output':'old result'})
        self.assertEqual(502,self.request(body,token='synthetic-access-secret',path='/legacy/v1/responses')[0])
        body['input'].insert(1,{'role':'user','content':'new turn with explicitly changed model'})
        self.assertEqual(200,self.request(body,token='synthetic-access-secret',path='/legacy/v1/responses')[0])
        self.assertEqual('gpt-test-large',self.requests[-1][1]['model'])

    def test_native_turn_marker_advances_without_allowing_same_turn_change(self):
        h={'session_id':'native-turn-marker'}
        body=self.body();body['model']='gpt-test-small'
        self.native.select(body,h,legacy_account=self.aid)
        body['input'].extend([{'role':'user','content':'U2'}, {'type':'function_call_output','call_id':'c2','output':'done'}])
        self.native.select(body,h,legacy_account=self.aid)
        body['model']='gpt-test-large'
        with self.assertRaises(CollectorError):self.native.select(body,h,legacy_account=self.aid)
        body['input'].insert(-1,{'role':'user','content':'U3'})
        with self.assertRaises(CollectorError):self.native.select(body,h,compact=True,legacy_account=self.aid)

    def test_old_native_binding_can_recover_only_explicit_model(self):
        h={'session_id':'legacy-old-format'}
        body=self.body();body['model']='gpt-test-small'
        binding,sid=self.native.select(body,h,legacy_account=self.aid)
        old=dict(binding);old.pop('user_count')
        with self.native.receipts() as db:
            db.execute('UPDATE legacy_bindings SET binding=? WHERE session=?',(json.dumps(old),sid))
        self.native.bindings.clear()
        body['model']='gpt-test-large'
        body['input'].append({'type':'function_call_output','call_id':'old-tool','output':'history'})
        with self.assertRaises(CollectorError):self.native.select(body,h,legacy_account=self.aid)
        body['input'].insert(-1,{'role':'user','content':'new request after reconnect'})
        restored,_=self.native.select(body,h,legacy_account=self.aid)
        self.assertEqual(self.aid,restored['channel']['account_id'])
        self.assertEqual('gpt-test-large',restored['channel']['model'])

    def test_compressed_builtin_request_and_wrong_workspace(self):
        import zstandard
        (self.native.root/'desktop.json').write_text(json.dumps({'legacy_enabled':True}))
        # Codex sends frames without content size; tiny known-size fixtures miss
        # the decoder window limit applied to the real streaming encoder.
        raw=zstandard.ZstdCompressor(write_content_size=False).compress(json.dumps(self.body('翻译 '+ 'x'*50000)).encode())
        for workspace,expected in [('wrong-workspace',401),('test-subject',200)]:
            c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
            c.request('POST','/legacy/v1/responses',raw,headers={'Content-Type':'application/json',
                'Content-Encoding':'zstd','Authorization':'Bearer synthetic-access-secret',
                'ChatGPT-Account-Id':workspace,'session_id':'compressed-session'})
            r=c.getresponse();self.assertEqual(expected,r.status);r.read();c.close()
        self.assertEqual(1,len(self.requests))

    def test_failed_legacy_selection_does_not_pin_route(self):
        body=self.body();body['model']='unavailable-model'
        with self.assertRaises(CollectorError):
            self.native.select(body,{'session_id':'failed-session'},legacy_account=self.aid)
        with self.native.receipts() as c:
            self.assertEqual(0,c.execute('SELECT count(*) FROM legacy_bindings').fetchone()[0])

    def test_account_onboarding_persists_reference_and_real_window(self):
        config=profiles.load();account=config['accounts'][self.aid]
        self.assertEqual('test-subject',profiles.credentials(account,codex)['account_id'])
        self.assertEqual(['seven_day'],[w['key'] for w in next(iter(config['pools'].values()))['windows']])
        raw=(self.root/'pools.json').read_text()
        self.assertNotIn('synthetic-access-secret',raw);self.assertNotIn('never-copy-refresh-secret',raw)
        self.assertEqual(0o600,(self.root/'pools.json').stat().st_mode & 0o777)
        self.assertEqual(2,len(self.connected['models']))
        self.assertTrue(self.native.discover()['sources'][0]['connected'])

    def test_catalog_preserves_original_models_outside_route_and_hidden_metadata(self):
        path = self.home / 'models_cache.json'
        data = json.loads(path.read_text())
        extra = dict(data['models'][0], slug='original-model-not-in-route', visibility='list',
                     display_name='Original model', context_window=400000)
        hidden = dict(extra, slug='internal-hidden-model', visibility='hide')
        data['models'].extend([extra, hidden])
        path.write_text(json.dumps(data))
        self.native.catalog(self.route)
        catalog = json.loads((self.native.root / 'catalog.json').read_text())['models']
        by_id = {m['slug']: m for m in catalog}
        self.assertEqual(extra, by_id[extra['slug']])
        self.assertEqual(hidden, by_id[hidden['slug']])
        self.assertEqual(200000, by_id['qpool-auto']['context_window'])
        self.assertEqual('qpool auto', by_id['qpool-auto']['display_name'])
        self.assertIn('Existing OpenAI chats', by_id['qpool-auto']['description'])
        # A subsequent enable must not shrink the original catalog again.
        self.native.enable(self.route['id'])
        self.assertEqual(catalog, json.loads((self.native.root / 'catalog.json').read_text())['models'])

    def test_gateway_rejects_original_model_selection_before_routing_or_spending(self):
        body = self.body()
        body['model'] = 'original-model-not-in-route'
        with patch.object(self.native, 'select') as select:
            status, raw = self.request(body)
            self.assertEqual(502, status)
            self.assertIn('请选择 qpool auto', raw.decode())
            select.assert_not_called()
        self.assertEqual([], self.requests)

    def test_desktop_chatgpt_auth_uses_local_header_and_never_forwards_oauth(self):
        key='synthetic-local-desktop-key-0123456789abcdef'
        (self.native.root/'desktop-key').write_text(key)
        status,_=self.request(self.body(),token='native-oauth',headers={
            'X-Qpool-Desktop-Token':key,'ChatGPT-Account-Id':'native-account'})
        self.assertEqual(200,status)
        headers={k.lower():v for k,v in self.requests[-1][2].items()}
        self.assertEqual('Bearer private-adapter-key',headers['authorization'])
        self.assertNotIn('x-qpool-desktop-token',headers)
        self.assertNotIn('chatgpt-account-id',headers)
        status,_=self.request(self.body(),token='native-oauth',headers={'X-Qpool-Desktop-Token':'wrong'})
        self.assertEqual(401,status)
        self.native.disable()
        status,_=self.request(self.body(),token='native-oauth',headers={'X-Qpool-Desktop-Token':key})
        self.assertEqual(401,status)

    def test_desktop_route_rotation_while_waiting_rejects_before_upstream(self):
        key='synthetic-local-desktop-key-0123456789abcdef'
        (self.native.root/'desktop-key').write_text(key)
        adapter=self.adapter_mock.return_value
        def rotate():
            self.native.grant='different-grant'
        adapter.ensure=rotate
        status,_=self.request(self.body(),token='native-oauth',headers={'X-Qpool-Desktop-Token':key})
        self.assertEqual(502,status)
        self.assertEqual([],self.requests)

    def test_sync_refreshes_catalog_without_revoking_live_entry(self):
        path = self.home / 'models_cache.json'
        data = json.loads(path.read_text())
        extra = dict(data['models'][0], slug='new-original-model', visibility='list')
        data['models'].append(extra)
        path.write_text(json.dumps(data))
        adapter = self.adapter_mock.return_value
        grant = self.native.grant
        with patch.object(adapter, 'refresh_models', return_value=adapter.models + [extra['slug']], create=True):
            result = self.native.sync_models(self.aid)
        self.assertFalse(result['errors'])
        self.assertEqual(grant, self.native.grant)
        self.assertIn(extra, json.loads((self.native.root / 'catalog.json').read_text())['models'])

    def test_desktop_grant_survives_service_restart_but_explicit_disable_revokes(self):
        from qp import desktop
        desktop.connect(self.native)
        self.assertEqual(200, self.request(self.body())[0])
        self.native.save_route(dict(self.route))
        self.native.enable(self.route['id'])
        grant = self.native.grant
        port = self.server.server_port
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.native.close(); self.console.manager.close()
        self.console = web.Console(self.root / 'ledger.db')
        self.native = self.console.native
        self.server = web.Server(('127.0.0.1', port), self.console)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.assertEqual(grant, self.native.grant)
        self.assertEqual(self.route, self.native.active)
        self.assertTrue(desktop.status(self.native)['connected'])
        # Recover the same account and allow a new turn after process restart.
        old_adapter=self.adapter_mock.return_value
        patch.object(self.native, 'adapter', return_value=old_adapter).start()
        body = self.body('现在做下一件事')
        status, raw = self.request(body)
        self.assertEqual(200, status)
        self.assertEqual(2, len(self.requests))
        self.assertEqual(self.aid, next(iter(self.native.bindings.values()))['channel']['account_id'])
        self.native.disable()
        self.assertFalse((self.native.root / 'desktop-route.json').exists())
        desktop.resume(self.native)
        self.assertIsNone(self.native.grant)

    def test_approval_hidden_model_uses_original_account_without_decider(self):
        from qp import profiles
        (self.native.root/'desktop.json').write_text(json.dumps({'legacy_enabled':True}))
        raw=json.loads(profiles.config_path().read_text())
        account=next(a for a in raw['accounts'] if a['id']==self.aid)
        account['model_inventory']['upstream'].append('codex-auto-review')
        from qp.native import atomic
        atomic(profiles.config_path(),raw)
        adapter=self.adapter_mock.return_value
        adapter.models=adapter.models+['codex-auto-review']
        body=self.body();body['model']='codex-auto-review'
        with patch.object(self.native,'select') as select:
            status,_=self.request(body,token='synthetic-access-secret',path='/legacy/v1/responses')
            self.assertEqual(200,status)
            select.assert_not_called()
        self.assertEqual('codex-auto-review',self.requests[-1][1]['model'])
        self.assertEqual(body['input'],self.requests[-1][1]['input'])
        self.assertEqual(200,self.request(self.body())[0])

    def test_restored_binding_rejects_changed_route(self):
        self.assertEqual(200,self.request(self.body())[0])
        self.native.bindings.clear()
        self.native.active=dict(self.native.active,name='changed')
        status,_=self.request(self.body())
        self.assertEqual(502,status)
        self.assertEqual(1,len(self.requests))

    def test_unchanged_save_and_enable_preserve_grant_and_tool_binding(self):
        self.assertEqual(200, self.request(self.body())[0])
        grant = self.native.grant
        self.native.save_route(dict(self.route))
        self.native.enable(self.route['id'])
        self.assertEqual(grant, self.native.grant)
        self.native.bindings.clear()  # Force durable readback, not the cache.
        body = self.body()
        body['input'].append({'type':'function_call_output','call_id':'c1','output':'ok'})
        self.assertEqual(200, self.request(body)[0])
        self.assertEqual('gpt-test-small', self.requests[-1][1]['model'])

    def test_changed_plan_retains_binding_and_restoring_plan_resumes_chat(self):
        self.assertEqual(200, self.request(self.body())[0])
        original = dict(self.route)
        self.native.save_route(dict(original, mode='fixed', regular=[]))
        self.native.enable(original['id'])
        status, raw = self.request(self.body())
        self.assertEqual(502, status)
        self.assertIn('路由设置已变化', raw.decode())
        self.assertEqual(1, len(self.requests))
        with self.native.receipts() as conn:
            self.assertEqual(1, conn.execute('SELECT count(*) FROM session_bindings').fetchone()[0])
        self.native.save_route(original)
        self.native.enable(original['id'])
        self.assertEqual(200, self.request(self.body())[0])
        self.assertEqual('gpt-test-small', self.requests[-1][1]['model'])

    def test_disable_reenable_restores_binding_without_rebinding_account(self):
        self.assertEqual(200, self.request(self.body())[0])
        old_grant = self.native.grant
        self.native.disable()
        self.native.enable(self.route['id'])
        self.assertNotEqual(old_grant, self.native.grant)
        self.assertEqual(401, self.request(self.body(), token=old_grant)[0])
        self.assertEqual(200, self.request(self.body())[0])
        with self.native.receipts() as conn:
            binding = json.loads(conn.execute('SELECT binding FROM session_bindings').fetchone()[0])
        self.assertEqual(self.aid, binding['channel']['account_id'])

    def test_status_explains_native_account_pin_separately_from_model_decision(self):
        from qp import desktop
        desktop.connect(self.native)
        self.assertEqual(200, self.request(self.body(), token='synthetic-access-secret', path='/legacy/v1/responses')[0])
        row = self.native.status()['sessions'][0]
        self.assertIn('原生聊天', row['account_policy'])
        self.assertIn('不参与 Plus 优先', row['account_policy'])
        self.assertIn('本地规则', row['decision'])

    def test_session_identity_cannot_cross_entry_before_upstream_receipt(self):
        self.native.select(self.body(),{'session_id':'routed'})
        with self.assertRaises(CollectorError):
            self.native.select(self.body(),{'session_id':'routed'},legacy_account='different-account')
        self.native.select(self.body(),{'session_id':'legacy'},legacy_account=self.aid)
        with self.assertRaises(CollectorError):
            self.native.select(self.body(),{'session_id':'legacy'})
        self.assertEqual([],self.requests)

    def test_stream_records_selected_and_actual_model_usage_durably(self):
        status,raw=self.request(self.body());self.assertEqual(200,status);self.assertIn(b'response.completed',raw)
        rows=self.native.status()['sessions'];self.assertEqual(1,len(rows))
        row=rows[0];self.assertEqual('completed',row['status']);self.assertEqual('gpt-test-small',row['model'])
        self.assertEqual('gpt-test-small',row['actual_model']);self.assertEqual(19,row['input_tokens'])
        self.assertEqual(2,row['output_tokens'])
        self.assertEqual('Bearer private-adapter-key',self.requests[0][2]['Authorization'])
        self.assertNotIn('X-Should-Not-Forward',self.requests[0][2])
        with self.native.receipts() as conn:self.assertEqual(1,conn.execute('SELECT count(*) FROM requests').fetchone()[0])

    def test_complex_task_selects_primary_and_tool_continuation_pins(self):
        body=self.body('实现并发安全的缓存');self.assertEqual(200,self.request(body)[0])
        body['input'].append({'type':'function_call_output','call_id':'call_test','output':'翻译 hello'})
        self.assertEqual(200,self.request(body)[0])
        self.assertEqual(['gpt-test-large','gpt-test-large'],[r[1]['model'] for r in self.requests])

    def test_new_user_turn_can_change_model(self):
        body=self.body();self.request(body)
        body['input'].append({'role':'user','content':'重构并实现新的算法'})
        self.request(body)
        self.assertEqual(['gpt-test-small','gpt-test-large'],[r[1]['model'] for r in self.requests])

    def test_missing_binding_never_reroutes_tool_output(self):
        body=self.body();body['input'].append({'type':'function_call_output','call_id':'call_test','output':'ok'})
        self.assertEqual(502,self.request(body)[0]);self.assertEqual([],self.requests)

    def test_previous_response_id_and_missing_session_rejected(self):
        body=self.body();body['previous_response_id']='old-response'
        self.assertEqual(502,self.request(body)[0])
        self.assertEqual(502,self.request(self.body(),headers={'session_id':''})[0]);self.assertEqual([],self.requests)

    def test_error_is_not_replayed_and_upstream_secrets_not_exposed(self):
        self.failure=True;status,raw=self.request(self.body())
        self.assertEqual(429,status);self.assertEqual(1,len(self.requests));self.assertNotIn(b'secret upstream detail',raw)
        
        deadline=time.monotonic()+2
        while self.native.status()['sessions'][0]['status']=='running' and time.monotonic()<deadline:time.sleep(.01)
        self.assertEqual('failed',self.native.status()['sessions'][0]['status'])

    def test_wrong_or_revoked_token_and_cross_origin_rejected(self):
        self.assertEqual(401,self.request(self.body(),token='wrong')[0])
        self.assertEqual(401,self.request(self.body(),headers={'Origin':'https://evil.example'})[0])
        old=self.native.grant;self.native.disable()
        self.assertEqual(401,self.request(self.body(),token=old)[0]);self.assertEqual([],self.requests)
        self.assertFalse((self.native.root/'entry.json').exists())

    def test_native_state_contains_no_credentials_or_prompt(self):
        self.request(self.body())
        raw=json.dumps(self.console.snapshot())
        for value in ('synthetic-access-secret','never-copy-refresh-secret','private-adapter-key',self.native.grant,'翻译 hello'):
            self.assertNotIn(value,raw)

    def test_entry_keeps_native_home_and_does_not_edit_user_config(self):
        self.assertFalse((self.home/'config.toml').exists())
        entry=json.loads((self.native.root/'entry.json').read_text());self.assertEqual(str(self.home),entry['home'])
        self.assertEqual(0o700,(self.native.root/'codex-qpool').stat().st_mode & 0o777)
        catalog=json.loads((self.native.root/'catalog.json').read_text())
        self.assertEqual('qpool-auto',catalog['models'][0]['slug'])

    def test_duplicate_identity_and_changed_source_fail(self):
        sid=self.native.discover()['sources'][0]['id']
        with self.assertRaisesRegex(CollectorError,'已经接入'):self.native.connect({'source_id':sid})
        (self.home/'auth.json').write_text(json.dumps({'tokens':{'account_id':'other','access_token':'other'}}))
        with self.assertRaises(CollectorError):self.native.connect({'source_id':sid})

    def test_route_edit_revokes_entry_and_invalid_models_fail(self):
        with self.assertRaises(CollectorError):self.native.save_route({'name':'bad','mode':'fixed','primary':['missing']})
        self.native.save_route(dict(self.route,name='edited'))
        self.assertIsNone(self.native.grant);self.assertFalse((self.native.root/'entry.json').exists())

    def test_exhausted_quotas_fail_closed_before_upstream(self):
        from qp.db import Database
        db=Database(self.root/'ledger.db')
        with db.conn:db.conn.execute('UPDATE quota_entries SET used_percent=100')
        db.close()
        self.assertEqual(502,self.request(self.body())[0]);self.assertEqual([],self.requests)

    def test_compaction_keeps_current_binding(self):
        self.request(self.body())
        self.assertEqual(200,self.request({'input':[]},path='/v1/responses/compact')[0])
        self.assertEqual(['gpt-test-small','gpt-test-small'],[r[1]['model'] for r in self.requests])

    def test_new_login_can_rebind_existing_identity_without_json(self):
        other = self.native.root / 'logins' / 'fresh'
        other.mkdir(parents=True)
        (other/'auth.json').write_text((self.home/'auth.json').read_text())
        (self.home/'auth.json').write_text(json.dumps({'tokens':{'account_id':'other-account','access_token':'other'}}))
        source=next(s for s in self.native.discover()['sources'] if s['location']==str(other))
        self.assertFalse(source['connected']);self.assertTrue(source['reconnect'])
        self.native.connect({'source_id':source['id']})
        config=profiles.load()
        self.assertEqual(1,len(config['accounts']))
        self.assertEqual(str(other/'auth.json'),config['accounts'][self.aid]['source']['credentials_file'])
        self.assertEqual(self.route['primary'],self.native.data()['routes'][0]['primary'])
        self.assertEqual('test-subject',profiles.credentials(config['accounts'][self.aid],codex)['account_id'])

    def test_identical_user_text_is_still_a_new_turn(self):
        body=self.body('继续');first,_=self.native.select(body,{'session_id':'repeat'})
        body['input'] += [{'role':'assistant','content':'done'},{'role':'user','content':'继续'}]
        second,_=self.native.select(body,{'session_id':'repeat'})
        self.assertNotEqual(first['turn'],second['turn'])

    def test_malformed_usage_does_not_leak_slots_or_break_receipts(self):
        self.bad_usage=True
        for _ in range(5):self.assertEqual(200,self.request(self.body())[0])
        self.assertEqual(5,len(self.native.status()['sessions']))
        self.assertTrue(all(r['input_tokens'] is None for r in self.native.status()['sessions']))
        acquired=[self.native.slots.acquire(blocking=False) for _ in range(4)]
        self.assertEqual([True]*4,acquired)
        for _ in acquired:self.native.slots.release()

    def test_tool_continuation_cannot_ignore_newly_exhausted_quota(self):
        body=self.body();self.request(body)
        from qp.db import Database
        db=Database(self.root/'ledger.db')
        with db.conn:db.conn.execute('UPDATE quota_entries SET used_percent=100')
        db.close()
        body['input'].append({'type':'function_call_output','call_id':'x','output':'done'})
        self.assertEqual(502,self.request(body)[0]);self.assertEqual(1,len(self.requests))

    def test_queued_request_rechecks_quota_after_adapter_lock(self):
        real_lock=threading.RLock();waiting=threading.Event()
        class ObservedLock:
            def acquire(self):
                waiting.set();return real_lock.acquire()
            def release(self):real_lock.release()
        self.adapter_mock.return_value.lock=ObservedLock()
        real_lock.acquire();result=[]
        worker=threading.Thread(target=lambda:result.append(self.request(self.body())))
        worker.start();self.assertTrue(waiting.wait(2))
        from qp.db import Database
        db=Database(self.root/'ledger.db')
        with db.conn:db.conn.execute('UPDATE quota_entries SET used_percent=100')
        db.close();real_lock.release();worker.join(5)
        self.assertEqual(502,result[0][0]);self.assertEqual([],self.requests)

    def test_custom_tool_output_without_binding_is_rejected(self):
        body=self.body();body['input'].append({'type':'custom_tool_call_output','call_id':'x','output':'done'})
        self.assertEqual(502,self.request(body)[0]);self.assertEqual([],self.requests)

    def test_same_runtime_cannot_be_owned_by_two_services(self):
        from qp.native import Native
        with self.assertRaisesRegex(CollectorError,'已有 qpool 原生网关'):
            Native(self.root/'other.db',threading.Lock())

    def test_terminal_task_is_not_misclassified_by_reply_only_suffix(self):
        body=self.body('Use your terminal tool to run pwd. Then reply only OK.')
        self.request(body)
        self.assertEqual('gpt-test-large',self.requests[0][1]['model'])

    def test_audit_failure_revokes_entry_and_releases_slots(self):
        import contextlib, sqlite3
        original=self.native.receipts
        @contextlib.contextmanager
        def failing_receipts():
            with original() as conn:
                class Proxy:
                    def execute(self, sql, *args):
                        if sql.startswith('UPDATE requests SET status='):
                            raise sqlite3.OperationalError('synthetic disk failure')
                        return conn.execute(sql,*args)
                yield Proxy()
        with patch.object(self.native,'receipts',failing_receipts):
            self.request(self.body())
        self.assertIsNone(self.native.grant)
        self.assertIsNotNone(self.native.audit_error)
        acquired=[self.native.slots.acquire(blocking=False) for _ in range(4)]
        self.assertEqual([True]*4,acquired)
        for _ in acquired:self.native.slots.release()


class QuotaWindowTests(unittest.TestCase):
    def test_weekly_primary_is_not_misidentified_as_five_hour(self):
        with patch.object(codex,'_fetch_usage',return_value={'plan_type':'pro','rate_limit':{
            'primary_window':{'limit_window_seconds':604800,'used_percent':21}}}):
            readings=codex.fetch_readings({'account_id':'synthetic'})
        self.assertEqual(['seven_day'],[r.window_key for r in readings])

    def test_window_slots_can_swap_without_losing_limits(self):
        with patch.object(codex,'_fetch_usage',return_value={'rate_limit':{
            'primary_window':{'limit_window_seconds':604800,'used_percent':21},
            'secondary_window':{'limit_window_seconds':18000,'used_percent':32}}}):
            readings=codex.fetch_readings({'account_id':'synthetic'})
        self.assertEqual(['seven_day','five_hour'],[r.window_key for r in readings])
