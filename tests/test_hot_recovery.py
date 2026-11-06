"""Real protocol check in a temporary Codex home, with no model requests."""
import json
import os
from pathlib import Path
import queue
import shlex
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace

import tomlkit
from qp import desktop
from qp.collectors.base import CollectorError
from unittest.mock import patch


BINARY = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex'


class Client:
    def __init__(self, resume_command=None):
        command = [BINARY, '--no-daemon', 'app-server', '--listen', 'stdio://']
        if resume_command:
            prefix = shlex.split(resume_command)
            command = prefix[:prefix.index('resume')] + ['app-server', '--listen', 'stdio://']
        self.p = subprocess.Popen(command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.events = queue.Queue();self.ident = 0
        def read():
            for line in self.p.stdout:
                try:self.events.put(json.loads(line))
                except ValueError:pass
        threading.Thread(target=read, daemon=True).start()
        try:
            self.call('initialize', {'clientInfo': {'name': 'qpool_test', 'version': '1'},
                                    'capabilities': {'experimentalApi': True}})
        except Exception:
            self.p.terminate();self.p.wait(timeout=10)
            detail = self.p.stderr.read()[-3000:]
            self.p.stdin.close();self.p.stdout.close();self.p.stderr.close()
            raise AssertionError(detail)
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
        self.p.stdin.close();self.p.stdout.close();self.p.stderr.close()


@unittest.skipUnless(Path(BINARY).is_file(), 'requires the installed Codex protocol engine')
class RecoveryProtocolTest(unittest.TestCase):
    def test_restore_final_config_preserves_chat_history_models_and_archive(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, CODEX_HOME=folder), \
                patch('qp.desktop.install_startup'), patch('qp.desktop.remove_startup'):
            home = Path(folder)
            root = home / 'qpool'
            root.mkdir()
            native = SimpleNamespace(root=root, active={'id': 'route'}, grant='synthetic-local-grant',
                                     port=9876, db_path=root/'ledger.db')
            (home/'config.toml').write_text('model="gpt-6.1-sol"\n')
            (root/'catalog.json').write_text(json.dumps({'models': [{
                'slug': 'qpool-auto', 'display_name': 'qpool auto', 'description': 'Synthetic test model',
                'default_reasoning_level': 'medium', 'supported_reasoning_levels': [],
                'shell_type': 'unified_exec', 'visibility': 'list', 'supported_in_api': True,
                'priority': 1, 'base_instructions': 'Synthetic protocol test; no inference.',
                'supports_reasoning_summaries': False, 'support_verbosity': False,
                'default_reasoning_summary': 'none', 'truncation_policy': {'mode': 'tokens', 'limit': 10000},
                'input_modalities': ['text'], 'experimental_supported_tools': [], 'context_window': 200000}]}))
            desktop._connect_custom_provider(native)
            threads = []
            # Include a hidden approval model: recovery must not replace it with a general model.
            for model, archived in [('qpool-auto', False), ('codex-auto-review', True)]:
                client = Client()
                try:
                    thread = client.call('thread/start', {'ephemeral': False, 'cwd': folder,
                        'model': model, 'approvalPolicy': 'never', 'sandbox': 'read-only'})['thread']
                finally:client.close()
                path = Path(thread['path']);path.parent.mkdir(parents=True, exist_ok=True)
                meta = {'id': thread['id'], 'timestamp': '2026-10-31T00:00:00Z', 'cwd': folder,
                        'originator': 'qpool_test', 'cli_version': '0.166.0', 'source': 'cli',
                        'model_provider': 'qpool-desktop'}
                events = [('session_meta', meta), ('event_msg', {'type': 'user_message',
                    'message': 'synthetic history ' + model, 'images': [], 'local_images': [], 'text_elements': []})]
                path.write_text(''.join(json.dumps({'timestamp': '2026-10-31T00:00:00Z',
                    'type': kind, 'payload': payload})+'\n' for kind, payload in events))
                history = path.read_bytes()
                client = Client()
                try:
                    client.call('thread/resume', {'threadId': thread['id'], 'model': model, 'excludeTurns': True})
                    if archived:
                        client.call('thread/archive', {'threadId': thread['id']})
                finally:client.close()
                threads.append((thread['id'], model, archived, history))
            connected = (home/'config.toml').read_text()
            with patch('qp.desktop.remove_startup', side_effect=CollectorError('stop failed')):
                with self.assertRaisesRegex(CollectorError, '部分聊天可能已恢复模型'):
                    desktop.restore(native)
            self.assertEqual(connected, (home/'config.toml').read_text())
            self.assertTrue((root/'desktop.json').exists())
            self.assertTrue((root/'desktop-key').exists())
            # Retry after a partial model conversion must complete without losing archived chats.
            restored = desktop.restore(native)
            self.assertFalse(restored['connected'])
            self.assertFalse((root/'desktop-key').exists())
            self.assertFalse((root/'desktop.json').exists())
            doc = tomlkit.parse((home/'config.toml').read_text())
            self.assertNotIn('openai_base_url', doc)
            self.assertNotIn('model_catalog_json', doc)
            self.assertEqual('gpt-6.1-sol', doc['model'])
            self.assertEqual('https://chatgpt.com/backend-api/codex',
                             doc['model_providers']['qpool-desktop']['base_url'])
            self.assertTrue(doc['model_providers']['qpool-desktop']['requires_openai_auth'])
            self.assertNotIn('http_headers', doc['model_providers']['qpool-desktop'])
            report = json.loads((root/'desktop-recovery.json').read_text())
            self.assertEqual({t[0] for t in threads}, set(report['thread_ids']))
            client = Client()
            try:
                self.assertEqual('openai', client.call('thread/start', {'ephemeral': True})['modelProvider'])
                with self.assertRaisesRegex(AssertionError, 'modelProviderMismatch'):
                    client.call('thread/resume', {'threadId': threads[0][0]})
            finally:client.close()
            with patch.dict(os.environ, CODEX_HOME=str(home/'wrong-home')):
                client = Client(report['resume_command'])
            try:
                for ident, model, archived, history in threads:
                    page = client.call('thread/list', {'modelProviders': ['qpool-desktop'],
                        'archived': archived, 'useStateDbOnly': True})
                    row = next(row for row in page['data'] if row['id'] == ident)
                    self.assertTrue(Path(row['path']).read_bytes().startswith(history))
                    if archived:
                        client.call('thread/unarchive', {'threadId': ident})
                    result = client.call('thread/resume', {'threadId': ident})
                    self.assertEqual(ident, result['thread']['id'])
                    self.assertEqual('qpool-desktop', result['modelProvider'])
                    self.assertEqual('gpt-6.1-sol' if model == 'qpool-auto' else model, result['model'])
                    self.assertIn('synthetic history ' + model, json.dumps(result['thread']['turns']))
                    if archived:
                        client.call('thread/archive', {'threadId': ident})
            finally:client.close()
