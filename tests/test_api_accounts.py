"""API account lifecycle and routing against isolated local fixtures."""
import http.client
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import test_native as foundation
from qp import openai_account, profiles
from qp.collectors.base import CollectorError

KEY = 'sk-synthetic-existing-key-for-tests-only'


class APIAccountTests(unittest.TestCase):
    setUp = foundation.NativeTests.setUp
    tearDown = foundation.NativeTests.tearDown
    request = foundation.NativeTests.request
    body = foundation.NativeTests.body

    def connect_api(self):
        with patch('qp.openai_account.list_models', return_value=['gpt-test-small', 'gpt-test-large', 'other-embedding']):
            result = self.native.connect_api({'api_key': KEY, 'label': 'Test API', 'consent': True})
        return result['connected']

    def use_real_api_adapter(self):
        fake = self.adapter_mock.return_value
        native_type = type(self.native)
        patch.object(self.native, 'adapter', side_effect=lambda a, **kw:
                     native_type.adapter(self.native, a, **kw) if a['agent'] == 'openai_api' else fake).start()
        patch('qp.openai_account.connection', side_effect=lambda adapter, timeout:
              http.client.HTTPConnection('127.0.0.1', self.upstream.server_port, timeout=timeout)).start()

    def test_api_onboarding_private_key_and_sanitized_state(self):
        aid = self.connect_api()
        cfg = profiles.load();a = cfg['accounts'][aid]
        path = Path(a['source']['credentials_file'])
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual(KEY, json.loads(path.read_text())['api_key'])
        self.assertNotIn(KEY, profiles.config_path().read_text())
        state = self.console.snapshot()
        self.assertNotIn(KEY, json.dumps(state))
        inventory = next(x for x in state['accounts'] if x['id'] == aid)['model_inventory']
        self.assertEqual(2, len(inventory['selectable']))
        self.assertIn('other-embedding', [m['model'] for m in inventory['excluded']])
        self.assertIsNone(next(p for p in state['pools'] if p['account_id'] == aid)['channels'][0]['est'])

    def test_explicit_storage_consent_and_key_validation(self):
        with patch('qp.openai_account.list_models') as fetch:
            for data in ({'api_key': KEY}, {'api_key': KEY, 'consent': False}, {'api_key': 'wrong', 'consent': True}):
                with self.assertRaises(CollectorError):self.native.connect_api(data)
            fetch.assert_not_called()
        self.assertFalse((self.native.root/'api-keys').exists())

    def test_duplicate_key_does_not_create_another_account(self):
        aid = self.connect_api();self.assertEqual(aid, self.connect_api())
        self.assertEqual(1, sum(a['agent'] == 'openai_api' for a in profiles.load()['accounts'].values()))

    def test_api_route_requires_paid_opt_in_and_calls_correct_key(self):
        aid = self.connect_api();self.use_real_api_adapter()
        route = {'name':'paid','mode':'fixed','primary':[aid+'-gpt-test-small']}
        with self.assertRaises(CollectorError):self.native.save_route(route)
        r = self.native.save_route(dict(route, allow_api=True));self.native.enable(r['id'])
        before = self.readings.call_count
        self.assertEqual(200, self.request(self.body())[0])
        self.assertEqual(before, self.readings.call_count)
        self.assertEqual('Bearer '+KEY, self.requests[0][2]['Authorization'])
        self.assertEqual('Test API', self.native.status()['sessions'][0]['account'])

    def test_subscription_is_preferred_to_api_when_both_selected(self):
        aid = self.connect_api()
        r = self.native.save_route({'name':'both','mode':'fixed','allow_api':True,
                                   'primary':[aid+'-gpt-test-small',self.small]})
        self.native.enable(r['id'])
        binding,_ = self.native.select(self.body(), {'session_id':'priority'})
        self.assertEqual(self.aid, binding['channel']['account_id'])

    def subscription_priority_fixture(self):
        # Import another distinct subscription through the real onboarding path.
        home=self.native.root/'logins'/'plus-fixture';home.mkdir(parents=True)
        (home/'auth.json').write_text(json.dumps({'tokens':{'account_id':'plus-subject','access_token':'synthetic-plus'}}))
        source=next(s for s in self.native.discover()['sources'] if s['location']==str(home))
        plus=self.native.connect({'source_id':source['id']})['connected']
        paid=self.connect_api()
        r=self.native.save_route({'name':'Plus then Pro then API','mode':'local','allow_api':True,
            'regular':[plus+'-gpt-test-small',self.small,paid+'-gpt-test-small'],
            'primary':[plus+'-gpt-test-large',self.large,paid+'-gpt-test-large']})
        self.native.enable(r['id'])
        return plus,paid

    def test_plus_then_pro_then_api_for_new_tasks_and_both_groups(self):
        plus,paid=self.subscription_priority_fixture()
        for blocked,expected in ((set(),plus),({plus},self.aid),({plus,self.aid},paid)):
            for text,model in (('翻译 hello','gpt-test-small'),('实现并发算法','gpt-test-large')):
                def verdict(db,pool,model):return {'state':'blocked' if pool['account_id'] in blocked else 'ready'}
                with patch('qp.native.pools.evaluate',side_effect=verdict):
                    binding,_=self.native.select(self.body(text),{'session_id':str((sorted(blocked),text))})
                self.assertEqual(expected,binding['channel']['account_id'])
                self.assertEqual(model,binding['channel']['model'])

    def test_unknown_subscription_does_not_authorize_api_spend(self):
        plus,paid=self.subscription_priority_fixture()
        def verdict(db,pool,model):return {'state':'unknown' if pool['account_id']==plus else 'blocked'}
        with patch('qp.native.pools.evaluate',side_effect=verdict), \
             patch.object(self.native,'refresh',side_effect=CollectorError('unavailable')):
            with self.assertRaisesRegex(CollectorError,'订阅额度尚未确认'):
                self.native.select(self.body(),{'session_id':'unknown-quota'})

    def test_plus_primary_precedes_pro_regular_when_plus_regular_unavailable(self):
        plus, _ = self.subscription_priority_fixture()
        def verdict(db, pool, model):
            return {'state':'blocked' if pool['account_id'] == plus and model == 'gpt-test-small' else 'ready'}
        with patch('qp.native.pools.evaluate', side_effect=verdict):
            binding, _ = self.native.select(self.body(), {'session_id':'plus-primary-fallback'})
        self.assertEqual(plus, binding['channel']['account_id'])
        self.assertEqual('gpt-test-large', binding['channel']['model'])

    def test_missing_plus_regular_catalog_entry_keeps_account_priority(self):
        plus, _ = self.subscription_priority_fixture()
        config = profiles.load()
        for channel in config['channels']:
            if channel['account_id'] == plus and channel['model'] == 'gpt-test-small':
                channel['available'] = False
        with patch('qp.native.profiles.load', return_value=config):
            binding, _ = self.native.select(self.body(), {'session_id':'plus-catalog-fallback'})
        self.assertEqual(plus, binding['channel']['account_id'])
        self.assertEqual('gpt-test-large', binding['channel']['model'])

    def test_unknown_plus_still_allows_ready_pro(self):
        plus,paid=self.subscription_priority_fixture()
        def verdict(db,pool,model):return {'state':'unknown' if pool['account_id']==plus else 'ready'}
        with patch('qp.native.pools.evaluate',side_effect=verdict), \
             patch.object(self.native,'refresh',side_effect=CollectorError('unavailable')):
            binding,_=self.native.select(self.body(),{'session_id':'known-pro'})
        self.assertEqual(self.aid,binding['channel']['account_id'])

    def test_api_decision_test_requires_its_own_paid_opt_in(self):
        aid=self.connect_api()
        data={'task':'hello','decision':{'provider':'account','channel_id':aid+'-gpt-test-small'}}
        with patch.object(self.native.decision_engine,'run') as run:
            with self.assertRaises(CollectorError):self.native.test_decision(data)
            run.assert_not_called()
            run.return_value={'group':'regular'}
            self.native.test_decision(dict(data,allow_api=True))
            run.assert_called_once()

    def test_stopping_model_sync_cannot_publish_late_results(self):
        import threading
        before=profiles.config_path().read_text();entered=threading.Event();release=threading.Event()
        def delayed():entered.set();release.wait(3);return ['gpt-test-large']
        self.adapter_mock.return_value.refresh_models=delayed
        worker=threading.Thread(target=self.native.sync_models);worker.start()
        self.assertTrue(entered.wait(1));self.native.models_stop.set();release.set();worker.join(2)
        self.assertFalse(worker.is_alive());self.assertEqual(before,profiles.config_path().read_text())

    def test_key_identity_change_and_permissions_rejected(self):
        aid = self.connect_api();account = profiles.load()['accounts'][aid]
        adapter = openai_account.APIAdapter(None,account)
        adapter.ensure();path = Path(account['source']['credentials_file'])
        path.chmod(0o644)
        with self.assertRaises(CollectorError):adapter.ensure()
        path.chmod(0o600);path.write_text(json.dumps({'api_key':KEY+'changed'}))
        with self.assertRaises(CollectorError):adapter.ensure()

    def test_model_sync_adds_models_and_revokes_removed_without_deleting_route(self):
        cache = self.home/'models_cache.json';data = json.loads(cache.read_text())
        data['models'].append(dict(data['models'][0],slug='gpt-new'))
        cache.write_text(json.dumps(data))
        self.adapter_mock.return_value.refresh_models = lambda: ['gpt-test-large','gpt-new']
        result = self.native.sync_models(self.aid)
        self.assertFalse(result['errors'])
        channels = {c['model']: c for c in profiles.load()['channels']}
        self.assertTrue(channels['gpt-new']['available']);self.assertFalse(channels['gpt-test-small']['available'])
        with self.assertRaises(CollectorError):self.native.enable(self.route['id'])
        self.assertEqual(1,len(self.native.data()['routes']))

    def test_failed_sync_keeps_last_known_directory(self):
        before = profiles.config_path().read_text()
        def fail():raise OSError('sensitive upstream response')
        self.adapter_mock.return_value.refresh_models = fail
        result = self.native.sync_models(self.aid)
        self.assertTrue(result['errors']);self.assertNotIn('sensitive',json.dumps(result))
        self.assertEqual(before,profiles.config_path().read_text())

    def test_sync_preserves_legacy_binding_when_only_default_availability_added(self):
        raw=json.loads(profiles.config_path().read_text())
        for c in raw['channels']:c.pop('available',None)
        profiles.config_path().write_text(json.dumps(raw))
        binding,_=self.native.select(self.body(),{'session_id':'legacy'})
        self.adapter_mock.return_value.refresh_models=lambda:['gpt-test-small','gpt-test-large']
        self.native.sync_models(self.aid)
        self.native.validate_binding(binding['channel'])

    def test_hidden_models_are_not_route_choices(self):
        cache = self.home/'models_cache.json';data = json.loads(cache.read_text())
        data['models'][0]['visibility'] = 'hide';cache.write_text(json.dumps(data))
        self.adapter_mock.return_value.refresh_models = lambda:['gpt-test-small','gpt-test-large']
        self.native.sync_models(self.aid)
        c = next(c for c in profiles.load()['channels'] if c['id']==self.small)
        self.assertFalse(c['available'])

    def test_api_post_errors_never_echo_secret(self):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port)
        with patch('qp.openai_account.list_models',side_effect=OSError(KEY)):
            conn.request('POST','/api/accounts/api-connect',json.dumps({'api_key':KEY,'consent':True}),
                         {'Content-Type':'application/json','X-Qpool-Request':'1'})
            response=conn.getresponse();raw=response.read();conn.close()
        self.assertNotIn(KEY.encode(),raw)


