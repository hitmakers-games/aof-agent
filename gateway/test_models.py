import json
from pathlib import Path
import tempfile
import unittest

from gateway import models
from gateway.commands import dispatch
from telegram.storage import connect, put


class ModelsTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'state.sqlite3'
        self.db=connect(self.path)
        with self.db:put(self.db,'owner',10)

    def tearDown(self):
        self.db.close();self.temp.cleanup()

    def test_settings_accept_models_and_efforts_without_a_catalog(self):
        with self.db:
            models.configure(self.db, 10, 10, 'model', 'custom-future-model custom-effort')
            models.snapshot(self.db, 123, 10)
        self.assertEqual(models.for_request(self.db, 123, 10),
                         {'model': 'custom-future-model', 'effort': 'custom-effort'})

    def test_telegram_model_commands_are_removed(self):
        for command in ('/model other low', '/models', '/reasoning high'):
            with self.db:
                self.assertEqual(dispatch(self.db, 1, command, 10), 'Unknown command. Use /help.')
        self.assertEqual(models.selected(self.db, 10), models.DEFAULT)

    def test_shared_choice_survives_reconnect_and_applies_to_other_chats(self):
        with self.db:models.configure(self.db,10,-20,'model','gpt-6-luna low')
        other=connect(self.path)
        try:
            self.assertEqual(models.selected(other,-20),{'model':'gpt-6-luna','effort':'low'})
            self.assertEqual(models.selected(other,10),{'model':'gpt-6-luna','effort':'low'})
        finally:other.close()

    def test_upgrade_carries_owner_preference_and_ignores_old_group_override(self):
        preference = {'model': 'gpt-6.1-sol', 'effort': 'medium'}
        with self.db:
            put(self.db, 'model:10', json.dumps(preference))
            put(self.db, 'model:-20', json.dumps({'model':'gpt-6-astra','effort':'high'}))
            models.initialize(self.db)
        self.assertEqual(models.selected(self.db, -20), preference)
        with self.db:
            models.configure(self.db,10,-20,'model','gpt-6-luna low')
            models.initialize(self.db)
        self.assertEqual(models.selected(self.db, 10), {'model':'gpt-6-luna','effort':'low'})

    def test_viewer_cannot_change_shared_settings(self):
        from gateway import access
        with self.db:
            access.grant(self.db, 10, 11, 'viewer')
        with self.assertRaises(ValueError):
            models.configure(self.db, 11, -20, 'model', 'gpt-6-luna low')
        self.assertEqual(models.selected(self.db, 10), models.DEFAULT)

if __name__=='__main__':unittest.main()
