"""Business acceptance tests. All credentials, CLIs and CPA receipts are synthetic."""

import contextlib
from concurrent.futures import ThreadPoolExecutor
import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

from qp import cli, conductor, executors, ledger, pools, profiles
from qp.collectors.base import CollectorError, QuotaReading
from qp.db import Database, SCHEMA


class FakeCPA:
    base_url = "http://synthetic-cpa.invalid"

    def __init__(self, records=None, files=None):
        self.records = records or []
        self.files = files or []
        self.changes = []

    def drain_usage_queue(self, count):
        return self.records[:count]

    def list_auth_files(self):
        return self.files

    def set_auth_disabled(self, name, disabled):
        self.changes.append((name, disabled))


class PoolAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"QPOOL_CONFIG": str(self.root / "pools.json"),
                                           "QPOOL_DB": str(self.root / "test.db")})
        self.env.start()
        self.db = Database(self.root / "test.db")
        self.now = ledger.now_utc()
        script = self.root / "fake_cli.py"
        script.write_text('''import json, os, pathlib, sys
data = json.loads((pathlib.Path(os.environ['QPOOL_TEST_PROFILE']) / 'auth.json').read_text())
if sys.argv[2] == 'fail':
    (pathlib.Path(os.environ['QPOOL_TEST_PROFILE']) / 'started').write_text('started')
    sys.exit(1)
print(json.dumps({'subject': data['account_id'], 'model': sys.argv[1], 'task': sys.argv[2]}))
''')
        accounts, channels = [], []
        for aid in ("A", "B"):
            directory = self.root / aid
            directory.mkdir()
            auth = directory / "auth.json"
            auth.write_text(json.dumps({"access_token": "synthetic-secret", "account_id": "vendor-" + aid}))
            accounts.append({"id": aid, "agent": "codex", "label": "label-" + aid,
                             "source": {"credentials_file": str(auth),
                                        "fields": {"access_token": "access_token", "account_id": "account_id"},
                                        "subject_field": "account_id", "expected_subject": "vendor-" + aid,
                                        "profile_env": "QPOOL_TEST_PROFILE", "profile_dir": str(directory)},
                             "pools": [{"id": aid + "-sub", "kind": "subscription", "unit": "requests",
                                        "capability_tier": 4,
                                        "windows": [{"key": "five_hour"}, {"key": "seven_day"}]}]})
            channels.append({"id": aid + "-cli", "account_id": aid, "pool_id": aid + "-sub",
                             "type": "cli", "model": "test-model", "command":
                             [sys.executable, str(script), "{model}", "{task}"],
                             "env": {"QPOOL_TEST_PROFILE": str(directory)}})
        self.raw = {"accounts": accounts, "channels": channels}
        self.save()
        self.config = profiles.load()
        pools.register(self.db, self.config)
        for aid in ("A", "B"):
            self.snapshot(aid)

    def tearDown(self):
        self.db.close()
        self.env.stop()
        self.tmp.cleanup()

    def save(self):
        (self.root / "pools.json").write_text(json.dumps(self.raw))

    def readings(self, five=20, week=30):
        return [QuotaReading("codex", key, key, used_percent=value,
                             resets_at=self.now + timedelta(hours=2 if key == "five_hour" else 24),
                             window_seconds=7200 if key == "five_hour" else 86400)
                for key, value in (("five_hour", five), ("seven_day", week))]

    def snapshot(self, aid="A", five=20, week=30, at=None):
        return pools.apply_snapshot(self.db, self.config, aid, self.readings(five, week), at or self.now)

    def verdict(self, aid="A", model="test-model", at=None, amount=50000):
        return pools.evaluate(self.db, self.config["pools"][aid + "-sub"], model,
                              now=at or self.now, amount=amount)

    def plan(self):
        return pools.plan_route(self.db, 50000, "tokens", 3, "cost")

    def with_cpa(self):
        self.raw["accounts"][0]["cpa"] = {"auth_name": "a.json", "provider": "codex",
                "subject_field": "account_id", "usage_source": "source-A", "exclusive": True}
        self.save()

    def test_two_accounts_do_not_overwrite_each_other(self):
        self.snapshot("A", 100, at=self.now + timedelta(seconds=1))
        self.assertEqual("blocked", self.verdict(at=self.now + timedelta(seconds=1))["state"])
        self.assertEqual("ready", self.verdict("B")["state"])
        rows = self.db.conn.execute("SELECT pool_id, COUNT(*) FROM quota_entries GROUP BY pool_id").fetchall()
        self.assertEqual({"A-sub": 2, "B-sub": 2}, {r[0]: r[1] for r in rows})

    def test_either_common_window_blocks(self):
        for five, week in ((100, 10), (10, 100)):
            self.now += timedelta(seconds=1)
            self.snapshot(five=five, week=week)
            self.assertEqual("blocked", self.verdict()["state"])
            self.assertNotIn("A", [c["account_id"] for c in self.plan()["candidates"]])

    def test_percentage_is_not_a_token_capacity(self):
        verdict = self.verdict()
        self.assertEqual("ready", verdict["state"])
        self.assertEqual("unknown", verdict["coverage"])
        self.assertIsNone(verdict["remaining"])

    def test_duplicate_and_late_batches_are_ignored(self):
        self.assertTrue(self.snapshot(five=100)["ignored"])
        self.snapshot(five=50, at=self.now + timedelta(seconds=2))
        self.assertTrue(self.snapshot(five=100, at=self.now + timedelta(seconds=1))["ignored"])
        value = self.db.conn.execute("SELECT used_percent FROM quota_entries WHERE pool_id='A-sub' AND window_key='five_hour'").fetchone()[0]
        self.assertEqual(50, value)

    def test_partial_batch_invalidates_without_half_update(self):
        outcome = pools.apply_snapshot(self.db, self.config, "A", self.readings(100)[:1], self.now + timedelta(seconds=2))
        self.assertTrue(outcome["incomplete"])
        self.assertEqual("unknown", self.verdict()["state"])
        values = self.db.conn.execute("SELECT used_percent FROM quota_entries WHERE pool_id='A-sub' ORDER BY window_key").fetchall()
        self.assertEqual([20, 30], [r[0] for r in values])
        self.assertTrue(self.snapshot(at=self.now + timedelta(seconds=1))["ignored"])
        self.assertEqual("unknown", self.verdict()["state"])

    def test_batch_sql_error_rolls_back_all_windows(self):
        self.db.conn.execute("""CREATE TRIGGER reject_week BEFORE UPDATE ON quota_entries
            WHEN NEW.window_key='seven_day' BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.snapshot(five=90, at=self.now + timedelta(seconds=1))
        self.assertEqual(20, self.db.conn.execute("SELECT used_percent FROM quota_entries WHERE pool_id='A-sub' AND window_key='five_hour'").fetchone()[0])

    def test_statistical_and_model_specific_windows(self):
        self.raw["accounts"][0]["pools"][0]["windows"] += [
            {"key": "opus", "scope": "opus"}, {"key": "spend", "role": "stat"}]
        self.save()
        self.config = profiles.load()
        pools.register(self.db, self.config)
        readings = self.readings() + [QuotaReading("codex", "opus", "opus", used_percent=100),
                                     QuotaReading("codex", "spend", "spend", consumed_abs=100000)]
        pools.apply_snapshot(self.db, self.config, "A", readings, self.now + timedelta(seconds=1))
        at = self.now + timedelta(seconds=1)
        self.assertEqual("blocked", self.verdict(model="opus", at=at)["state"])
        self.assertEqual("ready", self.verdict(model="sonnet", at=at)["state"])

    def test_stale_and_reset_observations_need_confirmation(self):
        self.assertEqual("unknown", self.verdict(at=self.now + timedelta(seconds=121))["state"])
        with self.db.conn:
            self.db.conn.execute("UPDATE quota_entries SET resets_at=? WHERE pool_id='A-sub'", (ledger.iso(self.now - timedelta(seconds=1)),))
        ledger.apply_due_resets(self.db)
        self.assertEqual("unknown", self.verdict()["state"])
        values = self.db.conn.execute("SELECT used_percent FROM quota_entries WHERE pool_id='A-sub' ORDER BY window_key").fetchall()
        self.assertEqual([20, 30], [r[0] for r in values])

    def test_missing_channel_and_gateway_are_explained(self):
        self.raw["channels"] = [dict(self.raw["channels"][1], type="gateway")]
        self.save()
        plan = self.plan()
        self.assertEqual([], plan["candidates"])
        reasons = " ".join(c["why"] for c in plan["skipped"])
        self.assertIn("no account-bound", reasons)
        self.assertIn("pinning", reasons)

    def test_wrong_profile_and_missing_executable_are_not_candidates(self):
        self.raw["channels"][0]["env"] = self.raw["channels"][1]["env"]
        self.raw["channels"][1]["command"][0] = "/does/not/exist"
        self.save()
        plan = self.plan()
        self.assertEqual([], plan["candidates"])
        self.assertIn("profile", plan["skipped"][0]["why"])
        self.assertIn("not found", plan["skipped"][1]["why"])

    def test_selected_account_and_model_reach_actual_cli(self):
        candidate = self.plan()["candidates"][0]
        self.assertEqual("A", candidate["account_id"])
        result = executors.execute(candidate, "hello", db=self.db)
        self.assertTrue(result.success, result.error)
        actual = json.loads(result.output)
        self.assertEqual({"subject": "vendor-A", "model": "test-model", "task": "hello"}, actual)

    def test_successful_cli_text_is_not_misclassified_as_http_failure(self):
        candidate = self.plan()["candidates"][0]
        result = executors.execute(candidate, "explain HTTP 429 and insufficient quota", db=self.db)
        self.assertTrue(result.success)

    def test_ambient_api_credentials_do_not_override_selected_profile(self):
        config = self.config
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-ambient", "OPENAI_BASE_URL": "http://invalid"}):
            env = executors.execution_env(config["channels"][0], config["pools"]["A-sub"], config)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("OPENAI_BASE_URL", env)
        self.assertEqual(str((self.root / "A").resolve()), env["QPOOL_TEST_PROFILE"])

    def test_credential_rotation_and_label_change_keep_identity(self):
        auth = self.root / "A" / "auth.json"
        auth.write_text(json.dumps({"access_token": "rotated", "account_id": "vendor-A"}))
        self.raw["accounts"][0]["label"] = "renamed"
        self.save()
        config = profiles.load()
        pools.register(self.db, config)
        result = executors.execute(self.plan()["candidates"][0], "hello", db=self.db)
        self.assertTrue(result.success)
        self.assertEqual(2, self.db.conn.execute("SELECT COUNT(*) FROM quota_entries WHERE pool_id='A-sub'").fetchone()[0])

    def test_login_change_after_plan_does_not_execute(self):
        candidate = self.plan()["candidates"][0]
        (self.root / "A" / "auth.json").write_text(json.dumps({"access_token": "different", "account_id": "vendor-C"}))
        with patch("qp.executors.subprocess.run") as run:
            result = executors.execute(candidate, "hello", db=self.db)
        self.assertFalse(result.success)
        run.assert_not_called()

    def test_config_or_quota_change_after_plan_does_not_execute(self):
        candidate = self.plan()["candidates"][0]
        self.raw["channels"][0]["model"] = "different"
        self.save()
        with patch("qp.executors.subprocess.run") as run:
            self.assertFalse(executors.execute(candidate, "hello", db=self.db).success)
        run.assert_not_called()
        self.raw["channels"][0]["model"] = "test-model"
        self.save()
        self.snapshot(five=100, at=self.now + timedelta(microseconds=1))
        with patch("qp.executors.subprocess.run") as run:
            self.assertFalse(executors.execute(candidate, "hello", db=self.db).success)
        run.assert_not_called()

    def test_simulate_dry_run_and_real_run_share_plan(self):
        outputs = []
        for argv in (["quota", "simulate", "--amount", "50000", "--unit", "tokens"],
                     ["quota", "run", "hello", "--dry-run"]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output), patch("qp.executors.subprocess.run") as run:
                cli.main(argv)
            run.assert_not_called()
            outputs.append(output.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.main(["quota", "run", "hello"])
        self.assertIn('"subject": "vendor-A"', output.getvalue())

    def test_failure_after_start_does_not_try_other_account(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["quota", "run", "fail"])
        self.assertTrue((self.root / "A" / "started").exists())
        self.assertFalse((self.root / "B" / "started").exists())

    def test_sync_checks_each_profile_and_keeps_other_accounts_working(self):
        class Collector:
            def fetch_readings(inner, creds):
                if creds["account_id"] == "vendor-A":
                    raise CollectorError("synthetic provider failure")
                return self.readings(42)
        with patch.dict("qp.collectors.COLLECTORS", {"codex": Collector()}):
            result = pools.sync_accounts(self.db, apply=True)
        self.assertEqual(1, len(result["errors"]))
        self.assertEqual(2, result["synced"])

    def test_account_filtered_sync_does_not_change_other_account_metadata(self):
        self.raw["accounts"][1]["label"] = "not-applied"
        self.save()

        class Collector:
            def fetch_readings(inner, creds):
                self.assertEqual("vendor-A", creds["account_id"])
                return self.readings(42)

        with patch.dict("qp.collectors.COLLECTORS", {"codex": Collector()}):
            result = pools.sync_accounts(self.db, apply=True, account_id="A")
        self.assertEqual(2, result["synced"])
        self.assertEqual("label-B", self.db.conn.execute("SELECT account_label FROM quota_pools WHERE id='B-sub'").fetchone()[0])

    def test_existing_pool_cannot_be_reassigned(self):
        config = copy.deepcopy(self.config)
        config["pools"]["A-sub"]["subject"] = "vendor-other"
        with self.assertRaises(CollectorError):
            pools.register(self.db, config)
        self.assertEqual("vendor-A", self.db.conn.execute("SELECT subject FROM quota_pools WHERE id='A-sub'").fetchone()[0])

    def test_snapshot_cannot_be_manually_consumed(self):
        eid = self.db.conn.execute("SELECT id FROM quota_entries LIMIT 1").fetchone()[0]
        with self.assertRaises(ledger.LedgerError):
            ledger.consume(self.db, eid, 100, force=True)

    def test_usage_exact_identity_idempotency_and_no_window_deduction(self):
        self.with_cpa()
        record = {"provider": "codex", "source": "source-A", "model": "test-model",
                  "request_id": "request-1", "tokens": {"total_tokens": 200}}
        cpa = FakeCPA([record])
        self.assertEqual(1, conductor.pull_usage(self.db, cpa)["consumed"])
        self.assertEqual(0, conductor.pull_usage(self.db, cpa)["consumed"])
        self.assertEqual(1, self.db.conn.execute("SELECT COUNT(*) FROM imported_usage").fetchone()[0])
        self.assertEqual(0, self.db.conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0])
        self.assertEqual([20, 30], [r[0] for r in self.db.conn.execute("SELECT used_percent FROM quota_entries WHERE pool_id='A-sub' ORDER BY window_key")])
        report = ledger.monthly_report(self.db, self.now.strftime("%Y-%m"))
        self.assertEqual(200, report["rows"][0]["amount"])
        self.assertEqual(1, report["rows"][0]["n"])
        self.assertEqual(1, len(ledger.list_events(self.db)))

    def test_unmatched_or_missing_id_receipts_remain_pending(self):
        self.with_cpa()
        cpa = FakeCPA([{"provider": "codex", "source": "source-A-extra", "request_id": "unknown",
                        "tokens": {"total_tokens": 200}},
                       {"provider": "codex", "source": "source-A", "tokens": {"total_tokens": 200}}])
        self.assertEqual(0, conductor.pull_usage(self.db, cpa)["consumed"])
        self.assertEqual(2, self.db.conn.execute("SELECT COUNT(*) FROM imported_usage WHERE status='pending'").fetchone()[0])
        self.assertEqual(2, ledger.monthly_report(self.db, self.now.strftime("%Y-%m"))["pending"])

    def test_malformed_usage_does_not_abort_following_receipts(self):
        self.with_cpa()
        records = [None, {"request_id": "bad", "tokens": ["invalid"]},
                   {"provider": "codex", "source": "source-A", "request_id": "good",
                    "tokens": {"total_tokens": 123, "input_tokens": 2**100}}]
        result = conductor.pull_usage(self.db, FakeCPA(records))
        self.assertEqual(1, result["consumed"])
        self.assertEqual(3, self.db.conn.execute("SELECT COUNT(*) FROM imported_usage").fetchone()[0])

    def test_custom_provider_usage_fallback_is_preserved(self):
        self.raw["accounts"][0]["id"] = "custom-account"
        self.raw["accounts"][0]["agent"] = "custom-provider"
        self.raw["accounts"][0]["pools"][0]["id"] = "custom-sub"
        self.raw["channels"] = []
        self.raw["accounts"][0]["cpa"] = {"usage_source": "custom-source", "exclusive": True}
        self.save()
        pools.register(self.db, profiles.load())
        cpa = FakeCPA([{"provider": "openai-compatible-custom-provider", "source": "custom-source",
                        "request_id": "custom-1", "tokens": {"total_tokens": 100}}])
        self.assertEqual(1, conductor.pull_usage(self.db, cpa)["consumed"])

    def test_manual_disable_is_never_recovered(self):
        self.with_cpa()
        cpa = FakeCPA(files=[{"name": "a.json", "provider": "codex", "account_id": "vendor-A", "disabled": True}])
        plan = conductor.reconcile(self.db, cpa, apply=True)
        self.assertEqual("keep", plan[0]["decision"])
        self.assertEqual([], cpa.changes)

    def test_model_scope_does_not_globally_pause_cpa(self):
        self.with_cpa()
        self.raw["accounts"][0]["pools"][0]["windows"] = [{"key": "five_hour", "scope": "opus"}]
        self.save()
        self.config = profiles.load()
        pools.register(self.db, self.config)
        pools.apply_snapshot(self.db, self.config, "A", self.readings(100), self.now + timedelta(microseconds=1))
        cpa = FakeCPA(files=[{"name": "a.json", "provider": "codex", "account_id": "vendor-A"}])
        self.assertEqual("keep", conductor.reconcile(self.db, cpa, apply=True)[0]["decision"])
        self.assertEqual([], cpa.changes)

    def test_concurrent_usage_imports_book_once(self):
        self.with_cpa()
        record = {"provider": "codex", "source": "source-A", "request_id": "same-id",
                  "tokens": {"total_tokens": 700}}

        def import_once():
            db = Database(self.root / "test.db")
            try:
                return conductor.pull_usage(db, FakeCPA([record]))["consumed"]
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(lambda _: import_once(), range(2)))
        self.assertEqual(1, sum(results))
        self.assertEqual(700, self.db.conn.execute("SELECT SUM(amount) FROM imported_usage").fetchone()[0])

    def manual_pack(self, unit="tokens", expires=None):
        self.raw["accounts"][1]["pools"] = [{"id": "B-pack", "kind": "credit_pack", "unit": unit,
                                                "windows": [{"key": "manual"}]}]
        self.raw["channels"][1]["pool_id"] = "B-pack"
        self.save()
        self.config = profiles.load()
        eid = ledger.add_entry(self.db, agent="codex", account="manual-B", kind="credit_pack",
                               total=1000, unit=unit, expires_at=expires)
        pools.bind_entry(self.db, self.config, eid, "B-pack", "manual")
        return eid

    def test_legacy_manual_binding_preserves_history_and_capacity(self):
        eid = self.manual_pack()
        ledger.consume(self.db, eid, 100)
        verdict = pools.evaluate(self.db, self.config["pools"]["B-pack"], "test-model", amount=500)
        self.assertEqual(("ready", "known", 900), (verdict["state"], verdict["coverage"], verdict["remaining"]))
        self.assertEqual("blocked", pools.evaluate(self.db, self.config["pools"]["B-pack"], "test-model", amount=901)["state"])
        self.assertEqual(100, self.db.conn.execute("SELECT amount FROM usage_events WHERE entry_id=?", (eid,)).fetchone()[0])

    def test_manual_expiry_and_units_are_respected(self):
        self.manual_pack(expires=ledger.iso(self.now - timedelta(days=1)))
        verdict = pools.evaluate(self.db, self.config["pools"]["B-pack"], "test-model", amount=1)
        self.assertEqual("blocked", verdict["state"])
        self.assertIn("expired", verdict["why"])

    def test_credit_balance_is_never_converted_to_tokens(self):
        self.manual_pack(unit="credits")
        verdict = pools.evaluate(self.db, self.config["pools"]["B-pack"], "test-model", amount=50000, unit="tokens")
        self.assertEqual("unknown", verdict["coverage"])
        self.assertIsNone(verdict["remaining"])

    def test_rebinding_snapshot_does_not_make_it_manual(self):
        eid = self.db.conn.execute("SELECT id FROM quota_entries WHERE pool_id='A-sub' LIMIT 1").fetchone()[0]
        key = self.db.conn.execute("SELECT window_key FROM quota_entries WHERE id=?", (eid,)).fetchone()[0]
        for _ in range(2):
            pools.bind_entry(self.db, self.config, eid, "A-sub", key)
        self.assertEqual("snapshot", self.db.conn.execute("SELECT source_kind FROM quota_entries WHERE id=?", (eid,)).fetchone()[0])
        self.assertEqual("unknown", self.verdict()["state"])

    def test_multiple_funding_pools_are_not_silently_selected_by_cli(self):
        self.raw["accounts"][0]["pools"].append({"id": "A-credits", "kind": "credit_pack",
                                                    "windows": [{"key": "credits"}]})
        self.save()
        plan = self.plan()
        self.assertNotIn("A", [c["account_id"] for c in plan["candidates"]])
        self.assertTrue(any("funding selection" in c["why"] for c in plan["skipped"]))

    def test_unknown_payg_estimate_cannot_bypass_budget(self):
        self.raw["accounts"][1]["pools"] = [{"id": "B-payg", "kind": "payg", "unit": "credits",
                                                "cost_per_unit": 0.2, "windows": []}]
        self.raw["channels"][1]["pool_id"] = "B-payg"
        self.save()
        pools.register(self.db, profiles.load())
        self.snapshot(five=100, at=self.now + timedelta(microseconds=1))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), patch("qp.executors.subprocess.run") as run:
            with self.assertRaises(SystemExit):
                cli.main(["quota", "run", "hello", "--max-cost", "1"])
        run.assert_not_called()

    def test_nonfinite_budget_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()), patch("qp.executors.subprocess.run") as run:
            with self.assertRaises(SystemExit):
                cli.main(["quota", "run", "hello", "--max-cost", "nan"])
        run.assert_not_called()

    def test_only_qpool_pause_recovers_after_fresh_reading(self):
        self.with_cpa()
        self.snapshot(five=100, at=self.now + timedelta(microseconds=1))
        cpa = FakeCPA(files=[{"name": "a.json", "provider": "codex", "account_id": "vendor-A", "disabled": False}])
        self.assertEqual("disable", conductor.reconcile(self.db, cpa, apply=True)[0]["decision"])
        cpa.files[0]["disabled"] = True
        self.snapshot(at=self.now + timedelta(microseconds=2))
        self.assertEqual("enable", conductor.reconcile(self.db, cpa, apply=True)[0]["decision"])
        self.assertEqual([("a.json", True), ("a.json", False)], cpa.changes)

    def test_pause_ownership_does_not_cross_cpa_instances(self):
        self.with_cpa()
        self.snapshot(five=100, at=self.now + timedelta(microseconds=1))
        cpa = FakeCPA(files=[{"name": "a.json", "provider": "codex", "account_id": "vendor-A", "disabled": False}])
        conductor.reconcile(self.db, cpa, apply=True)
        self.snapshot(at=self.now + timedelta(microseconds=2))
        other = FakeCPA(files=[dict(cpa.files[0], disabled=True)])
        other.base_url = "http://different-cpa.invalid"
        self.assertEqual("keep", conductor.reconcile(self.db, other, apply=True)[0]["decision"])
        self.assertEqual([], other.changes)

    def test_cpa_identity_mismatch_and_model_scope_do_not_disable(self):
        self.with_cpa()
        self.snapshot(five=100, at=self.now + timedelta(microseconds=1))
        cpa = FakeCPA(files=[{"name": "a.json", "provider": "codex", "account_id": "vendor-B"}])
        self.assertEqual("untracked", conductor.reconcile(self.db, cpa, apply=True)[0]["decision"])
        self.assertEqual([], cpa.changes)


class MigrationAcceptance(unittest.TestCase):
    def test_v1_history_backup_binding_and_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            old = sqlite3.connect(path)
            old.executescript(SCHEMA)
            stamp = ledger.iso(ledger.now_utc())
            old.execute("""INSERT INTO quota_entries
                (agent, account, kind, total, remaining, created_at, updated_at)
                VALUES ('codex', 'work', 'credit_pack', 100, 75, ?, ?)""", (stamp, stamp))
            old.execute("INSERT INTO usage_events (entry_id, amount, est_cost_usd, created_at) VALUES (1,25,0,?)", (stamp,))
            old.commit()
            old.close()
            db = Database(path)
            row = db.conn.execute("SELECT * FROM quota_entries").fetchone()
            self.assertEqual((1, 75, None), (row["id"], row["remaining"], row["pool_id"]))
            self.assertEqual(25, db.conn.execute("SELECT amount FROM usage_events").fetchone()[0])
            db.close()
            backup = sqlite3.connect(str(path) + ".pre-pools-v1.bak")
            self.assertEqual(75, backup.execute("SELECT remaining FROM quota_entries").fetchone()[0])
            self.assertNotIn("pool_id", [r[1] for r in backup.execute("PRAGMA table_info(quota_entries)")])
            backup.close()
            reopened = Database(path)
            self.assertEqual(2, reopened.conn.execute("PRAGMA user_version").fetchone()[0])
            reopened.close()

    def test_newer_schema_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "new.db"
            conn = sqlite3.connect(path)
            conn.execute("PRAGMA user_version=99")
            conn.close()
            with self.assertRaises(RuntimeError):
                Database(path)


if __name__ == "__main__":
    unittest.main()
