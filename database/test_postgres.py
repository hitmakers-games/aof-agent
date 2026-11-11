"""Run explicitly against the disposable aof_agent_migration_test database."""
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from database.errors import StorageError
from database.migrate import compare, rows, transfer
from database.postgres import Connection, initialize
from gateway import tasks
from telegram import bot
from telegram.storage import connect as legacy_connect, put, queue_report


def connect():
    if os.environ.get("AOF_DATABASE_NAME") != "aof_agent_migration_test":
        raise RuntimeError("This suite requires its explicitly named disposable database")
    return Connection(Path(os.environ["AOF_DATABASE_DSN_FILE"]).read_text().strip(),
                      os.environ["AOF_DATABASE_CA_FILE"], "aof_agent_migration_test")


class PostgresTests(unittest.TestCase):
    def setUp(self):
        self.db = connect()
        self.addCleanup(self.db.close)
        self.assertEqual(self.db.execute("SELECT current_database(),current_user").fetchone(),
                         ("aof_agent_migration_test", "aof_agent"))
        initialize(self.db)
        with self.db:
            self.db.execute("TRUNCATE settings,inbox,outbox RESTART IDENTITY")

    def queue(self, task_id=1):
        with self.db:
            put(self.db, "owner", 123)
            tasks.enqueue(self.db, task_id, "Synthetic task")
        tasks.claim(self.db, "model")
        with self.db:
            self.db.execute("DELETE FROM outbox")

    def source(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with patch.dict(os.environ, {}, clear=True):
            source = legacy_connect(Path(folder.name) / "synthetic.sqlite3")
        self.addCleanup(source.close)
        with source:
            put(source, "owner", 123)
            put(source, "offset", 900)
            source.execute("INSERT INTO inbox(update_id,text,status,created_at,response) VALUES (?,?,?,?,?)",
                           (4000000000000, "Unicode 🙂 and ? % ' text", "completed", "2026-11-11 12:34:56", "Saved reply"))
            source.execute("INSERT INTO outbox(id,text,delivered) VALUES (?,?,?)", (72, "Pending report", 0))
        return source

    def test_exact_migration_repeat_and_identity_sequence(self):
        source = self.source()
        expected = rows(source)
        transfer(source, self.db)
        compare(self.db, expected)
        transfer(source, self.db)
        compare(self.db, expected)
        with self.db:
            queue_report(self.db, "New report")
        self.assertGreater(self.db.execute("SELECT max(id) FROM outbox").fetchone()[0], 72)
        self.assertEqual(source.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)

    def test_migration_refuses_to_replace_different_existing_data(self):
        source = self.source()
        with self.db:
            put(self.db, "owner", 456)
        with self.assertRaises(StorageError):
            transfer(source, self.db)
        self.assertEqual(self.db.execute("SELECT value FROM settings WHERE key='owner'").fetchone()[0], "456")
        self.assertEqual(self.db.execute("SELECT count(*) FROM inbox").fetchone()[0], 0)

    def test_commit_rollback_and_bound_values_survive_reconnect(self):
        value = "🙂 ? % '; DROP TABLE settings; --"
        with self.db:
            put(self.db, "keep", value)
        with self.assertRaises(RuntimeError):
            with self.db:
                put(self.db, "rollback", "not committed")
                raise RuntimeError("synthetic failure")
        other = connect()
        self.addCleanup(other.close)
        self.assertEqual(other.execute("SELECT key,value FROM settings").fetchall(), [("keep", value)])

    def test_duplicate_updates_and_failure_preserve_offset_atomically(self):
        with self.db:
            put(self.db, "owner", 123)
            put(self.db, "offset", 1)
        update = {"update_id": 1, "message": {"text": "/ask synthetic", "chat": {"id": 123, "type": "private"},
                                             "from": {"id": 123, "is_bot": False}}}
        with patch.object(tasks, "queue_report", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):
                bot.handle_update(self.db, update, "", 0)
        self.assertEqual(bot.setting(self.db, "offset"), "1")
        self.assertEqual(self.db.execute("SELECT count(*) FROM inbox").fetchone()[0], 0)
        bot.handle_update(self.db, update, "", 0)
        bot.handle_update(self.db, update, "", 0)
        self.assertEqual(bot.setting(self.db, "offset"), "2")
        self.assertEqual(self.db.execute("SELECT count(*) FROM inbox").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)

    def parallel(self, operation):
        barrier = threading.Barrier(2)
        results, errors = [], []
        def worker():
            connection = connect()
            try:
                barrier.wait(timeout=5)
                results.append(operation(connection))
            except BaseException as error:
                errors.append(type(error).__name__)
            finally:
                connection.close()
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        return results

    def test_concurrent_workers_claim_one_task_once(self):
        with self.db:
            tasks.enqueue(self.db, 1, "Synthetic task")
        results = self.parallel(lambda db: tasks.claim(db, "model"))
        self.assertEqual(sum(item is not None for item in results), 1)
        self.assertEqual(tasks.task(self.db, 1)["status"], "running")

    def test_concurrent_completion_publishes_exactly_one_report(self):
        self.queue()
        self.parallel(lambda db: tasks.finish(db, 1, "Synthetic result"))
        self.assertEqual(tasks.task(self.db, 1)["status"], "completed")
        self.assertEqual(self.db.execute("SELECT text FROM outbox").fetchall(), [("Synthetic result",)])

    def test_cancellation_discards_the_response(self):
        self.queue()
        other = connect()
        self.addCleanup(other.close)
        with other:
            tasks.cancel(other, 1)
        tasks.finish(self.db, 1, "This must be discarded")
        item = tasks.task(self.db, 1)
        self.assertEqual(item["status"], "cancelled")
        self.assertNotIn("discarded", item["response"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)

    def test_report_failure_rolls_back_completion(self):
        self.queue()
        with patch.object(tasks, "queue_report", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):
                tasks.finish(self.db, 1, "Synthetic result")
        self.assertEqual(tasks.task(self.db, 1)["status"], "running")
        self.assertIsNone(tasks.task(self.db, 1)["response"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)
        tasks.finish(self.db, 1, "Synthetic result")
        self.assertEqual(self.db.execute("SELECT count(*) FROM outbox").fetchone()[0], 1)

    def test_restart_preserves_queue_and_does_not_repeat_interrupted_work(self):
        self.queue()
        with self.db:
            tasks.enqueue(self.db, 2, "Next synthetic task")
        other = connect()
        self.addCleanup(other.close)
        tasks.recover(other, "model")
        self.assertEqual(tasks.task(other, 1)["status"], "interrupted")
        self.assertEqual(tasks.claim(other, "model")["update_id"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
