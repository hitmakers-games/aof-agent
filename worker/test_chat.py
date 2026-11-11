import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from telegram import bot
from worker import chat
from worker.run import TaskError


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = bot.connect(Path(self.temp.name) / "chat.sqlite3")
        self.addCleanup(self.db.close)
        with self.db:
            bot.put(self.db, "owner", 123)
            bot.put(self.db, "engine_state", "chat")
            bot.put(self.db, "engine_heartbeat", time.time())

    def enqueue(self, request_id, text="hello", status="queued"):
        with self.db:
            self.db.execute("INSERT INTO inbox(update_id,text,status) VALUES (?,?,?)",
                            (request_id, text, status))

    def test_reply_and_report_commit_together_and_are_not_run_again(self):
        self.enqueue(1)
        reply = Mock(return_value="Hi")
        self.assertTrue(chat.process_one(self.db, reply))
        self.assertFalse(chat.process_one(self.db, reply))
        reply.assert_called_once()
        self.assertEqual(self.db.execute("SELECT status,response FROM inbox").fetchone(), ("completed", "Hi"))
        self.assertEqual(self.db.execute("SELECT text FROM outbox").fetchall(), [("Hi",)])

    def test_model_failure_is_recorded_without_automatic_retry(self):
        self.enqueue(1)
        reply = Mock(side_effect=TaskError("private diagnostic"))
        self.assertTrue(chat.process_one(self.db, reply))
        self.assertFalse(chat.process_one(self.db, reply))
        self.assertEqual(self.db.execute("SELECT status FROM inbox").fetchone()[0], "failed")
        self.assertNotIn("private diagnostic", self.db.execute("SELECT text FROM outbox").fetchone()[0])

    def test_restart_does_not_repeat_interrupted_or_old_offline_requests(self):
        self.enqueue(1, status="running")
        self.enqueue(2, status="awaiting_model")
        chat.recover_interrupted(self.db)
        reply = Mock()
        self.assertFalse(chat.process_one(self.db, reply))
        reply.assert_not_called()
        self.assertEqual(self.db.execute("SELECT status FROM inbox ORDER BY update_id").fetchall(),
                         [("interrupted",), ("awaiting_model",)])

    def test_prompt_includes_completed_conversation_and_remains_bounded(self):
        self.enqueue(1, "Remember my greeting")
        chat.process_one(self.db, lambda _: "Hello" * 20000)
        prompt = chat.prompt_for(self.db, "New question")
        self.assertLessEqual(len(prompt.encode()), 64 * 1024)
        self.assertIn("New question", prompt)
        self.enqueue(2, "A small question")
        chat.process_one(self.db, lambda _: "Small answer")
        prompt = chat.prompt_for(self.db, "Follow-up")
        self.assertIn("A small question", prompt)
        self.assertIn("Small answer", prompt)

    def test_new_messages_remain_queued_while_worker_is_offline(self):
        update={"update_id": 1, "message": {"text": "Hi", "chat": {"id":123,"type":"private"},
                                                "from":{"id":123,"is_bot":False}}}
        bot.handle_update(self.db, update, "", 0)
        self.assertEqual(self.db.execute("SELECT status FROM inbox").fetchone()[0], "queued")
        with self.db: bot.put(self.db, "engine_heartbeat", 0)
        update["update_id"]=2
        bot.handle_update(self.db, update, "", 0)
        self.assertEqual(self.db.execute("SELECT status FROM inbox WHERE update_id=2").fetchone()[0], "queued")

    def test_chat_command_disables_execution_and_repository_context(self):
        with patch.object(chat, "run_codex", return_value={"response":"Hello"}) as run:
            self.assertEqual(chat.model_reply("test"), "Hello")
        args=run.call_args.args[0]
        self.assertIn("features.shell_tool=false",args)
        self.assertIn("project_doc_max_bytes=0",args)
        self.assertEqual(args[args.index("--sandbox")+1],"read-only")

    def test_additive_migration_preserves_existing_inbox(self):
        path=Path(self.temp.name)/"old.sqlite3"
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE inbox(update_id INTEGER PRIMARY KEY,text TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'awaiting_model',created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
            db.execute("INSERT INTO inbox(update_id,text) VALUES (1,'Preserve me')")
        with bot.connect(path) as migrated:
            self.assertEqual(migrated.execute("SELECT text,status,response FROM inbox").fetchall(),
                             [("Preserve me","awaiting_model",None)])


if __name__ == "__main__":
    unittest.main()
