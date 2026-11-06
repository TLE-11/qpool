"""Isolation and route-evidence gates for the desktop QA entry."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import tomlkit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import isolated_desktop_qa as qa
from isolated_qa_runtime import configure_home


class IsolatedDesktopQATests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name).resolve()
        self.root = self.repo / '.qpool/qa'
        self.root.mkdir(parents=True)
        self.addCleanup(patch.stopall)
        patch.object(qa, 'REPO', self.repo).start()
        (self.root / 'runtime.json').write_text('{"port":58289}')
        native = SimpleNamespace(root=self.root / 'native')
        configure_home(native, self.root / 'codex-home', 58289)
        self.source = self.repo / 'official'
        self.source.mkdir()
        (self.source / 'config.toml').write_text('model="gpt-6.1-sol"\n')
        (self.source / 'auth.json').write_text(json.dumps({
            'last_refresh': '2026-11-06T00:00:00Z',
            'tokens': {'access_token': 'synthetic-access', 'refresh_token': 'never-copy-me',
                       'account_id': 'synthetic-subject', 'id_token': 'synthetic-id'}}))
        self.history = self.source / 'sessions/2026/10/31/rollout-old.jsonl'
        self.history.parent.mkdir(parents=True)
        self.history.write_text('{"synthetic":"old history"}\n')
        with sqlite3.connect(self.source / 'state_5.sqlite') as db:
            db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT)')
            db.execute('INSERT INTO threads VALUES (?,?)', (qa.TEMP_CHAT, str(self.history)))
        with sqlite3.connect(native.root / 'sessions.db') as db:
            db.execute('CREATE TABLE requests (id,session,account,model,status,started)')

    def test_gui_home_has_native_oauth_and_both_entry_points_without_environment_key(self):
        result = qa.prepare(self.root, self.source)
        home = Path(result['home'])
        doc = tomlkit.parse((home / 'config.toml').read_text())
        self.assertEqual('openai', doc['model_provider'])
        self.assertNotIn('model_providers', doc)
        self.assertEqual('http://127.0.0.1:58289/legacy/v1', doc['openai_base_url'])
        copied = json.loads((home / 'auth.json').read_text())
        self.assertEqual('', copied['tokens']['refresh_token'])
        self.assertEqual('2026-11-06T00:00:00Z', copied['last_refresh'])
        self.assertEqual(0o600, (home / 'auth.json').stat().st_mode & 0o777)
        self.assertEqual(self.history.read_bytes(), (home / self.history.relative_to(self.source)).read_bytes())
        self.assertEqual([], list((self.root / 'desktop-user-data').iterdir()))
        self.assertNotIn('synthetic-access', result['launch_command'])
        self.assertNotIn('never-copy-me', result['launch_command'])
        with self.assertRaises(ValueError):
            qa.prepare(self.root, self.source)

    def test_check_requires_recent_receipt_for_exact_chat_and_never_claims_gui_pass(self):
        qa.prepare(self.root, self.source)
        state = json.loads((self.root / 'desktop-qa.json').read_text())
        with sqlite3.connect(self.root / 'native/sessions.db') as db:
            db.executemany('INSERT INTO requests VALUES (?,?,?,?,?,?)', [
                ('earlier', state['session'], 'Plus', 'sol', 'completed', state['created'] - 1),
                ('other', 'another-chat', 'Plus', 'sol', 'completed', state['created'] + 1)])
        self.assertFalse(qa.check(self.root)['gateway_observed'])
        with sqlite3.connect(self.root / 'native/sessions.db') as db:
            db.execute('INSERT INTO requests VALUES (?,?,?,?,?,?)',
                       ('actual', state['session'], 'Plus', 'sol', 'completed', state['created'] + 1))
        result = qa.check(self.root)
        self.assertTrue(result['gateway_observed'])
        self.assertEqual(['actual'], [row['id'] for row in result['gateway_receipts']])
        self.assertEqual('requires_desktop_browser_observation', result['full_gui_regression'])
        self.assertTrue(result['originals_unchanged'])
        self.history.write_text('changed by another process')
        self.assertFalse(qa.check(self.root)['originals_unchanged'])

    def test_unsupported_transport_and_outside_runtime_leave_source_untouched(self):
        with self.assertRaises(ValueError):
            qa.prepare(self.repo, self.source)
        path = self.root / 'codex-home/config.toml'
        path.write_text('model_provider="broken"\n')
        with self.assertRaises(ValueError):
            qa.prepare(self.root, self.source)
        self.assertFalse((self.root / 'desktop-home').exists())
        self.assertIn('never-copy-me', (self.source / 'auth.json').read_text())

    def test_wrong_default_endpoint_or_external_auth_cannot_escape_isolation(self):
        path = self.root / 'codex-home/config.toml'
        original = path.read_text()
        for change in ({'openai_base_url': 'http://127.0.0.1:8765/v1'},
                       {'openai_base_url': 'https://example.invalid/v1'},
                       {'model_provider': 'qpool-other'},
                       {'model_providers': {'openai': {'base_url': 'https://example.invalid'}}}):
            with self.subTest(change=change):
                doc = tomlkit.parse(original)
                doc.update(change)
                path.write_text(tomlkit.dumps(doc))
                with self.assertRaises(ValueError):
                    qa.prepare(self.root, self.source)
                self.assertFalse((self.root / 'desktop-home').exists())


if __name__ == '__main__':
    unittest.main()