class OfficialTransportTests(unittest.TestCase):
    def test_fixed_host_no_redirect_and_no_body_echo(self):
        from unittest.mock import MagicMock
        for status in (302,401,403):
            c=MagicMock();c.getresponse.return_value.status=status
            with patch('qp.openai_account.http.client.HTTPSConnection',return_value=c) as factory:
                with self.assertRaises(CollectorError):openai_account.list_models(KEY)
                factory.assert_called_once_with('api.openai.com',timeout=15)
                c.getresponse.return_value.read.assert_not_called()
                c.close.assert_called_once()


class SubscriptionCatalogTests(unittest.TestCase):
    def adapter(self):
        import threading
        from qp.native import Adapter
        a=object.__new__(Adapter)
        a.account={'source':{'credentials_file':'unused','expected_subject':'subject'}}
        a.lock=threading.Lock();a.port=1234;a.key='synthetic';a.models=['gpt-old']
        a.ensure=lambda **kw:a
        return a

    def refresh(self, expected, replies, timeout=1):
        import time
        from unittest.mock import MagicMock
        a=self.adapter();opener=MagicMock()
        raw=[json.dumps({'data':[{'id':m} for m in models]}).encode() for models in replies]
        response=opener.open.return_value.__enter__.return_value
        response.read.side_effect=raw
        with patch('qp.native.read_login',return_value={}), patch('qp.native.metadata',return_value={'plan':'plus'}), \
             patch('qp.native.model_catalog.subscription_models',return_value=expected), \
             patch('qp.native.urllib.request.build_opener',return_value=opener):
            result=a.refresh_models(deadline=time.monotonic()+timeout)
        return a,result

    def test_bootstrap_embedded_list_cannot_delete_new_models(self):
        a,result=self.refresh(['gpt-new'],[['gpt-old'],['gpt-old','gpt-new']])
        self.assertEqual(['gpt-new'],result);self.assertEqual(result,a.models)

    def test_public_catalog_confirms_removal_even_if_local_catalog_is_stale(self):
        a,result=self.refresh(['gpt-new'],[['gpt-old','gpt-new']])
        self.assertNotIn('gpt-old',a.models)

    def test_public_lookup_failure_preserves_old_and_never_locks_account(self):
        import time
        a=self.adapter()
        def unavailable(*args):
            self.assertTrue(a.lock.acquire(blocking=False));a.lock.release()
            raise CollectorError('public unavailable')
        with patch('qp.native.read_login',return_value={}), patch('qp.native.metadata',return_value={'plan':'plus'}), \
             patch('qp.native.model_catalog.subscription_models',side_effect=unavailable):
            with self.assertRaises(CollectorError):a.refresh_models(deadline=time.monotonic()+1)
        self.assertEqual(['gpt-old'],a.models)

    def test_pending_local_catalog_timeout_preserves_old(self):
        import time
        from unittest.mock import MagicMock
        a=self.adapter();opener=MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value=b'{"data":[{"id":"gpt-old"}]}'
        with patch('qp.native.read_login',return_value={}), patch('qp.native.metadata',return_value={'plan':'plus'}), \
             patch('qp.native.model_catalog.subscription_models',return_value=['gpt-new']), \
             patch('qp.native.urllib.request.build_opener',return_value=opener):
            with self.assertRaises(CollectorError):a.refresh_models(deadline=time.monotonic()+.03)
        self.assertEqual(['gpt-old'],a.models)

    def test_public_catalog_is_validated_as_a_whole_and_has_no_auth(self):
        import time
        from unittest.mock import MagicMock
        from qp import model_catalog
        for data in ({}, {'codex-plus':[]}, {'codex-plus':[{'id':'valid'},{'id':'bad\nname'}]}):
            opener=MagicMock();opener.open.return_value.__enter__.return_value.read.return_value=json.dumps(data).encode()
            with patch('qp.model_catalog.urllib.request.build_opener',return_value=opener):
                with self.assertRaises(CollectorError):model_catalog.subscription_models('plus',time.monotonic()+1)
                self.assertEqual(model_catalog.SOURCE,opener.open.call_args.args[0])
        with self.assertRaises(CollectorError):model_catalog.subscription_models('unknown',time.monotonic()+1)

    def test_plan_mapping_matches_supported_cpa_plans(self):
        import time
        from unittest.mock import MagicMock
        from qp import model_catalog
        opener=MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value=b'{"codex-team":[{"id":"team-model"}],"codex-free":[{"id":"free-model"}]}'
        with patch('qp.model_catalog.urllib.request.build_opener',return_value=opener):
            self.assertEqual(['team-model'],model_catalog.subscription_models(' GO ',time.monotonic()+1))
            for plan in ('enterprise','edu'):
                with self.assertRaises(CollectorError):model_catalog.subscription_models(plan,time.monotonic()+1)
