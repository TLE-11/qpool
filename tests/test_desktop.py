"""Desktop config ownership, restart grants, and credential helper boundaries."""
import json
import os
from pathlib import Path
import tempfile
import threading
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import tomlkit

from qp import desktop
from qp.collectors.base import CollectorError


class DesktopTests(unittest.TestCase):
    """Compatibility coverage for previously installed custom providers."""
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
        # The real Native constructor always creates all three ownership tables.
        with sqlite3.connect(self.root / 'sessions.db') as db:
            db.execute('CREATE TABLE requests (session,model,started)')
            db.execute('CREATE TABLE session_bindings (session,binding)')
            db.execute('CREATE TABLE legacy_bindings (session,binding)')

    def tearDown(self):
        self.env.stop()
        self.startup.stop()
        self.cleanup.stop()
        self.tmp.cleanup()


class BuiltinDesktopTests(unittest.TestCase):
    setUp = DesktopTests.setUp
    tearDown = DesktopTests.tearDown

    def test_connect_retains_openai_identity_without_defining_reserved_provider(self):
        result = desktop.connect(self.native)
        doc = tomlkit.parse(self.path.read_text())
        self.assertTrue(result['connected'])
        self.assertFalse(result['needs_upgrade'])
        self.assertEqual('openai', doc['model_provider'])
        self.assertEqual('qpool-auto', doc['model'])
        self.assertEqual('http://127.0.0.1:8765/legacy/v1', doc['openai_base_url'])
        self.assertNotIn('model_providers', doc)
        self.assertFalse((self.root / 'desktop-key').exists())
        self.assertTrue(doc['features']['foo'])
        desktop.connect(self.native)
        self.assertEqual(self.original, (self.root / 'codex-config.before-desktop.toml').read_text())

    def test_restore_builtin_does_not_recover_or_modify_any_chat(self):
        desktop.connect(self.native)
        self.path.write_text(self.path.read_text() + '\n[projects.test]\ntrust_level="trusted"\n')
        with patch('qp.codex_recovery.recover', return_value=[]) as recover:
            result = desktop.restore(self.native)
            self.assertEqual({}, recover.call_args.kwargs['managed_sessions'])
            self.assertTrue(recover.call_args.kwargs['require_owned_aliases'])
        doc = tomlkit.parse(self.path.read_text())
        self.assertEqual('original', doc['model'])
        self.assertNotIn('openai_base_url', doc)
        self.assertNotIn('model_providers', doc)
        self.assertEqual('trusted', doc['projects']['test']['trust_level'])
        self.assertFalse(result['connected'])

    def test_failed_install_rolls_back_builtin_transport(self):
        with patch('qp.desktop.install_startup', side_effect=CollectorError('failed')):
            with self.assertRaises(CollectorError):
                desktop.connect(self.native)
        self.assertEqual(self.original, self.path.read_text())
        self.assertFalse((self.root / 'desktop.json').exists())
        self.assertFalse((self.root / 'desktop-route.json').exists())

    def test_foreign_provider_or_endpoint_is_not_silently_replaced(self):
        for prefix in ('model_provider="external"\n', 'openai_base_url="https://example.invalid/v1"\n',
                       '[model_providers.openai]\nbase_url="https://example.invalid"\n'):
            self.path.write_text(prefix + self.original)
            before = self.path.read_text()
            with self.assertRaises(CollectorError):
                desktop.connect(self.native)
            self.assertEqual(before, self.path.read_text())
            self.assertFalse((self.root / 'desktop.json').exists())

    def test_existing_custom_connection_requires_explicit_restore_before_new_transport(self):
        desktop._connect_custom_provider(self.native)
        before = self.path.read_text()
        with self.assertRaisesRegex(CollectorError, '恢复官方配置'):
            desktop.connect(self.native)
        self.assertEqual(before, self.path.read_text())

    def test_restore_limits_alias_recovery_to_ledger_sessions_and_keeps_config_on_failure(self):
        desktop.connect(self.native)
        with sqlite3.connect(self.root / 'sessions.db') as db:
            db.executemany('INSERT INTO requests VALUES (?,?,?)',
                           [('known', 'old-model', 1), ('known', 'last-model', 2)])
        before = self.path.read_text()
        with patch('qp.codex_recovery.recover', side_effect=CollectorError('active writer')) as recover:
            with self.assertRaises(CollectorError):
                desktop.restore(self.native)
            self.assertEqual({'known': 'last-model'}, recover.call_args.kwargs['managed_sessions'])
        self.assertEqual(before, self.path.read_text())
        self.assertTrue((self.root / 'desktop.json').exists())

    def test_restore_includes_both_binding_tables_and_prefers_current_binding(self):
        desktop.connect(self.native)
        with sqlite3.connect(self.root / 'sessions.db') as db:
            db.execute('INSERT INTO requests VALUES (?,?,?)', ('native', 'old-model', 1))
            db.execute('INSERT INTO session_bindings VALUES (?,?)',
                       ('dedicated', json.dumps({'channel': {'model': 'selected-without-receipt'}})))
            db.execute('INSERT INTO legacy_bindings VALUES (?,?)',
                       ('native', json.dumps({'channel': {'model': 'newer-binding-model'}})))
        with patch('qp.codex_recovery.recover', return_value=[]) as recover:
            desktop.restore(self.native)
        self.assertEqual({'dedicated': 'selected-without-receipt', 'native': 'newer-binding-model'},
                         recover.call_args.kwargs['managed_sessions'])
        self.assertEqual(self.original, self.path.read_text())

    def test_unrecoverable_binding_preserves_entry_and_does_not_guess_model(self):
        desktop.connect(self.native)
        before = self.path.read_text()
        for raw in (None, 'not-json', '{}', '{"channel":{"model":"qpool-auto"}}'):
            with self.subTest(binding=raw):
                with sqlite3.connect(self.root / 'sessions.db') as db:
                    db.execute('DELETE FROM legacy_bindings')
                    db.execute('INSERT INTO legacy_bindings VALUES (?,?)', ('known', raw))
                with patch('qp.codex_recovery.recover') as recover:
                    with self.assertRaisesRegex(CollectorError, '保留接入配置'):
                        desktop.restore(self.native)
                    recover.assert_not_called()
                self.assertEqual(before, self.path.read_text())
                self.assertTrue((self.root / 'desktop.json').exists())
                self.assertTrue((self.root / 'desktop-route.json').exists())

    def test_missing_or_incomplete_ledger_cannot_silently_remove_entry(self):
        desktop.connect(self.native)
        before = self.path.read_text()
        ledger = self.root / 'sessions.db'
        for missing in (True, False):
            ledger.unlink(missing_ok=True)
            if not missing:
                with sqlite3.connect(ledger) as db:
                    db.execute('CREATE TABLE requests (session,model,started)')
            with patch('qp.codex_recovery.recover') as recover:
                with self.assertRaises(CollectorError):
                    desktop.restore(self.native)
                recover.assert_not_called()
            self.assertEqual(before, self.path.read_text())
            self.assertTrue((self.root / 'desktop.json').exists())


