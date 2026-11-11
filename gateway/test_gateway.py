import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from gateway import commands, diagnostics, tasks
from gateway.kubernetes import DiagnosticError
from gateway.redaction import log_summary
from telegram.storage import connect, put
from worker.run import TaskCancelled, run_codex


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "gateway.sqlite3"
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        with self.db:
            put(self.db, "owner", 123)

    def command(self, task_id, text):
        with self.db:
            return commands.dispatch(self.db, task_id, text)

    def test_commands_choose_explicit_targets_and_reject_injection(self):
        self.command(1, "/logs dev backend 5m")
        item = tasks.task(self.db, 1)
        self.assertEqual((item["kind"], item["target"], item["window"]), ("logs", "dev", 5))
        for value in ["/health prod", "/health", "/logs dev database", "/logs dev backend 61m",
                      "/logs dev backend 5m;curl", "/health dev anything"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.command(2, value)
        self.assertEqual(self.db.execute("SELECT count(*) FROM inbox").fetchone()[0], 1)
        self.assertIn("Unknown command", self.command(3, "/exec rm -rf /"))

    def test_status_and_cancel_do_not_wait_for_the_model(self):
        self.command(1, "/ask investigate a bug")
        tasks.claim(self.db, "model")
        self.assertIn("#1: running", self.command(2, "/status"))
        self.assertIn("cancelling", self.command(3, "/cancel 1"))
        self.assertTrue(tasks.cancellation_requested(self.db, 1))
        tasks.finish(self.db, 1, "This output must be discarded")
        self.assertEqual(tasks.task(self.db, 1)["status"], "cancelled")
        self.assertNotIn("discarded", tasks.task(self.db, 1)["response"])

    def test_queued_cancel_prevents_claim_and_completed_cancel_does_not_reopen(self):
        self.command(1, "/ask queued")
        self.command(2, "/cancel 1")
        self.assertIsNone(tasks.claim(self.db, "model"))
        self.command(3, "/ask complete")
        tasks.claim(self.db, "model")
        tasks.finish(self.db, 3, "Done")
        self.assertIn("already completed", self.command(4, "/cancel 3"))
        self.assertEqual(tasks.task(self.db, 3)["status"], "completed")

    def test_tasks_persist_and_interrupted_work_is_not_repeated(self):
        self.command(1, "/ask pending")
        self.command(2, "/health feature")
        tasks.claim(self.db, "model")
        with connect(self.path) as reopened:
            tasks.recover(reopened, "model")
            self.assertEqual(tasks.task(reopened, 1)["status"], "interrupted")
            self.assertIsNone(tasks.claim(reopened, "model"))
            self.assertEqual(tasks.claim(reopened, "operations")["update_id"], 2)

    def test_cancel_is_visible_across_worker_connections(self):
        self.command(1, "/ask running")
        tasks.claim(self.db, "model")
        with connect(self.path) as other:
            with other:
                tasks.cancel(other, 1)
        self.assertTrue(tasks.cancellation_requested(self.db, 1))
        tasks.finish(self.db, 1, "Done")
        self.assertEqual(tasks.task(self.db, 1)["status"], "cancelled")

    def test_report_reads_saved_result_without_starting_work(self):
        self.command(1, "/ask report")
        tasks.claim(self.db, "model")
        tasks.finish(self.db, 1, "Finding and next step")
        self.assertIn("Finding and next step", self.command(2, "/report 1"))
        self.assertEqual(self.db.execute("SELECT count(*) FROM inbox").fetchone()[0], 1)

    def test_analyses_require_evidence_before_the_model_can_claim(self):
        self.command(1, "/analyze dev backend 15m")
        self.assertIsNone(tasks.claim(self.db, "model"))
        item = tasks.claim(self.db, "operations")
        self.assertEqual(item["update_id"], 1)
        tasks.save_evidence(self.db, 1, "Observed health and exception counts")
        self.assertEqual(tasks.claim(self.db, "model")["context"], "Observed health and exception counts")

    def test_cancellation_during_collection_prevents_model_analysis(self):
        self.command(1, "/analyze dev")
        tasks.claim(self.db, "operations")
        self.command(2, "/cancel 1")
        tasks.save_evidence(self.db, 1, "Evidence")
        self.assertIsNone(tasks.claim(self.db, "model"))
        self.assertEqual(tasks.task(self.db, 1)["status"], "cancelled")

    def test_diagnostic_failures_are_reported_without_raw_response_data(self):
        self.command(1, "/health dev")
        client = Mock()
        client.deployment.side_effect = DiagnosticError("Kubernetes read failed (HTTP 403)")
        self.assertTrue(diagnostics.process_one(self.db, client))
        item = tasks.task(self.db, 1)
        self.assertEqual(item["status"], "failed")
        self.assertIn("403", item["response"])

    def test_log_summary_never_includes_player_records_or_credentials(self):
        text = 'ERROR java.lang.NullPointerException player=Alice balance=500 token=private\n'
        text += '{"player_id":123,"email":"alice@example.com","payment":"private"}\n'
        result = log_summary(text)
        self.assertIn("NullPointerException", result)
        for value in ["Alice", "balance", "500", "token=", "alice@example.com", '"player_id"']:
            self.assertNotIn(value, result)

    def test_cancellation_stops_an_actual_child_process(self):
        started = time.monotonic()
        with self.assertRaises(TaskCancelled):
            run_codex([sys.executable, "-c", "import time; time.sleep(30)"], "test", 30,
                      {"PATH": "/bin:/usr/bin"}, cancelled=lambda: time.monotonic() - started > 0.1)
        self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
