"""Regression cases from the full-repository review, using isolated fixtures."""
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import test_native as native_fixture
import test_decisions as decision_fixture
import test_api_accounts as api_fixture
from qp import ledger, profiles
from qp.collectors.base import CollectorError
from qp.db import Database


class ImageTurnTests(unittest.TestCase):
    setUp = native_fixture.NativeTests.setUp
    tearDown = native_fixture.NativeTests.tearDown
    body = native_fixture.NativeTests.body
    request = native_fixture.NativeTests.request

    def test_image_only_new_turn_and_tool_continuation(self):
        cache = self.home / 'models_cache.json'
        models = json.loads(cache.read_text())
        for model in models['models']:
            model['input_modalities'] = ['text', 'image']
        cache.write_text(json.dumps(models))
        for mode in ('local', 'fixed', 'model'):
            with self.subTest(mode=mode):
                route = dict(self.route, mode=mode)
                if mode == 'fixed':
                    route['regular'] = []
                if mode == 'model':
                    route['decision'] = {'channel_id': self.small}
                self.native.save_route(route)
                self.native.enable(route['id'])
                headers = {'session_id': 'image-' + mode}
                body = self.body()
                body['input'][0]['content'] = [{'type': 'input_image',
                                              'image_url': 'data:image/png;base64,AA=='}]
                with patch.object(self.native.decision_engine, '_model') as judge:
                    self.assertEqual(200, self.request(body, headers=headers)[0])
                    self.assertEqual('gpt-test-large', self.requests[-1][1]['model'])
                    self.assertEqual(body['input'], self.requests[-1][1]['input'])
                    body['input'].append({'type': 'function_call_output', 'call_id': 'image-tool', 'output': 'done'})
                    self.assertEqual(200, self.request(body, headers=headers)[0])
                    self.assertEqual('gpt-test-large', self.requests[-1][1]['model'])
                    judge.assert_not_called()
                # A later text task remains a new turn, not an image continuation.
                body['input'].append({'role': 'user', 'content': '翻译 hello'})
                if mode == 'local':
                    self.assertEqual(200, self.request(body, headers=headers)[0])
                    self.assertEqual('gpt-test-small', self.requests[-1][1]['model'])

    def test_empty_user_content_and_unbound_tool_output_still_rejected(self):
        for content in ('', [], [{'type': 'input_image'}]):
            body = self.body()
            body['input'][0]['content'] = content
            self.assertEqual(502, self.request(body)[0])
        body = self.body()
        body['input'][0]['content'] = [{'type': 'input_image', 'image_url': 'data:image/png;base64,AA=='}]
        body['input'].append({'type': 'function_call_output', 'call_id': 'unbound', 'output': 'done'})
        self.assertEqual(502, self.request(body)[0])
        self.assertEqual([], self.requests)


class JudgeAvailabilityTests(unittest.TestCase):
    setUp = decision_fixture.DecisionTests.setUp
    tearDown = decision_fixture.DecisionTests.tearDown
    body = native_fixture.NativeTests.body
    request = native_fixture.NativeTests.request

    def test_removed_or_unavailable_judge_falls_back_with_audit_record(self):
        original = json.loads(profiles.config_path().read_text())
        for removed in (False, True):
            with self.subTest(removed=removed):
                raw = json.loads(json.dumps(original))
                if removed:
                    raw['channels'] = [c for c in raw['channels'] if c['id'] != self.small]
                else:
                    next(c for c in raw['channels'] if c['id'] == self.small)['available'] = False
                profiles.config_path().write_text(json.dumps(raw))
                status, _ = self.request(self.body('实现并发控制'), headers={'session_id': str(removed)})
                self.assertEqual(200, status)
                self.assertEqual('gpt-test-large', self.requests[-1][1]['model'])
                self.assertEqual([], self.judge_calls)
                logs = self.native.status()
                judgment = logs['decisions'][0]
                self.assertTrue(judgment['fallback'])
                self.assertEqual('unavailable', judgment['failure'])
                self.assertEqual(judgment['id'], logs['sessions'][0]['decision_id'])
                with self.assertRaises(CollectorError):
                    self.native.save_route(dict(self.route))


class PinnedAPITests(unittest.TestCase):
    setUp = api_fixture.APIAccountTests.setUp
    tearDown = api_fixture.APIAccountTests.tearDown
    body = api_fixture.APIAccountTests.body
    request = api_fixture.APIAccountTests.request
    connect_api = api_fixture.APIAccountTests.connect_api
    subscription_priority_fixture = api_fixture.APIAccountTests.subscription_priority_fixture
    use_real_api_adapter = api_fixture.APIAccountTests.use_real_api_adapter

    def test_bound_api_ignores_other_accounts_but_keeps_paid_permission(self):
        _, paid = self.subscription_priority_fixture()
        self.use_real_api_adapter()
        body = self.body()
        with patch('qp.native.pools.evaluate', return_value={'state': 'blocked'}):
            self.assertEqual(200, self.request(body)[0])
        self.native.bindings.clear()  # Exercise durable restoration as well.
        body['input'].append({'role': 'user', 'content': '实现并发算法'})
        with patch('qp.native.pools.evaluate') as evaluate, patch.object(self.native, 'refresh') as refresh:
            self.assertEqual(200, self.request(body)[0])
            evaluate.assert_not_called()
            refresh.assert_not_called()
        self.assertEqual('gpt-test-large', self.requests[-1][1]['model'])
        self.assertEqual('Bearer ' + api_fixture.KEY, self.requests[-1][2]['Authorization'])
        raw = json.loads(profiles.config_path().read_text())
        next(a for a in raw['accounts'] if a['id'] == paid)['paid_enabled'] = False
        profiles.config_path().write_text(json.dumps(raw))
        body['input'].append({'type': 'function_call_output', 'call_id': 'paid-off', 'output': 'done'})
        self.assertEqual(502, self.request(body)[0])
        self.assertEqual(2, len(self.requests))


