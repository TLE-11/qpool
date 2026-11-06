"""Real protocol check in a temporary Codex home, with no model requests."""
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import unittest
import hashlib
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from qp.codex_recovery import recover
from qp.collectors.base import CollectorError

BINARY = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex'


class Client:
    def __init__(self, official=False):
        command=[BINARY, '--no-daemon', 'app-server', '--listen', 'stdio://']
        if official:command+=['-c','model_provider="openai"']
        self.p = subprocess.Popen(command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.events = queue.Queue();self.ident = 0
        def read():
            for line in self.p.stdout:
                try:self.events.put(json.loads(line))
                except ValueError:pass
        threading.Thread(target=read, daemon=True).start()
        self.call('initialize', {'clientInfo': {'name': 'qpool_test', 'version': '1'},
                                'capabilities': {'experimentalApi': True}})
    def call(self, method, params):
        self.ident += 1
        self.p.stdin.write(json.dumps({'id': self.ident, 'method': method, 'params': params})+'\n')
        self.p.stdin.flush()
        while True:
            result = self.events.get(timeout=30)
            if result.get('id') == self.ident:
                if 'error' in result:raise AssertionError(result['error'])
                return result['result']
    def close(self):
        self.p.terminate();self.p.wait(timeout=10)
        self.p.stdin.close();self.p.stdout.close()


@unittest.skipUnless(Path(BINARY).is_file(), 'requires the installed Codex protocol engine')
class RecoveryProtocolTest(unittest.TestCase):
    def test_binding_without_any_receipt_restores_same_alias_chat_through_product(self):
        from qp import desktop
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, CODEX_HOME=folder):
            home = Path(folder)
            config = home / 'config.toml'
            config.write_text('model="qpool-auto"\nmodel_provider="openai"\n'
                              'openai_base_url="http://127.0.0.1:9/legacy/v1"\n')
            client = Client()
            try:
                thread = client.call('thread/start', {'ephemeral': False, 'cwd': folder,
                    'approvalPolicy': 'never', 'sandbox': 'read-only'})['thread']
            finally:
                client.close()
            history = Path(thread['path'])
            history.parent.mkdir(parents=True, exist_ok=True)
            meta = {'id': thread['id'], 'timestamp': '2026-11-06T00:00:00Z', 'cwd': folder,
                    'originator': 'qpool_test', 'cli_version': '0.166.0', 'source': 'cli', 'model_provider': 'openai'}
            history.write_text(json.dumps({'timestamp': '2026-11-06T00:00:00Z',
                'type': 'session_meta', 'payload': meta}) + '\n' +
                json.dumps({'timestamp': '2026-11-06T00:00:00Z', 'type': 'event_msg', 'payload': {
                    'type': 'user_message', 'message': 'first turn failed before inference',
                    'images': [], 'local_images': [], 'text_elements': []}}) + '\n')
            client = Client()
            try:
                client.call('thread/resume', {'threadId': thread['id'], 'excludeTurns': True})
            finally:
                client.close()
            before_history = history.read_bytes()
            native_root = home / 'native'
            native_root.mkdir()
            backup = native_root / 'before.toml'
            official = 'model="gpt-6.1-sol"\nmodel_provider="openai"\n'
            backup.write_text(official)
            (native_root / 'desktop.json').write_text(json.dumps({
                'transport': 'builtin-openai', 'legacy_enabled': True, 'config_path': str(config),
                'backup_path': str(backup), 'provider_id': 'openai', 'provider': {},
                'managed': {'model': 'qpool-auto', 'model_provider': 'openai',
                            'openai_base_url': 'http://127.0.0.1:9/legacy/v1'}}))
            with sqlite3.connect(native_root / 'sessions.db') as db:
                db.execute('CREATE TABLE requests (session,model,started)')
                db.execute('CREATE TABLE legacy_bindings (session,binding)')
                db.execute('CREATE TABLE session_bindings (session,binding)')
            # Selection can fail even before a binding exists. The unknown alias
            # must block config removal instead of being silently abandoned.
            before_config = config.read_text()
            with patch('qp.desktop.remove_startup') as remove:
                with self.assertRaisesRegex(CollectorError, '保留接入配置'):
                    desktop.restore(SimpleNamespace(root=native_root, desktop_error=None))
                remove.assert_not_called()
            self.assertEqual(before_config, config.read_text())
            self.assertTrue((native_root / 'desktop.json').exists())
            self.assertEqual(before_history, history.read_bytes())
            with sqlite3.connect(native_root / 'sessions.db') as db:
                db.execute('INSERT INTO legacy_bindings VALUES (?,?)', (
                    hashlib.sha256(thread['id'].encode()).hexdigest()[:16],
                    json.dumps({'channel': {'model': 'gpt-6.1-sol'}})))
            with patch('qp.desktop.remove_startup'):
                desktop.restore(SimpleNamespace(root=native_root, desktop_error=None))
            self.assertEqual(official, config.read_text())
            self.assertFalse((native_root / 'desktop.json').exists())
            self.assertTrue(history.read_bytes().startswith(before_history))
            client = Client()
            try:
                result = client.call('thread/resume', {'threadId': thread['id'], 'excludeTurns': True})
                self.assertEqual('openai', result['modelProvider'])
                self.assertEqual('gpt-6.1-sol', result['model'])
            finally:
                client.close()

    def test_builtin_recovery_only_changes_known_aliases_and_preserves_other_chats(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, CODEX_HOME=folder):
            home = Path(folder)
            (home / 'config.toml').write_text('model="qpool-auto"\nmodel_provider="openai"\n')
            threads = []
            for model in ('qpool-auto', 'qpool-auto', 'gpt-6-astra'):
                client = Client()
                try:
                    thread = client.call('thread/start', {'ephemeral': False, 'cwd': folder,
                        'model': model, 'approvalPolicy': 'never', 'sandbox': 'read-only'})['thread']
                finally:
                    client.close()
                path = Path(thread['path'])
                path.parent.mkdir(parents=True, exist_ok=True)
                meta = {'id': thread['id'], 'timestamp': '2026-11-06T00:00:00Z', 'cwd': folder,
                        'originator': 'qpool_test', 'cli_version': '0.166.0', 'source': 'cli', 'model_provider': 'openai'}
                path.write_text(json.dumps({'timestamp': '2026-11-06T00:00:00Z', 'type': 'session_meta', 'payload': meta})+'\n'+
                    json.dumps({'timestamp': '2026-11-06T00:00:00Z', 'type': 'event_msg',
                                'payload': {'type': 'user_message', 'message': 'synthetic history',
                                            'images': [], 'local_images': [], 'text_elements': []}})+'\n')
                client = Client()
                try:
                    client.call('thread/resume', {'threadId': thread['id'], 'model': model, 'excludeTurns': True})
                finally:
                    client.close()
                threads.append(thread)
            before = [Path(t['path']).read_bytes() for t in threads]
            sid = lambda t: hashlib.sha256(t['id'].encode()).hexdigest()[:16]
            result = recover(home, 'openai', None, home / 'backup',
                             managed_sessions={sid(threads[0]): 'gpt-6.1-sol', sid(threads[2]): 'gpt-6.1-sol'})
            self.assertEqual([threads[0]['id']], result)
            self.assertTrue(Path(threads[0]['path']).read_bytes().startswith(before[0]))
            for index in (1, 2):
                self.assertEqual(before[index], Path(threads[index]['path']).read_bytes())
            client = Client()
            try:
                result = client.call('thread/resume', {'threadId': threads[0]['id'], 'excludeTurns': True})
                self.assertEqual('openai', result['modelProvider'])
                self.assertEqual('gpt-6.1-sol', result['model'])
            finally:
                client.close()

    def test_recover_same_chat_id_and_history_without_model_inference(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, CODEX_HOME=folder):
            home = Path(folder)
            (home/'config.toml').write_text('model="gpt-6.1-sol"\nmodel_provider="qpool-desktop"\n'
                '[model_providers.qpool-desktop]\nname="test"\nbase_url="http://127.0.0.1:9/v1"\n'
                'wire_api="responses"\nrequires_openai_auth=false\nexperimental_bearer_token="synthetic"\n')
            client = Client()
            try:
                thread = client.call('thread/start', {'ephemeral': False, 'cwd': folder,
                    'approvalPolicy': 'never', 'sandbox': 'read-only'})['thread']
            finally:client.close()
            path = Path(thread['path']);path.parent.mkdir(parents=True, exist_ok=True)
            meta = {'id': thread['id'], 'timestamp': '2026-10-31T00:00:00Z', 'cwd': folder,
                    'originator': 'qpool_test', 'cli_version': '0.166.0', 'source': 'cli',
                    'model_provider': 'qpool-desktop'}
            events = [('session_meta', meta), ('event_msg', {'type': 'user_message',
                'message': 'synthetic history', 'images': [], 'local_images': [], 'text_elements': []})]
            path.write_text(''.join(json.dumps({'timestamp': '2026-10-31T00:00:00Z',
                'type': kind, 'payload': payload})+'\n' for kind, payload in events))
            history = path.read_bytes()
            client = Client()
            try:client.call('thread/resume', {'threadId': thread['id'], 'excludeTurns': True})
            finally:client.close()
            restored = recover(home, 'qpool-desktop', 'gpt-6.1-sol', home/'backup')
            self.assertEqual([thread['id']], restored)
            self.assertTrue(path.read_bytes().startswith(history))
            client = Client(official=not restored.compatibility_provider)
            try:
                result = client.call('thread/resume', {'threadId': thread['id'], 'excludeTurns': True})
                self.assertEqual('qpool-desktop' if restored.compatibility_provider else 'openai', result['modelProvider'])
                self.assertEqual('gpt-6.1-sol', result['model'])
            finally:client.close()

    def test_archived_guardian_keeps_model_history_and_archive_state(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, CODEX_HOME=folder):
            home=Path(folder)
            (home/'config.toml').write_text('model="codex-auto-review"\nmodel_provider="qpool-desktop"\n'
                '[model_providers.qpool-desktop]\nname="test"\nbase_url="http://127.0.0.1:9/v1"\n'
                'wire_api="responses"\nrequires_openai_auth=false\nexperimental_bearer_token="synthetic"\n')
            client=Client()
            try:
                thread=client.call('thread/start',{'ephemeral':False,'cwd':folder,
                    'model':'codex-auto-review','approvalPolicy':'never','sandbox':'read-only'})['thread']
            finally:client.close()
            path=Path(thread['path']);path.parent.mkdir(parents=True,exist_ok=True)
            meta={'id':thread['id'],'timestamp':'2026-11-06T00:00:00Z','cwd':folder,
                  'originator':'qpool_test','cli_version':'0.166.0','source':'cli','model_provider':'qpool-desktop'}
            path.write_text(json.dumps({'timestamp':'2026-11-06T00:00:00Z','type':'session_meta','payload':meta})+'\n'+
                json.dumps({'timestamp':'2026-11-06T00:00:00Z','type':'event_msg',
                            'payload':{'type':'user_message','message':'synthetic approval history',
                                       'images':[],'local_images':[],'text_elements':[]}})+'\n')
            history=path.read_bytes()
            client=Client()
            try:
                client.call('thread/resume',{'threadId':thread['id'],'model':'codex-auto-review','excludeTurns':True})
                client.call('thread/archive',{'threadId':thread['id']})
            finally:client.close()
            restored=recover(home,'qpool-desktop','gpt-6.1-sol',home/'backup')
            self.assertEqual([thread['id']],restored)
            target_provider='qpool-desktop' if restored.compatibility_provider else 'openai'
            client=Client(official=not restored.compatibility_provider)
            try:
                page=client.call('thread/list',{'archived':True,'modelProviders':[target_provider],
                    'sourceKinds':['cli'],'useStateDbOnly':True,'limit':100})
                self.assertIn(thread['id'],[r['id'] for r in page['data']])
                client.call('thread/unarchive',{'threadId':thread['id']})
                result=client.call('thread/resume',{'threadId':thread['id'],'excludeTurns':True})
                self.assertEqual('codex-auto-review',result['model'])
                self.assertEqual(target_provider,result['modelProvider'])
                self.assertTrue(Path(result['thread']['path']).read_bytes().startswith(history))
            finally:client.close()
