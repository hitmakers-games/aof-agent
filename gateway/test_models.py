import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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

    def test_catalog_falls_back_when_cache_access_is_denied(self):
        original_stat = Path.stat
        original_read = Path.read_text

        def denied_stat(path, *args, **kwargs):
            if str(path) == '/transport/models.json':
                raise PermissionError('cache access denied')
            return original_stat(path, *args, **kwargs)

        def denied_read(path, *args, **kwargs):
            if str(path) == '/transport/models.json':
                raise PermissionError('cache access denied')
            return original_read(path, *args, **kwargs)

        source = Path(self.temp.name) / 'models_cache.json'
        source.write_text(json.dumps({'models': [{
            'slug': 'fallback-model', 'visibility': 'list',
            'default_reasoning_level': 'medium',
            'supported_reasoning_levels': [{'effort': 'medium'}],
        }]}))
        with patch.dict('os.environ', CODEX_HOME=self.temp.name), \
                patch.object(Path, 'stat', denied_stat), \
                patch.object(Path, 'read_text', denied_read):
            self.assertEqual(models.catalog(), [{
                'model': 'fallback-model', 'default': 'medium', 'efforts': ['medium'],
            }])
            source.unlink()
            self.assertEqual([item['model'] for item in models.catalog()],
                             [value['model'] for value in models.FALLBACK_MODELS])

    def test_shared_choice_survives_reconnect_and_applies_to_other_chats(self):
        with self.db:dispatch(self.db,1,'/model gpt-6-luna low',10,chat_id=-20)
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
            dispatch(self.db, 1, '/model gpt-6-luna low', 10, chat_id=-20)
            models.initialize(self.db)
        self.assertEqual(models.selected(self.db, 10), {'model':'gpt-6-luna','effort':'low'})

    def test_other_admin_changes_the_same_shared_preference(self):
        from gateway import access
        with self.db:
            access.grant(self.db, 10, 11, 'admin')
            dispatch(self.db, 1, '/reasoning high', 11, chat_id=-20)
        self.assertEqual(models.selected(self.db, 10)['effort'], 'high')

    def test_viewer_cannot_change_shared_settings(self):
        from gateway import access
        with self.db:
            access.grant(self.db, 10, 11, 'viewer')
        with self.assertRaises(ValueError):
            models.command(self.db, 11, -20, '/model', 'gpt-6-luna low')
        self.assertEqual(models.selected(self.db, 10), models.DEFAULT)

    def test_removed_commands_do_not_create_requests_or_change_settings(self):
        with self.db:
            for command in ('/mode fast','/fast explain this','/smart solve this'):
                self.assertEqual(dispatch(self.db,1,command,10),'Unknown command. Use /help.')
        self.assertEqual(models.selected(self.db,10),models.DEFAULT)
        self.assertEqual(self.db.execute('SELECT count(*) FROM inbox').fetchone()[0],0)
        self.assertNotIn('Mode:',dispatch(self.db,2,'/status',10))
        self.assertNotIn('/mode ',dispatch(self.db,3,'/help',10))

    def test_queued_request_keeps_its_original_choice(self):
        with self.db:
            dispatch(self.db,1,'/model gpt-6-luna low',10)
            dispatch(self.db,2,'explain this',10)
            dispatch(self.db,3,'/model gpt-6-astra high',10)
        self.assertEqual(models.for_request(self.db,2,10),{'model':'gpt-6-luna','effort':'low'})

    def test_invalid_combination_does_not_replace_saved_choice(self):
        with self.assertRaises(ValueError):
            dispatch(self.db,1,'/reasoning impossible',10)
        self.assertEqual(models.selected(self.db,10),models.DEFAULT)


if __name__=='__main__':unittest.main()
