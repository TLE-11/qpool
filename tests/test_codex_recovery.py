"""Real protocol check in a temporary Codex home, with no model requests."""
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from qp.codex_recovery import recover

BINARY = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex'


class Client:
    def __init__(self):
        self.p = subprocess.Popen([BINARY, '--no-daemon', 'app-server', '--listen', 'stdio://'],
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
            client = Client()
            try:
                result = client.call('thread/resume', {'threadId': thread['id'], 'excludeTurns': True})
                self.assertEqual('openai', result['modelProvider'])
                self.assertEqual('gpt-6.1-sol', result['model'])
            finally:client.close()
