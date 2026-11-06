"""Actual loopback Responses/HiRoute transports; synthetic model scores only."""
import json
import math
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_native as foundation
from qp import decisions, profiles
from qp.collectors.base import CollectorError
from qp._vendor.hiroute.decision import prepare_decision


class DecisionTests(unittest.TestCase):
    request = foundation.NativeTests.request
    body = foundation.NativeTests.body

    def setUp(self):
        foundation.NativeTests.setUp(self)
        self.probability = .92
        self.judge_delay = 0
        self.invalid = False
        self.judge_calls = []
        self.decision = {'provider':'account','channel_id':self.small,'simple_threshold':.8,'timeout_seconds':2}
        self.original_handler = self.upstream.RequestHandlerClass.do_POST
        outer = self
        def post(handler):
            # Peek is not needed: the test service decodes every request once.
            body = json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
            if body.get('instructions') == decisions.INSTRUCTIONS:
                outer.judge_calls.append((body,dict(handler.headers)))
                time.sleep(outer.judge_delay)
                content='not json' if outer.invalid else json.dumps({'answers':{'q0':{'type':'score',
                      'probabilities':{'0':outer.probability,'1':1-outer.probability}}}})
                data={'status':'completed','model':body['model'],'output':[{'type':'message','content':[
                      {'type':'output_text','text':content}]}],'usage':{'input_tokens':101,'output_tokens':23}}
                raw=json.dumps(data).encode();handler.send_response(200)
                handler.send_header('Content-Type','application/json');handler.send_header('Content-Length',str(len(raw)))
                handler.end_headers()
                try:handler.wfile.write(raw)
                except (BrokenPipeError,ConnectionResetError):pass
            else:
                outer.requests.append((handler.path,body,dict(handler.headers)))
                handler.send_response(200);handler.send_header('Content-Type','text/event-stream');handler.end_headers()
                event={'type':'response.completed','response':{'model':body['model'],'status':'completed',
                       'usage':{'input_tokens':19,'output_tokens':2}}}
                handler.wfile.write(('data: '+json.dumps(event)+'\n\n').encode())
        self.upstream.RequestHandlerClass.do_POST=post
        self.route=self.native.save_route(dict(self.route,mode='model',decision=self.decision))
        self.native.enable(self.route['id'])

    def tearDown(self):
        foundation.NativeTests.tearDown(self)

    def test_real_judge_transport_controls_model_and_has_separate_usage(self):
        self.assertEqual(200,self.request(self.body('实现一个简单的固定字符串替换'))[0])
        self.assertEqual('gpt-test-small',self.requests[0][1]['model'])
        self.assertEqual(1,len(self.judge_calls))
        sent,headers=self.judge_calls[0]
        self.assertEqual('gpt-test-small',sent['model'])
        self.assertEqual('Bearer private-adapter-key',headers['Authorization'])
        self.assertEqual([],sent['tools'])
        self.assertEqual(decisions.SCORE_FORMAT,sent['text']['format'])
        state=json.loads(sent['input'][0]['content'][0]['text'])
        self.assertEqual('score',state['questions']['q0']['type'])
        self.assertEqual(2,len(state['questions']['q0']['criteria']))
        logs=self.native.status();judge=logs['decisions'][0];request=logs['sessions'][0]
        self.assertEqual('model',judge['source']);self.assertFalse(judge['fallback'])
        self.assertEqual(101,judge['input_tokens']);self.assertEqual(19,request['input_tokens'])
        self.assertEqual(judge['id'],request['decision_id'])
        self.assertNotIn('固定字符串替换',json.dumps(logs,ensure_ascii=False))

    def test_complex_score_overrides_simple_keywords(self):
        self.probability=.2
        self.request(self.body('翻译 hello'))
        self.assertEqual('gpt-test-large',self.requests[0][1]['model'])
        self.assertEqual(.2,self.native.status()['decisions'][0]['simple_probability'])

    def test_development_policy_uses_route_owned_criteria(self):
        decision = dict(self.decision, task_policy='development_primary')
        validated = decisions.validate(decision)
        self.assertEqual('development_primary', validated['task_policy'])
        route = self.native.save_route(dict(self.route, decision=validated))
        self.native.enable(route['id'])
        self.assertEqual(200, self.request(self.body('运行现有测试并汇总结果'))[0])
        payload = json.loads(self.judge_calls[0][0]['input'][0]['content'][0]['text'])
        self.assertEqual(decisions.DEVELOPMENT_CRITERIA, payload['questions']['q0']['criteria'])
        # HiRoute maps probability 0 to regular. Do not reverse the criteria.
        self.assertTrue(payload['questions']['q0']['criteria'][0].startswith('Testing work:'))
        self.assertTrue(payload['questions']['q0']['criteria'][1].startswith('Software implementation'))
        self.assertIn('测试', self.native.status()['decisions'][0]['reason'])
        self.assertEqual(200,self.request(self.body('设计复杂测试框架'),headers={'session_id':'test-framework'})[0])
        self.assertEqual('gpt-test-small',self.requests[-1][1]['model'])
        self.probability=.01
        self.assertEqual(200,self.request(self.body('review代码'),headers={'session_id':'code-review'})[0])
        self.assertEqual('gpt-test-large',self.requests[-1][1]['model'])
        with self.assertRaises(CollectorError):decisions.validate(dict(decision,task_policy='unknown'))

    def test_tool_continuations_reuse_one_judgment(self):
        body=self.body();self.request(body)
        body['input'].append({'type':'function_call_output','call_id':'call','output':'done'})
        self.probability=.1;self.request(body)
        self.assertEqual(1,len(self.judge_calls));self.assertEqual(2,len(self.requests))
        rows=self.native.status()['sessions'];self.assertEqual(rows[0]['decision_id'],rows[1]['decision_id'])

    def test_new_turn_same_text_gets_a_new_judgment(self):
        body=self.body();self.request(body);self.probability=.1
        body['input'] += [{'role':'assistant','content':'done'},{'role':'user','content':'翻译 hello'}]
        self.request(body)
        self.assertEqual(2,len(self.judge_calls))
        self.assertEqual(['gpt-test-small','gpt-test-large'],[r[1]['model'] for r in self.requests])

    def test_invalid_answer_falls_back_and_reports_consumed_usage(self):
        self.invalid=True;self.request(self.body())
        self.assertEqual('gpt-test-large',self.requests[0][1]['model'])
        judge=self.native.status()['decisions'][0]
        self.assertTrue(judge['fallback']);self.assertEqual('invalid_output',judge['failure'])
        self.assertEqual(101,judge['input_tokens'])

    def test_timeout_bounded_and_late_result_cannot_change_selection(self):
        self.judge_delay=1.4
        start=time.monotonic();r=self.native.decision_engine.run(dict(self.decision,timeout_seconds=1),'翻译 hello',[])
        self.assertLess(time.monotonic()-start,1.25);self.assertEqual('primary',r['group']);self.assertTrue(r['fallback'])
        time.sleep(.5);self.assertEqual('primary',r['group'])
        self.assertTrue(self.native.decision_engine.slots.acquire(blocking=False));self.native.decision_engine.slots.release()

    def test_unknown_decision_model_rejected_before_save(self):
        with self.assertRaises(CollectorError):self.native.save_route(dict(self.route,decision=dict(self.decision,channel_id='missing')))

    def test_test_endpoint_does_not_execute_or_activate_route(self):
        old=self.native.grant
        result=self.native.test_decision({'decision':self.decision,'task':'翻译 hello'})
        self.assertEqual('regular',result['group']);self.assertEqual([],self.requests)
        self.assertEqual(old,self.native.grant)
        self.assertEqual('test',self.native.status()['decisions'][0]['scope'])

    def test_concurrent_same_turn_calls_judge_once(self):
        self.judge_delay=.1
        outcomes=[]
        threads=[threading.Thread(target=lambda:outcomes.append(self.request(self.body())[0])) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(5)
        self.assertEqual([200,200],outcomes);self.assertEqual(1,len(self.judge_calls))

    def test_different_session_is_not_blocked_by_another_sessions_judge(self):
        started=threading.Event();release=threading.Event()
        real_run=self.native.decision_engine.run
        def slow(settings,text,items):
            if text=='slow':started.set();release.wait(2)
            return real_run(settings,text,items)
        out=[]
        with patch.object(self.native.decision_engine,'run',side_effect=slow):
            worker=threading.Thread(target=lambda:self.native.select(self.body('slow'),{'session_id':'slow'}))
            worker.start();self.assertTrue(started.wait(1))
            second=threading.Thread(target=lambda:out.append(self.native.select(self.body(),{'session_id':'fast'})))
            second.start();second.join(1)
            self.assertEqual(1,len(out));release.set();worker.join(3)

    def test_reenable_same_route_preserves_binding_and_decision(self):
        self.request(self.body());self.native.enable(self.route['id']);self.probability=.1
        status, raw = self.request(self.body())
        self.assertEqual(200, status)
        self.assertEqual(1, len(self.judge_calls))
        self.assertEqual(2, len(self.requests))
        self.assertEqual(200, self.request(self.body(), headers={'session_id':'new-session'})[0])
        self.assertEqual(2,len(self.judge_calls));self.assertEqual('gpt-test-large',self.requests[-1][1]['model'])

    def test_external_hiroute_protocol_keeps_service_group(self):
        outer=self
        def post(handler):
            body=json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
            outer.judge_calls.append(body)
            raw=b'{"group":"regular","reason":"Simple task"}'
            handler.send_response(200);handler.send_header('Content-Length',str(len(raw)));handler.end_headers();handler.wfile.write(raw)
        self.upstream.RequestHandlerClass.do_POST=post
        cfg={'provider':'hiroute','endpoint':f'http://127.0.0.1:{self.upstream.server_port}/v1/decisions','timeout_seconds':2,'auth_env':''}
        result=self.native.test_decision({'decision':cfg,'task':'继续'})
        self.assertEqual('service',result['source']);self.assertEqual('regular',result['group'])
        self.assertEqual({'route','latest_user','visible_conversation','history_partial','preceding_stage'},set(self.judge_calls[0]))
        self.assertIsNone(result['simple_threshold'])

    def test_waiting_for_busy_execution_account_respects_deadline(self):
        lock=self.adapter_mock.return_value.lock;lock.acquire()
        try:
            started=time.monotonic()
            result=self.native.decision_engine.run(dict(self.decision,timeout_seconds=1),'翻译 hello',[])
            self.assertLess(time.monotonic()-started,1.25)
            self.assertTrue(result['fallback']);self.assertEqual([],self.judge_calls)
        finally:lock.release()
        # A timed lock acquisition must not linger and send a late inference.
        time.sleep(.1);self.assertEqual([],self.judge_calls)
        slots=[self.native.decision_engine.slots.acquire(blocking=False) for _ in range(2)]
        self.assertEqual([True,True],slots)
        for _ in slots:self.native.decision_engine.slots.release()

    def test_slow_quota_preflight_never_holds_execution_lock_or_sends_late(self):
        entered=threading.Event();release=threading.Event();real=self.native.validate_binding
        def blocked(channel,**kwargs):
            entered.set();release.wait(2);return real(channel,**kwargs)
        result=[]
        with patch.object(self.native,'validate_binding',side_effect=blocked):
            worker=threading.Thread(target=lambda:result.append(self.native.decision_engine.run(
                dict(self.decision,timeout_seconds=1),'翻译 hello',[])))
            worker.start();self.assertTrue(entered.wait(1));worker.join(1.3)
            self.assertTrue(result[0]['fallback'])
            lock=self.adapter_mock.return_value.lock
            self.assertTrue(lock.acquire(blocking=False));lock.release()
            release.set();time.sleep(.1)
        self.assertEqual([],self.judge_calls)

    def test_connect_consumes_deadline_and_never_sends_late(self):
        from unittest.mock import MagicMock
        connection = MagicMock()
        connection.connect.side_effect = lambda: time.sleep(1.2)
        with patch('qp.decisions.http.client.HTTPConnection', return_value=connection):
            result = self.native.decision_engine.run(dict(self.decision, timeout_seconds=1), '翻译 hello', [])
            self.assertTrue(result['fallback'])
            time.sleep(.3)
        connection.request.assert_not_called()
        connection.close.assert_called_once()


class PolicyTests(unittest.TestCase):
    def test_exact_threshold_and_invalid_distributions(self):
        prepared=prepare_decision(SimpleNamespace(model='test',max_request_bytes=32768,simple_threshold=.8,competence_floor=.5),decisions.context('hello',[]))
        def response(prob):return {'status':'completed','output':[{'type':'message','content':[{'type':'output_text',
              'text':json.dumps({'answers':{'q0':{'type':'score','probabilities':prob}}})}]}]}
        self.assertEqual('regular',decisions.score_response(prepared,response({'0':.8,'1':.2}))[0])
        self.assertEqual('primary',decisions.score_response(prepared,response({'0':.799,'1':.201}))[0])
        for prob in ({'0':True,'1':0},{'0':.9,'1':.9},{'0':math.nan,'1':.2},{'0':-1,'1':2},{'0':1,'extra':0}):
            with self.assertRaises(ValueError):decisions.score_response(prepared,response(prob))

    def test_context_excludes_system_and_tools_and_preserves_reference_history(self):
        items=[{'role':'system','content':'system-private'}, {'role':'user','content':'历史任务'},
               {'role':'assistant','content':'历史回答'}, {'type':'function_call_output','output':'tool-private'},
               {'role':'user','content':'继续'}]
        data=decisions.context('继续',items);wire=json.dumps(data,ensure_ascii=False)
        self.assertNotIn('system-private',wire);self.assertNotIn('tool-private',wire)
        self.assertIn('历史任务',wire);self.assertIn('历史回答',wire)
        self.assertEqual(1,len(data['visible_conversation']));self.assertTrue(data['history_partial'])
        self.assertIsNone(data['preceding_stage'])

    def test_context_budget_preserves_current_task_and_discloses_trim(self):
        items=[{'role':'user','content':'old'*6000} for _ in range(10)]+[{'role':'user','content':'current'}]
        data=decisions.context('current',items)
        self.assertEqual('current',data['latest_user'][0]['text']);self.assertTrue(data['history_partial'])
        self.assertLessEqual(len(json.dumps(data,ensure_ascii=False).encode()),decisions.MAX_CONTEXT)
        with self.assertRaises(CollectorError):decisions.context('a'*16001,[])

    def test_unsupported_latest_content_is_disclosed(self):
        d=decisions.context('描述图片',[{'role':'user','content':[{'type':'input_text','text':'描述图片'},{'type':'input_image','image_url':'private'}]}])
        self.assertTrue(d['history_partial']);self.assertEqual('unavailable',d['latest_user'][1]['kind'])
        self.assertNotIn('private',json.dumps(d))
