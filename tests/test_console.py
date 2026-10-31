"""Real local HTTP and CLI fixtures; no vendor requests or real credentials."""
import contextlib
import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from qp import cli, executors, ledger, pools, profiles, routing, tasks, web
from qp.db import Database


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config_path = self.root / 'pools.json'
        self.db_path = self.root / 'ledger.db'
        self.env = patch.dict(os.environ, {'QPOOL_CONFIG': str(self.config_path), 'QPOOL_DB': str(self.db_path),
                                            'QPOOL_COLLECTORS_DIR': str(self.root / 'no-collectors')})
        self.env.start()
        auth = self.root / 'auth.json'
        auth.write_text(json.dumps({'id':'synthetic-subject','token':'synthetic-secret'}))
        runner = self.root / 'runner.py'
        runner.write_text('''import os,sys,time,json,pathlib
with open(pathlib.Path(os.environ['TEST_PROFILE'])/'runs','a') as log: log.write(sys.argv[1]+'\\n')
if sys.argv[2]=='slow': time.sleep(0.6)
if sys.argv[2]=='fail': sys.exit(3)
print(json.dumps({'model':sys.argv[1],'task':sys.argv[2]}))
''')
        self.raw = {'accounts':[{'id':'test-account','agent':'fixture','source':{
            'credentials_file':str(auth),'fields':{'id':'id','token':'token'},'subject_field':'id',
            'expected_subject':'synthetic-subject','profile_env':'TEST_PROFILE','profile_dir':str(self.root)},
            'pools':[{'id':'sub','kind':'subscription','unit':'tokens','capability_tier':1,
                      'windows':[{'key':'manual'}]}]}], 'channels':[]}
        for name, tier in [('regular',2),('primary',5)]:
            self.raw['channels'].append({'id':name,'type':'cli','account_id':'test-account','pool_id':'sub',
                                         'model':'model-'+name,'group':name,'capability_tier':tier,
                                         'command':[sys.executable,str(runner),'{model}','{task}'],
                                         'env':{'TEST_PROFILE':str(self.root)}})
        self.save()
        self.db = Database(self.db_path)
        config = profiles.load()
        pools.register(self.db, config)
        entry = ledger.add_entry(self.db, agent='fixture',account='test-account',kind='subscription',
                                 total=1000000,remaining=1000000,unit='tokens',reset_period='monthly')
        pools.bind_entry(self.db,config,entry,'sub','manual')

    def save(self):
        self.config_path.write_text(json.dumps(self.raw))

    def tearDown(self):
        self.db.close()
        self.env.stop()
        self.tmp.cleanup()


class RoutingTests(Fixture):
    def test_output_io_failure_after_start_is_never_retry_safe(self):
        plan=routing.plan_task(self.db,'slow',mode='regular')
        real_fstat=os.fstat
        def broken_spool(fd):
            if (self.root/'runs').exists():
                raise OSError('synthetic output failure')
            return real_fstat(fd)
        with patch('qp.executors.os.fstat',side_effect=broken_spool):
            result=executors.execute(plan['candidates'][0],'slow',db=self.db,cwd=str(self.root))
        self.assertFalse(result.success)
        self.assertFalse(result.retry_safe)
        self.assertEqual(['model-regular'],(self.root/'runs').read_text().splitlines())

    def test_no_service_falls_back_to_primary_and_executes_exact_model(self):
        plan=routing.plan_task(self.db,'hello',mode='auto')
        self.assertEqual('fallback',plan['decision']['source'])
        self.assertEqual(['primary'],[c['channel_id'] for c in plan['candidates']])
        result=executors.execute(plan['candidates'][0],'hello',db=self.db,cwd=str(self.root))
        self.assertTrue(result.success,result.error)
        self.assertEqual('model-primary',json.loads(result.output)['model'])

    def test_regular_before_primary_and_channel_tier_not_pool_tier(self):
        plan=routing.plan_task(self.db,'hello',mode='regular')
        self.assertEqual(['regular','primary'],[c['channel_id'] for c in plan['candidates']])
        plan=routing.plan_task(self.db,'hello',mode='manual',min_tier=4)
        self.assertEqual(['primary'],[c['channel_id'] for c in plan['candidates']])

    def test_unclassified_legacy_channels_still_work_manually(self):
        for channel in self.raw['channels']:
            channel.pop('group');channel.pop('capability_tier')
        self.save()
        self.assertEqual(2,len(routing.plan_task(self.db,'hello')['candidates']))
        self.assertEqual([],routing.plan_task(self.db,'hello',mode='auto')['candidates'])

    def test_paid_regular_does_not_beat_free_primary(self):
        account=copy.deepcopy(self.raw['accounts'][0]);account['id']='paid';account['source']['expected_subject']='paid-subject'
        paid_dir=self.root/'paid';paid_dir.mkdir();auth=paid_dir/'auth.json';auth.write_text(json.dumps({'id':'paid-subject'}))
        account['source'].update(credentials_file=str(auth),profile_dir=str(paid_dir),fields={'id':'id'})
        account['pools']=[{'id':'payg','kind':'payg','cost_per_unit':0.01,'unit':'tokens'}]
        self.raw['accounts'].append(account)
        self.raw['channels'][0].update(account_id='paid',pool_id='payg',env={'TEST_PROFILE':str(paid_dir)})
        self.save();pools.register(self.db,profiles.load())
        plan=routing.plan_task(self.db,'hello',mode='regular',max_cost=0)
        self.assertEqual(['primary'],[c['channel_id'] for c in plan['candidates']])
        self.assertIn('above limit',plan['skipped'][0]['why'])

    def test_manual_dry_run_does_not_call_decider(self):
        with patch('qp.routing.decide') as decide, contextlib.redirect_stdout(io.StringIO()):
            cli.main(['quota','run','hello','--dry-run'])
        decide.assert_not_called()

    def test_public_plan_excludes_credentials_and_commands(self):
        raw=json.dumps(routing.public_plan(routing.plan_task(self.db,'hello')))
        for value in ('synthetic-secret','synthetic-subject','credentials_file','TEST_PROFILE','command'):
            self.assertNotIn(value,raw)

    def test_invalid_amount_rejected_before_decision(self):
        with patch('qp.routing.decide') as decide:
            with self.assertRaises(ledger.LedgerError):
                routing.plan_task(self.db,'hello',mode='auto',amount=float('inf'))
        decide.assert_not_called()