class CustomDesktopCompatibilityTests(DesktopTests):
    """Old custom-provider test cases, kept separate from the new default."""

    def test_connect_preserves_other_values_and_never_embeds_tokens(self):
        result = desktop._connect_custom_provider(self.native)
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
        desktop._connect_custom_provider(self.native) # Idempotent, original backup remains intact.
        desktop.restore(self.native)
        self.assertEqual(self.original,self.path.read_text())
        self.assertFalse((self.root/'desktop-route.json').exists())
        self.assertFalse(desktop.authorized(self.native, provider['http_headers']))

    def test_foreign_builtin_endpoint_rejected_without_partial_connection(self):
        original='openai_base_url="https://existing.example/v1"\n'+self.original
        self.path.write_text(original)
        with patch('qp.desktop.install_startup') as startup:
            with self.assertRaises(CollectorError):desktop._connect_custom_provider(self.native)
            startup.assert_not_called()
        self.assertEqual(original,self.path.read_text())
        self.assertFalse((self.root/'desktop.json').exists())
        self.assertFalse((self.root/'desktop-key').exists())

    def test_legacy_endpoint_is_managed_and_removed_on_restore(self):
        desktop._connect_custom_provider(self.native)
        self.assertEqual('http://127.0.0.1:8765/legacy/v1',tomlkit.parse(self.path.read_text())['openai_base_url'])
        self.assertTrue(desktop.read_state(self.root)['legacy_enabled'])
        desktop.restore(self.native)
        self.assertNotIn('openai_base_url',tomlkit.parse(self.path.read_text()))

    def test_upgrade_keeps_original_backup_and_user_effort(self):
        desktop._connect_custom_provider(self.native)
        state=desktop.read_state(self.root)
        state['provider'].pop('requires_openai_auth')
        state['provider'].pop('http_headers')
        state['provider']['auth']={'command':'old-helper'}
        (self.root/'desktop.json').write_text(json.dumps(state))
        doc=tomlkit.parse(self.path.read_text())
        doc['model_providers']['qpool-desktop']=state['provider']
        doc['model_reasoning_effort']='high'
        self.path.write_text(tomlkit.dumps(doc))
        desktop._connect_custom_provider(self.native)
        provider=tomlkit.parse(self.path.read_text())['model_providers']['qpool-desktop']
        self.assertTrue(provider['requires_openai_auth'])
        self.assertNotIn('auth',provider)
        self.assertEqual(self.original,(self.root/'codex-config.before-desktop.toml').read_text())
        self.assertEqual('high',tomlkit.parse(self.path.read_text())['model_reasoning_effort'])

    def test_startup_failure_rolls_back_config_and_authorization(self):
        with patch('qp.desktop.install_startup',side_effect=CollectorError('cannot start')):
            with self.assertRaises(CollectorError):desktop._connect_custom_provider(self.native)
        self.assertEqual(self.original,self.path.read_text())
        self.assertFalse((self.root/'desktop.json').exists())
        self.assertFalse((self.root/'desktop-key').exists())

    def test_earliest_provider_upgrade_preserves_unmanaged_target(self):
        desktop._connect_custom_provider(self.native)
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
        with self.assertRaises(CollectorError):desktop._connect_custom_provider(self.native)
        self.assertEqual(current,self.path.read_text())
        del doc['model_providers']['qpool-desktop']
        self.path.write_text(tomlkit.dumps(doc))
        desktop._connect_custom_provider(self.native)
        self.assertNotIn('qpool',tomlkit.parse(self.path.read_text())['model_providers'])
        desktop.restore(self.native)
        self.assertEqual(self.original,self.path.read_text())

    def test_launchagent_install_failure_removes_file(self):
        desktop._connect_custom_provider(self.native)
        state=desktop.read_state(self.root)
        self.startup.stop()
        path=self.root/'LaunchAgents'/'test.plist'
        with patch('qp.desktop.startup_path',return_value=(path,'test')),patch(
                'qp.desktop.subprocess.run',return_value=SimpleNamespace(returncode=1)):
            with self.assertRaises(CollectorError):desktop.install_startup(self.root,state)
        self.assertFalse(path.exists())

    def test_launchagent_program_contains_no_credentials_and_is_idempotent(self):
        import plistlib
        desktop._connect_custom_provider(self.native)
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
        desktop._connect_custom_provider(self.native)
        self.path.write_text(self.path.read_text()+'\n[projects.test]\ntrust_level="trusted"\n')
        desktop.restore(self.native)
        doc=tomlkit.parse(self.path.read_text())
        self.assertEqual('original',doc['model'])
        self.assertEqual('trusted',doc['projects']['test']['trust_level'])
        self.assertNotIn('qpool-desktop',doc.get('model_providers',{}))

    def test_chat_recovery_failure_keeps_provider_and_authorization(self):
        desktop._connect_custom_provider(self.native)
        original=self.path.read_text()
        with patch('qp.codex_recovery.recover',side_effect=CollectorError('chat recovery failed')):
            with self.assertRaises(CollectorError):desktop.restore(self.native)
        self.assertEqual(original,self.path.read_text())
        self.assertTrue(desktop.status(self.native)['connected'])
        self.assertTrue((self.root/'desktop-key').exists())

    def test_restore_retains_only_official_transport_for_provider_pinned_chats(self):
        from qp.codex_recovery import RecoveryResult
        desktop._connect_custom_provider(self.native)
        with patch('qp.codex_recovery.recover',return_value=RecoveryResult(['synthetic'],True)):
            result=desktop.restore(self.native)
        doc=tomlkit.parse(self.path.read_text())
        self.assertEqual('original',doc['model'])
        compat=doc['model_providers']['qpool-desktop']
        self.assertEqual('https://chatgpt.com/backend-api/codex',compat['base_url'])
        self.assertTrue(compat['requires_openai_auth'])
        self.assertNotIn('http_headers',compat)
        self.assertNotIn('127.0.0.1',tomlkit.dumps(compat))
        self.assertEqual('qpool-desktop',result['compatibility_provider'])
        self.assertFalse((self.root/'desktop-key').exists())
        desktop._connect_custom_provider(self.native)
        state=desktop.read_state(self.root)
        self.assertNotEqual('qpool-desktop',state['provider_id'])
        doc=tomlkit.parse(self.path.read_text())
        self.assertEqual(compat,doc['model_providers']['qpool-desktop'])
        self.assertIn('127.0.0.1',doc['model_providers'][state['provider_id']]['base_url'])

    def test_conflicting_external_model_edit_not_overwritten(self):
        desktop._connect_custom_provider(self.native)
        doc=tomlkit.parse(self.path.read_text());doc['model']='changed-by-user'
        self.path.write_text(tomlkit.dumps(doc))
        with self.assertRaises(CollectorError):desktop.restore(self.native)
        self.assertEqual('changed-by-user',tomlkit.parse(self.path.read_text())['model'])

    def test_user_effort_change_does_not_disconnect_or_block_restore(self):
        desktop._connect_custom_provider(self.native)
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
            with self.assertRaises(CollectorError):desktop._connect_custom_provider(self.native)
            self.assertEqual(self.original+extra,self.path.read_text())
        self.path.write_text('profile="work"\n'+self.original)
        with self.assertRaises(CollectorError):desktop._connect_custom_provider(self.native)

    def test_desktop_auth_does_not_merge_into_existing_cli_provider(self):
        self.path.write_text(self.original+'\n[model_providers.qpool]\nname="CLI"\nenv_key="QPOOL_ACCESS_TOKEN"\n')
        desktop._connect_custom_provider(self.native)
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
            with self.assertRaises(PermissionError):desktop._connect_custom_provider(self.native)
        self.assertEqual(self.original,self.path.read_text())
        self.assertFalse((self.root/'desktop-route.json').exists())
        self.assertFalse((self.root/'desktop.json').exists())

    def test_symlink_config_rejected(self):
        self.path.unlink();self.path.symlink_to(self.root/'target')
        with self.assertRaises(CollectorError):desktop._connect_custom_provider(self.native)
        self.assertFalse((self.root/'target').exists())

    def test_restart_requires_identical_route_port_and_config(self):
        desktop._connect_custom_provider(self.native)
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
        desktop._connect_custom_provider(self.native)
        (self.root/'desktop-route.json').unlink()
        with patch('qp.desktop.subprocess.Popen') as process:
            with self.assertRaises(CollectorError):desktop.token(self.root)
            process.assert_not_called()

    def test_helper_reuses_live_service_and_reads_rotated_token(self):
        desktop._connect_custom_provider(self.native)
        (self.root/'entry.json').write_text(json.dumps({'token':'rotated-token'}))
        with patch('qp.desktop.health',return_value={'active':True}),patch('qp.desktop.subprocess.Popen') as process:
            self.assertEqual('rotated-token',desktop.token(self.root))
            process.assert_not_called()

    def test_wrong_service_and_inactive_service_do_not_spawn_or_return_token(self):
        desktop._connect_custom_provider(self.native)
        for action in (CollectorError('wrong service'), {'active':False}):
            kw={'side_effect':action} if isinstance(action,Exception) else {'return_value':action}
            with patch('qp.desktop.health',**kw),patch('qp.desktop.subprocess.Popen') as process:
                with self.assertRaises(CollectorError):desktop.token(self.root)
                process.assert_not_called()

    def test_helper_starts_with_pinned_runtime_without_shell(self):
        desktop._connect_custom_provider(self.native)
        (self.root/'entry.json').write_text(json.dumps({'token':'restart-token'}))
        with patch('qp.desktop.health',side_effect=[ConnectionRefusedError(),{'active':True}]),patch('qp.desktop.subprocess.Popen') as process:
            self.assertEqual('restart-token',desktop.token(self.root))
            args,kw=process.call_args
            self.assertEqual(['-m','qp','web','--port','8765'],args[0][1:])
            self.assertEqual(str((self.root/'ledger.db').resolve()),kw['env']['QPOOL_DB'])
            self.assertTrue(kw['start_new_session'])
            self.assertNotIn('shell',kw)


if __name__=='__main__':unittest.main()
