import copy
import io
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from telegram import bot, api as telegram_api
from gateway import models


def message(update_id, text, sender=123):
    return {"update_id": update_id, "message": {"text": text,
            "chat": {"id": sender, "type": "private"},
            "from": {"id": sender, "is_bot": False}}}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.db = bot.connect(self.path)
        self.addCleanup(self.db.close)

    def handle(self, update, now=10):
        bot.handle_update(self.db, update, "private-code", 100, now)

    def pair(self):
        self.handle(message(1, "/start private-code"))

    def test_only_valid_unexpired_private_pairing_can_bind_owner(self):
        wrong = message(1, "/start wrong")
        group = message(2, "/start private-code")
        group["message"]["chat"]["type"] = "group"
        robot = message(3, "/start private-code")
        robot["message"]["from"]["is_bot"] = True
        for update in [wrong, group, robot]:
            self.handle(update)
            self.assertIsNone(bot.setting(self.db, "owner"))
        self.handle(message(4, "/start private-code"), now=100)
        self.assertIsNone(bot.setting(self.db, "owner"))
        self.handle(message(5, "/start private-code"))
        self.assertEqual(bot.setting(self.db, "owner"), "123")
        self.handle(message(6, "/start private-code", sender=456))
        self.assertEqual(bot.setting(self.db, "owner"), "123")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)

    def test_unpaired_chat_gets_a_hint_without_disclosing_pairing_code(self):
        notice = bot.handle_update(self.db, message(1, "hello"), "private-code", 100, now=10)
        self.assertEqual(notice["chat_id"], 123)
        self.assertNotIn("private-code", notice["text"])
        self.assertEqual(bot.setting(self.db, "pairing_last_result"), "missing_code")
        self.assertIsNone(bot.setting(self.db, "owner"))

    def test_duplicate_updates_do_not_duplicate_inbox_or_add_acknowledgments(self):
        self.pair()
        update = message(2, "Investigate AOF-3350")
        self.handle(update)
        self.handle(copy.deepcopy(update))
        self.assertEqual(self.db.execute("SELECT text, status FROM inbox").fetchall(),
                         [("Investigate AOF-3350", "queued")])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)
        self.assertEqual(bot.setting(self.db, "offset"), "3")

    def test_unpaired_or_unauthorized_messages_are_not_saved_or_answered(self):
        self.handle(message(1, "hello"))
        self.handle(message(2, "/start private-code"))
        self.handle(message(3, "execute something", sender=456))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)

    def test_failure_rolls_back_inbox_and_offset_together(self):
        self.pair()
        with patch.object(models, "snapshot", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.handle(message(2, "/ask request"))
        self.assertEqual(bot.setting(self.db, "offset"), "2")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 0)
        self.handle(message(2, "/ask request"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 1)

    def test_report_is_only_delivered_to_owner_and_retried_after_failure(self):
        self.pair()
        api = Mock()
        api.call.side_effect = bot.TelegramError()
        with self.assertRaises(bot.TelegramError):
            bot.deliver_one(self.db, api)
        self.assertEqual(self.db.execute("SELECT delivered FROM outbox").fetchone()[0], 0)
        api.call.side_effect = None
        self.assertTrue(bot.deliver_one(self.db, api))
        self.assertEqual(api.call.call_args.kwargs["chat_id"], 123)
        self.assertFalse(bot.deliver_one(self.db, api))

    def test_unicode_reports_are_split_without_losing_text(self):
        text = "🙂hello\n" * 1000
        with self.db:
            bot.queue_report(self.db, text)
        chunks = [row[0] for row in self.db.execute("SELECT text FROM outbox ORDER BY id")]
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(part.encode("utf-16-le")) <= 7000 for part in chunks))

    def test_api_error_never_exposes_token_and_respects_rate_limit(self):
        token = "123:" + "x" * 30
        api = bot.Telegram(token)
        error = HTTPError("https://api.telegram.org/bot" + token, 429, "secret url", {},
                          io.BytesIO(b'{"parameters":{"retry_after":12}}'))
        with patch.object(telegram_api, "urlopen", side_effect=error):
            with self.assertRaises(bot.TelegramError) as caught:
                api.call("getMe")
        self.assertNotIn(token, str(caught.exception))
        self.assertEqual(caught.exception.retry_after, 12)
        with patch.object(telegram_api, "urlopen", side_effect=URLError(token)):
            with self.assertRaises(bot.TelegramError) as caught:
                api.call("getMe")
        self.assertNotIn(token, str(caught.exception))

    def test_irrelevant_and_malformed_updates_cannot_bind_owner(self):
        for value in [None, [], {}, {"update_id": 1, "message": None},
                      {"update_id": 2, "message": {"chat": None, "from": None}}]:
            self.handle(value)
        self.assertIsNone(bot.setting(self.db, "owner"))


if __name__ == "__main__":
    unittest.main()