class DecisionTests(unittest.TestCase):
    def setUp(self):
        test=self
        self.response=b'{"group":"regular"}'
        self.delay=0
        self.requests=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                test.requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                time.sleep(test.delay)
                self.send_response(200);self.end_headers()
                try:self.wfile.write(test.response)
                except (BrokenPipeError, ConnectionResetError):pass
            def log_message(self,*args):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()
        self.settings={'endpoint':f'http://127.0.0.1:{self.server.server_port}/v1/decisions','timeout_seconds':0.2}

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()

    def test_current_protocol_and_reason_optional(self):
        result=routing.decide('hello',self.settings)
        self.assertEqual('regular',result['group']);self.assertEqual('service',result['source'])
        self.assertIsNone(result['cost_usd'])
        self.assertEqual({'route':{'kind':'smart_saving','groups':['regular','primary']},
                          'latest_user':[{'kind':'text','text':'hello'}],'visible_conversation':[],
                          'history_partial':False,'preceding_stage':None},self.requests[0])

    def test_invalid_legacy_duplicate_and_unknown_responses_fallback(self):
        for response in (b'{"simple":0.9}',b'{"group":"regular","group":"primary"}',
                         b'{"group":"regular","model":"invented"}',b'{"group":"other"}',
                         b'{"group":"regular","assessment":{"score":NaN}}', b'x'*65537):
            with self.subTest(response=response[:80]):
                self.response=response
                result=routing.decide('hello',self.settings)
                self.assertEqual('primary',result['group']);self.assertEqual('fallback',result['source'])

    def test_total_deadline_fallback(self):
        self.delay=0.5
        start=time.monotonic();result=routing.decide('hello',self.settings)
        self.assertLess(time.monotonic()-start,0.4)
        self.assertEqual('fallback',result['source'])

    def test_remote_plaintext_and_embedded_credentials_rejected(self):
        for endpoint in ('http://example.com/v1','https://secret@example.com/v1','https://example.com/v1?key=secret'):
            with self.assertRaises(ValueError):routing.validate_settings({'endpoint':endpoint})


