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

    def test_chat_choice_survives_reconnect_and_does_not_change_other_chats(self):
        with self.db:dispatch(self.db,1,'/mode fast',10,chat_id=-20)
        other=connect(self.path)
        try:
            self.assertEqual(models.selected(other,-20),models.PRESETS['fast'])
            self.assertEqual(models.selected(other,10),models.PRESETS['balanced'])
        finally:other.close()

    def test_one_message_override_does_not_change_saved_mode(self):
        with self.db:
            dispatch(self.db,1,'/mode fast',10)
            dispatch(self.db,2,'/smart solve the difficult problem',10)
        self.assertEqual(models.for_request(self.db,2,10),models.PRESETS['smart'])
        self.assertEqual(models.selected(self.db,10),models.PRESETS['fast'])
        self.assertEqual(self.db.execute('SELECT text FROM inbox').fetchone()[0],'solve the difficult problem')

    def test_queued_request_keeps_its_original_choice(self):
        with self.db:
            dispatch(self.db,1,'/fast explain this',10)
            dispatch(self.db,2,'/mode smart',10)
        self.assertEqual(models.for_request(self.db,1,10),models.PRESETS['fast'])

    def test_invalid_combination_does_not_replace_saved_choice(self):
        with self.assertRaises(ValueError):
            dispatch(self.db,1,'/reasoning impossible',10)
        self.assertEqual(models.selected(self.db,10),models.PRESETS['balanced'])


if __name__=='__main__':unittest.main()
