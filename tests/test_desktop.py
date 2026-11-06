"""Desktop config ownership, restart grants, and credential helper boundaries."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import tomlkit

from qp import desktop
from qp.collectors.base import CollectorError


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / 'codex'
        self.home.mkdir()
        self.path = self.home / 'config.toml'
        self.original = '# personal comment\nmodel = "original" # keep comment\n[features]\nfoo = true\n'
        self.path.write_text(self.original)
        self.env = patch.dict(os.environ, {'CODEX_HOME': str(self.home), 'QPOOL_CONFIG': str(self.root / 'pools.json')})
        self.env.start()
        self.startup = patch('qp.desktop.install_startup')
        self.startup.start()
        self.cleanup = patch('qp.desktop.remove_startup')
        self.cleanup.start()
        self.native = SimpleNamespace(root=self.root, active={'id':'route1','allow_api':True},
                                      grant='synthetic-local-token-0123456789abcdef', port=8765,
                                      db_path=self.root/'ledger.db', desktop_error=None)

    def tearDown(self):
        self.env.stop()
        self.startup.stop()
        self.cleanup.stop()
        self.tmp.cleanup()

    def test_connect_preserves_other_values_and_never_embeds_tokens(self):
        result = desktop.connect(self.native)
        self.assertTrue(result['connected'])
        raw=self.path.read_text();doc=tomlkit.parse(raw)
        self.assertEqual('qpool-auto',doc['model'])
        self.assertTrue(doc['features']['foo'])
        self.assertIn('# personal comment',raw)
        self.assertNotIn(self.native.grant,raw)
        provider=doc['model_providers']['qpool-desktop']
        self.assertTrue(provider['requires_openai_auth'])
        self.assertNotIn('auth',provider)
        self.assertTrue(desktop.authorized(self.native, provider['http_headers']))
        self.assertFalse(desktop.authorized(self.native, {'Authorization':'Bearer native-oauth'}))
        self.assertEqual(0o600,self.path.stat().st_mode&0o777)
        self.assertEqual(self.original,(self.root/'codex-config.before-desktop.toml').read_text())
        desktop.connect(self.native) # Idempotent, original backup remains intact.
        desktop.restore(self.native)
        self.assertEqual(self.original,self.path.read_text())
        self.assertFalse((self.root/'desktop-route.json').exists())
        self.assertFalse(desktop.authorized(self.native, provider['http_headers']))

    def test_foreign_builtin_endpoint_rejected_without_partial_connection(self):
        original='openai_base_url="https://existing.example/v1"\n'+self.original
        self.path.write_text(original)
        with patch('qp.desktop.install_startup') as startup:
            with self.assertRaises(CollectorError):desktop.connect(self.native)
            startup.assert_not_called()
        self.assertEqual(original,self.path.read_text())
        self.assertFalse((self.root/'desktop.json').exists())
        self.assertFalse((self.root/'desktop-key').exists())

    def test_legacy_endpoint_is_managed_and_removed_on_restore(self):
        desktop.connect(self.native)
        self.assertEqual('http://127.0.0.1:8765/legacy/v1',tomlkit.parse(self.path.read_text())['openai_base_url'])
        self.assertTrue(desktop.read_state(self.root)['legacy_enabled'])
        desktop.restore(self.native)
        self.assertNotIn('openai_base_url',tomlkit.parse(self.path.read_text()))

    def test_upgrade_keeps_original_backup_and_user_effort(self):
        desktop.connect(self.native)
        state=desktop.read_state(self.root)
        state['provider'].pop('requires_openai_auth')
        state['provider'].pop('http_headers')
        state['provider']['auth']={'command':'old-helper'}
        (self.root/'desktop.json').write_text(json.dumps(state))
        doc=tomlkit.parse(self.path.read_text())
        doc['model_providers']['qpool-desktop']=state['provider']
        doc['model_reasoning_effort']='high'
        self.path.write_text(tomlkit.dumps(doc))
        desktop.connect(self.native)
        provider=tomlkit.parse(self.path.read_text())['model_providers']['qpool-desktop']
        self.assertTrue(provider['requires_openai_auth'])
        self.assertNotIn('auth',provider)
        self.assertEqual(self.original,(self.root/'codex-config.before-desktop.toml').read_text())
        self.assertEqual('high',tomlkit.parse(self.path.read_text())['model_reasoning_effort'])

    def test_startup_failure_rolls_back_config_and_authorization(self):
        with patch('qp.desktop.install_startup',side_effect=CollectorError('cannot start')):
            with self.assertRaises(CollectorError):desktop.connect(self.native)
        self.assertEqual(self.original,self.path.read_text())
        self.assertFalse((self.root/'desktop.json').exists())
        self.assertFalse((self.root/'desktop-key').exists())

    def test_earliest_provider_upgrade_preserves_unmanaged_target(self):
        desktop.connect(self.native)
        state=desktop.read_state(self.root)
        state.pop('provider_id')
        state['provider']={'name':'old','auth':{'command':'old-helper'}}
        state['managed']['model_provider']='qpool'
        (self.root/'desktop.json').write_text(json.dumps(state))
        doc=tomlkit.parse(self.path.read_text())
        doc['model_provider']='qpool'
        doc['model_providers']['qpool']=state['provider']
        doc['model_providers']['qpool-desktop']={'name':'user-custom'}
        self.path.write_text(tomlkit.dumps(doc))
        current=self.path.read_text()
        with self.assertRaises(CollectorError):desktop.connect(self.native)
        self.assertEqual(current,self.path.read_text())
        del doc['model_providers']['qpool-desktop']
        self.path.write_text(tomlkit.dumps(doc))
        desktop.connect(self.native)
        self.assertNotIn('qpool',tomlkit.parse(self.path.read_text())['model_providers'])
        desktop.restore(self.native)
        self.assertEqual(self.original,self.path.read_text())

    def test_launchagent_install_failure_removes_file(self):
        desktop.connect(self.native)
        state=desktop.read_state(self.root)
        self.startup.stop()
        path=self.root/'LaunchAgents'/'test.plist'
        with patch('qp.desktop.startup_path',return_value=(path,'test')),patch(
                'qp.desktop.subprocess.run',return_value=SimpleNamespace(returncode=1)):
            with self.assertRaises(CollectorError):desktop.install_startup(self.root,state)
        self.assertFalse(path.exists())

    def test_launchagent_program_contains_no_credentials_and_is_idempotent(self):
        import plistlib
        desktop.connect(self.native)
        state=desktop.read_state(self.root)
        self.startup.stop()
        path=self.root/'LaunchAgents'/'test.plist'
        with patch('qp.desktop.startup_path',return_value=(path,'test')),patch(
                'qp.desktop.subprocess.run',side_effect=[SimpleNamespace(returncode=1),
                 SimpleNamespace(returncode=0),SimpleNamespace(returncode=0)]) as run:
            desktop.install_startup(self.root,state)
            desktop.install_startup(self.root,state)
        d=plistlib.loads(path.read_bytes())
        self.assertEqual([state['python'],str(self.root/'desktop-service.py')],d['ProgramArguments'])
        self.assertNotIn(self.native.grant,path.read_text())
        self.assertEqual(3,run.call_count)

    def test_restore_preserves_unrelated_later_edits(self):
        desktop.connect(self.native)
        self.path.write_text(self.path.read_text()+'\n[projects.test]\ntrust_level="trusted"\n')
        desktop.restore(self.native)
        doc=tomlkit.parse(self.path.read_text())
        self.assertEqual('original',doc['model'])
        self.assertEqual('trusted',doc['projects']['test']['trust_level'])
        self.assertNotIn('qpool-desktop',doc.get('model_providers',{}))

    def test_chat_recovery_failure_keeps_provider_and_authorization(self):
        desktop.connect(self.native)
        original=self.path.read_text()
        with patch('qp.codex_recovery.recover',side_effect=CollectorError('chat recovery failed')):
            with self.assertRaises(CollectorError):desktop.restore(self.native)
        self.assertEqual(original,self.path.read_text())
        self.assertTrue(desktop.status(self.native)['connected'])
        self.assertTrue((self.root/'desktop-key').exists())

    def test_conflicting_external_model_edit_not_overwritten(self):
        desktop.connect(self.native)
        doc=tomlkit.parse(self.path.read_text());doc['model']='changed-by-user'
        self.path.write_text(tomlkit.dumps(doc))
        with self.assertRaises(CollectorError):desktop.restore(self.native)
        self.assertEqual('changed-by-user',tomlkit.parse(self.path.read_text())['model'])

    def test_legacy_recovery_preserves_profiles_and_publishes_explicit_resume(self):
        from qp.codex_recovery import RecoveryResult
        name = 'personal-profile'
        self.path.write_text(self.original + '\n[profiles.' + name + ']\nmodel="personal"\n')
        desktop.connect(self.native)
        with patch('qp.codex_recovery.recover', return_value=RecoveryResult(['old-id'], ['old-id'])):
            result = desktop.restore(self.native)
        doc = tomlkit.parse(self.path.read_text())
        self.assertEqual('personal', doc['profiles'][name]['model'])
        self.assertEqual([name], list(doc['profiles']))
        self.assertEqual('original', doc['model'])
        import shlex
        command = shlex.split(result['legacy_resume_command'])
        self.assertEqual('CODEX_HOME=' + str(self.home.resolve()), command[1])
        self.assertEqual(['-c', 'model_provider="qpool-desktop"', 'resume', '--all', '--include-non-interactive'], command[-5:])
        self.assertEqual(1, result['legacy_recovered_count'])
        self.assertEqual('qpool-desktop', result['compatibility_provider'])
        self.assertNotIn('http_headers', doc['model_providers']['qpool-desktop'])

    def test_recovery_report_and_stop_failures_keep_configuration_retryable(self):
        from qp.codex_recovery import RecoveryResult
        from qp.native import atomic
        desktop.connect(self.native)
        current = self.path.read_text()
        previous_report = '{"resume_command":"previous", "thread_ids": []}'
        report = self.root/'desktop-recovery.json'
        report.write_text(previous_report)
        def fail_report(path, *args, **kwargs):
            if Path(path) == report:
                raise PermissionError('report write failed')
            return atomic(path, *args, **kwargs)
        for failure in (patch('qp.native.atomic', side_effect=fail_report),
                        patch('qp.desktop.remove_startup', side_effect=CollectorError('stop failed'))):
            with failure, patch('qp.codex_recovery.recover',
                    return_value=RecoveryResult(['old-id'], ['old-id'])):
                with self.assertRaises((PermissionError, CollectorError)):
                    desktop.restore(self.native)
            self.assertEqual(current, self.path.read_text())
            self.assertEqual(previous_report, report.read_text())
            self.assertTrue(desktop.status(self.native)['connected'])
            self.assertTrue((self.root/'desktop-key').exists())
            self.assertTrue((self.root/'desktop-route.json').exists())

    def test_user_effort_change_does_not_disconnect_or_block_restore(self):
        desktop.connect(self.native)
        doc=tomlkit.parse(self.path.read_text());doc['model_reasoning_effort']='high'
        self.path.write_text(tomlkit.dumps(doc))
        self.assertTrue(desktop.status(self.native)['connected'])
        desktop.restore(self.native)
        restored=tomlkit.parse(self.path.read_text())
        self.assertEqual('high', restored['model_reasoning_effort'])
        self.assertEqual('original', restored['model'])

    def test_existing_provider_and_profile_are_not_overwritten(self):
        for extra in ('\n[model_providers.qpool-desktop]\nname="external"\n',):
            self.path.write_text(self.original+extra)
            with self.assertRaises(CollectorError):desktop.connect(self.native)
            self.assertEqual(self.original+extra,self.path.read_text())
        self.path.write_text('profile="work"\n'+self.original)
        with self.assertRaises(CollectorError):desktop.connect(self.native)

    def test_desktop_auth_does_not_merge_into_existing_cli_provider(self):
        self.path.write_text(self.original+'\n[model_providers.qpool]\nname="CLI"\nenv_key="QPOOL_ACCESS_TOKEN"\n')
        desktop.connect(self.native)
        doc=tomlkit.parse(self.path.read_text())
        self.assertEqual('qpool-desktop',doc['model_provider'])
        self.assertNotIn('auth',doc['model_providers']['qpool'])
        self.assertNotIn('env_key',doc['model_providers']['qpool-desktop'])
        desktop.restore(self.native)
        self.assertEqual('QPOOL_ACCESS_TOKEN',tomlkit.parse(self.path.read_text())['model_providers']['qpool']['env_key'])

    def test_failed_config_write_cannot_enable_restart(self):
        from qp.native import atomic
        def fail(path,*args,**kwargs):
            if Path(path)==self.path:raise PermissionError()
            return atomic(path,*args,**kwargs)
        with patch('qp.native.atomic',side_effect=fail):
            with self.assertRaises(PermissionError):desktop.connect(self.native)
        self.assertEqual(self.original,self.path.read_text())
        self.assertFalse((self.root/'desktop-route.json').exists())
        self.assertFalse((self.root/'desktop.json').exists())

    def test_symlink_config_rejected(self):
        self.path.unlink();self.path.symlink_to(self.root/'target')
        with self.assertRaises(CollectorError):desktop.connect(self.native)
        self.assertFalse((self.root/'target').exists())

    def test_restart_requires_identical_route_port_and_config(self):
        desktop.connect(self.native)
        route=self.native.active.copy();grant=self.native.grant
        self.native.active=self.native.grant=None
        self.native.data=lambda:{'routes':[route]}
        self.native.catalog=lambda r:None
        calls=[];self.native.write_entry=lambda:calls.append(True)
        desktop.resume(self.native)
        self.assertEqual(grant,self.native.grant)
        self.assertEqual([True],calls)
        self.native.port=9999
        desktop.resume(self.native)
        self.assertIsNone(self.native.grant)
        self.assertIsNotNone(self.native.desktop_error)

    def test_disabled_route_is_never_reenabled_by_helper(self):
        desktop.connect(self.native)
        (self.root/'desktop-route.json').unlink()
        with patch('qp.desktop.subprocess.Popen') as process:
            with self.assertRaises(CollectorError):desktop.token(self.root)
            process.assert_not_called()

    def test_helper_reuses_live_service_and_reads_rotated_token(self):
        desktop.connect(self.native)
        (self.root/'entry.json').write_text(json.dumps({'token':'rotated-token'}))
        with patch('qp.desktop.health',return_value={'active':True}),patch('qp.desktop.subprocess.Popen') as process:
            self.assertEqual('rotated-token',desktop.token(self.root))
            process.assert_not_called()

    def test_wrong_service_and_inactive_service_do_not_spawn_or_return_token(self):
        desktop.connect(self.native)
        for action in (CollectorError('wrong service'), {'active':False}):
            kw={'side_effect':action} if isinstance(action,Exception) else {'return_value':action}
            with patch('qp.desktop.health',**kw),patch('qp.desktop.subprocess.Popen') as process:
                with self.assertRaises(CollectorError):desktop.token(self.root)
                process.assert_not_called()

    def test_helper_starts_with_pinned_runtime_without_shell(self):
        desktop.connect(self.native)
        (self.root/'entry.json').write_text(json.dumps({'token':'restart-token'}))
        with patch('qp.desktop.health',side_effect=[ConnectionRefusedError(),{'active':True}]),patch('qp.desktop.subprocess.Popen') as process:
            self.assertEqual('restart-token',desktop.token(self.root))
            args,kw=process.call_args
            self.assertEqual(['-m','qp','web','--port','8765'],args[0][1:])
            self.assertEqual(str((self.root/'ledger.db').resolve()),kw['env']['QPOOL_DB'])
            self.assertTrue(kw['start_new_session'])
            self.assertNotIn('shell',kw)


if __name__=='__main__':unittest.main()