class ConsoleTests(Fixture):
    def setUp(self):
        super().setUp()
        self.console=web.Console(self.db_path)
        self.server=web.Server(('127.0.0.1',0),self.console)
        self.thread=threading.Thread(target=self.server.serve_forever);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.console.native.close();self.console.manager.close()
        super().tearDown()

    def request(self,path,data=None,extra=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        headers={'X-Qpool-Request':'1'}
        if extra:headers.update(extra)
        if data is not None:headers['Content-Type']='application/json'
        connection.request('GET' if data is None else 'POST',path,json.dumps(data) if data is not None else None,headers)
        response=connection.getresponse();status=response.status;body=response.read();connection.close()
        return status,json.loads(body) if body.startswith((b'{',b'[')) else body

    def preview(self,task='hello'):
        status,result=self.request('/api/preview',{'task':task,'cwd':str(self.root),'mode':'auto','max_cost':0})
        self.assertEqual(200,status,result);return result

    def wait_task(self,tid):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            status,result=self.request('/api/tasks/'+tid)
            if result['status']!='running':return result
            time.sleep(.03)
        self.fail('task did not finish')

    def test_browser_flow_exact_model_and_durable_history(self):
        preview=self.preview()
        status,run=self.request('/api/run',{'preview_id':preview['id']});self.assertEqual(200,status)
        task=self.wait_task(run['id'])
        self.assertEqual('succeeded',task['status']);self.assertEqual('model-primary',json.loads(task['outcome']['output'])['model'])
        self.assertEqual('primary',task['outcome']['attempts'][0]['channel_id'])
        with sqlite3.connect(str(self.console.manager.path)) as db:
            self.assertEqual('succeeded',db.execute('SELECT status FROM tasks WHERE id=?',(run['id'],)).fetchone()[0])

    def test_duplicate_start_and_workspace_exclusion(self):
        first=self.preview('slow');second=self.preview('slow')
        _,run=self.request('/api/run',{'preview_id':first['id']})
        _,again=self.request('/api/run',{'preview_id':first['id']})
        self.assertEqual(run,again)
        status,error=self.request('/api/run',{'preview_id':second['id']})
        self.assertEqual(400,status);self.assertIn('已有任务',error['error'])
        with self.assertRaises(ledger.LedgerError):
            with tasks.workspace_lock(str(self.root),self.db_path):pass
        with self.assertRaises(ledger.LedgerError):
            with tasks.workspace_lock(str(self.root),self.root/'another-db'/'ledger.db'):pass
        self.wait_task(run['id'])
        self.assertEqual(['model-primary'],(self.root/'runs').read_text().splitlines())

    def test_preview_does_not_execute_and_config_change_rejected(self):
        preview=self.preview();self.assertFalse((self.root/'runs').exists())
        self.raw['channels'][1]['model']='changed';self.save()
        status,error=self.request('/api/run',{'preview_id':preview['id']})
        self.assertEqual(400,status);self.assertIn('配置已变化',error['error']);self.assertFalse((self.root/'runs').exists())

    def test_expired_and_failed_task_do_not_retry(self):
        preview=self.preview('fail')
        self.console.manager.previews[preview['id']]['at']-=601
        self.assertEqual(400,self.request('/api/run',{'preview_id':preview['id']})[0])
        preview=self.console.manager.preview({'task':'fail','cwd':str(self.root),'mode':'regular'})
        run=self.console.manager.start(preview['id']);task=self.wait_task(run['id'])
        self.assertEqual('failed',task['status']);self.assertEqual(1,len(task['outcome']['attempts']))
        self.assertEqual(['model-regular'],(self.root/'runs').read_text().splitlines())

    def test_settings_preserve_identity_reject_stale_and_invalid(self):
        state=self.request('/api/state')[1];settings=state['settings']
        secret_before=copy.deepcopy(self.raw['accounts'])
        data={'revision':settings['revision'],'routing':settings['routing'],
              'channels':[{'id':'regular','group':'primary','capability_tier':4}]}
        self.assertEqual(200,self.request('/api/settings',data)[0])
        raw=json.loads(self.config_path.read_text());self.assertEqual(secret_before,raw['accounts'])
        self.assertEqual('primary',raw['channels'][0]['group'])
        self.assertEqual(400,self.request('/api/settings',data)[0])
        before=self.config_path.read_text();data['revision']=tasks.config_revision();data['channels'][0]['capability_tier']=6
        self.assertEqual(400,self.request('/api/settings',data)[0]);self.assertEqual(before,self.config_path.read_text())

    def test_no_secret_exposure_and_cross_origin_rejected(self):
        status,state=self.request('/api/state');self.assertEqual(200,status)
        serialized=json.dumps(state)
        for secret in ('synthetic-subject','synthetic-secret','credentials_file','TEST_PROFILE'):
            self.assertNotIn(secret,serialized)
        self.assertEqual(403,self.request('/api/state',extra={'Origin':'https://evil.invalid'})[0])
        self.assertEqual(403,self.request('/api/state',extra={'Host':'evil.invalid'})[0])
        self.assertEqual(403,self.request('/api/run',{},extra={'X-Qpool-Request':'0'})[0])
        self.assertEqual(200,self.request('/')[0])
        self.assertEqual(404,self.request('/../profiles.py')[0])

    def test_single_console_owner(self):
        with self.assertRaises(ledger.LedgerError):tasks.TaskManager(self.db_path)

    def test_invalid_payload_does_not_produce_server_error(self):
        for payload in ({'task':'x','cwd':[]},{'task':'x','amount':True},{'task':'x','timeout':'bad'}, {'task':'x','min_tier':1.5}):
            self.assertEqual(400,self.request('/api/preview',payload)[0])


if __name__=='__main__':unittest.main()