class LedgerConcurrencyTests(unittest.TestCase):
    def test_overlapping_renewal_cannot_erase_an_already_recorded_debit(self):
        for reset_only in (False, True):
            with self.subTest(reset_only=reset_only), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'ledger.db'
                db = Database(path)
                entry = ledger.add_entry(db, agent='test', account='test', kind='subscription',
                                         total=10, remaining=0, reset_period='daily')
                db.conn.execute('UPDATE quota_entries SET period_start=?',
                                (ledger.iso(ledger.now_utc() - timedelta(days=1, minutes=1)),))
                db.conn.commit()
                db.close()
                connected = threading.Barrier(2)
                first_read = threading.Event()
                second_read = threading.Event()
                first_finished = threading.Event()
                context = threading.local()
                original = ledger._next_reset_start

                def pause_reset(row, now):
                    result = original(row, now)
                    if result is not None:
                        if context.second:
                            second_read.set()
                            self.assertTrue(first_finished.wait(3))
                        else:
                            first_read.set()
                            second_read.wait(.2)
                    return result

                def operate(second):
                    connection = Database(path)
                    context.second = second
                    try:
                        connected.wait(timeout=3)
                        if second:
                            self.assertTrue(first_read.wait(3))
                        if second and reset_only:
                            return ledger.apply_due_resets(connection)
                        try:
                            return ledger.consume(connection, entry, 7)['remaining']
                        except ledger.LedgerError:
                            return 'rejected'
                    finally:
                        connection.close()
                        if not second:
                            first_finished.set()

                with patch('qp.ledger._next_reset_start', side_effect=pause_reset), ThreadPoolExecutor(2) as workers:
                    first = workers.submit(operate, False)
                    second = workers.submit(operate, True)
                    self.assertEqual(3, first.result(timeout=5))
                    self.assertEqual(0 if reset_only else 'rejected', second.result(timeout=5))
                db = Database(path)
                try:
                    self.assertEqual(3, db.conn.execute('SELECT remaining FROM quota_entries').fetchone()[0])
                    self.assertEqual(7, db.conn.execute('SELECT sum(amount) FROM usage_events').fetchone()[0])
                finally:
                    db.close()

    def test_two_debits_cannot_overdraw_or_append_a_rejected_event(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'ledger.db'
            db = Database(path)
            entry = ledger.add_entry(db, agent='test', account='test', kind='credit_pack', total=10)
            db.close()
            checked = threading.Event()
            connected = threading.Barrier(2)
            second_started = threading.Event()
            second_finished = threading.Event()
            original = ledger.entry_status

            def pause_first(row, now):
                result = original(row, now)
                if not checked.is_set():
                    checked.set()
                    self.assertTrue(second_started.wait(3))
                    second_finished.wait(.2)
                return result

            def debit(second=False):
                connection = Database(path)
                try:
                    connected.wait(timeout=3)
                    if second:
                        self.assertTrue(checked.wait(3))
                        second_started.set()
                    try:
                        return ledger.consume(connection, entry, 7)['remaining']
                    except ledger.LedgerError:
                        return 'rejected'
                finally:
                    connection.close()
                    if second:
                        second_finished.set()

            with patch('qp.ledger.entry_status', side_effect=pause_first), ThreadPoolExecutor(2) as workers:
                first = workers.submit(debit)
                second = workers.submit(debit, True)
                self.assertCountEqual([3, 'rejected'], [first.result(timeout=5), second.result(timeout=5)])
            db = Database(path)
            try:
                self.assertEqual(3, db.conn.execute('SELECT remaining FROM quota_entries').fetchone()[0])
                self.assertEqual(1, db.conn.execute('SELECT count(*) FROM usage_events').fetchone()[0])
            finally:
                db.close()

    def test_event_failure_rolls_back_debit_and_force_remains_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / 'ledger.db')
            try:
                entry = ledger.add_entry(db, agent='test', account='test', kind='credit_pack', total=10)
                db.conn.execute("CREATE TRIGGER reject_event BEFORE INSERT ON usage_events BEGIN SELECT RAISE(ABORT, 'failed'); END")
                db.conn.commit()
                with self.assertRaises(sqlite3.IntegrityError):
                    ledger.consume(db, entry, 7)
                self.assertEqual(10, db.conn.execute('SELECT remaining FROM quota_entries').fetchone()[0])
                db.conn.execute('DROP TRIGGER reject_event')
                db.conn.commit()
                self.assertEqual(-4, ledger.consume(db, entry, 14, force=True)['remaining'])
            finally:
                db.close()
