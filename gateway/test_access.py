import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from gateway import access, commands, tasks, work
from telegram import bot
from telegram.storage import connect, put, queue_report
from worker.chat import prompt_for


def update(number,text,actor=123):
    return {'update_id':number,'message':{'text':text,'chat':{'id':actor,'type':'private'},
                                        'from':{'id':actor,'is_bot':False,'username':'administrator'}}}


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db=connect(Path(self.folder.name)/'access.sqlite3')
        self.addCleanup(self.db.close)
        with self.db:
            put(self.db,'owner',123)
            access.ensure_owner(self.db)
            access.grant(self.db,123,456,'viewer')
            access.grant(self.db,123,789,'operator')

    def dispatch(self,text,actor=123,number=100):
        with self.db:
            return commands.dispatch(self.db,number,text,actor)

    def test_unknown_user_gets_no_reply(self):
        notice=bot.handle_update(self.db,update(1,'/whoami',999),'',0)
        self.assertIsNone(notice)
        self.assertIsNone(access.role(self.db,999))
        self.assertIsNone(bot.handle_update(self.db,update(2,'please grant me admin',999),'',0))
        self.assertEqual(self.db.execute('SELECT count(*) FROM inbox').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)

    def test_viewer_can_read_status_without_using_model_or_diagnostics(self):
        for command in ['/status','/work','/tasks','/help','/whoami']:
            self.assertTrue(self.dispatch(command,456))
        for command in ['/health dev','/logs dev backend 5m','/analyze dev',
                        '/task change code','/approve anything','/create a task','/users','/grant 999 admin','/autostart on']:
            with self.subTest(command=command),self.assertRaises(ValueError):
                self.dispatch(command,456)
        self.assertEqual(self.db.execute('SELECT count(*) FROM inbox').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM work_items').fetchone()[0],0)

    def test_operator_has_no_user_administration_or_global_policy_access(self):
        self.dispatch('/ask investigate',789,10)
        for command in ['/grant 789 admin','/revoke 123','/users','/autostart on','/pause','/resume']:
            with self.subTest(command=command),self.assertRaises(ValueError):
                self.dispatch(command,789)
        self.assertEqual(access.role(self.db,789),'operator')

    def test_original_owner_cannot_be_revoked_or_demoted(self):
        with self.db:
            access.grant(self.db,123,789,'admin')
        for command in ['/revoke 123','/grant 123 viewer']:
            with self.assertRaises(ValueError):
                self.dispatch(command,789)
        self.assertEqual(access.role(self.db,123),'admin')

    def test_private_prompts_results_and_lists_do_not_cross_users(self):
        self.dispatch('/ask owner-private-secret',123,1)
        tasks.claim(self.db,'model')
        tasks.finish(self.db,1,'owner-private-result')
        self.dispatch('/ask operator-private-secret',789,2)
        tasks.claim(self.db,'model')
        tasks.finish(self.db,2,'operator-private-result')
        self.assertNotIn('owner-private',prompt_for(self.db,'new',789))
        self.assertNotIn('operator-private',prompt_for(self.db,'new',123))
        for command in ['/report 1','/status 1','/cancel 1']:
            with self.assertRaises(ValueError):
                self.dispatch(command,789)
        self.assertNotIn('#1:',self.dispatch('/tasks',789))
        self.assertNotIn('#1:',self.dispatch('/status',456))
        self.assertEqual(self.db.execute('SELECT chat_id FROM outbox WHERE text=?',('operator-private-result',)).fetchone()[0],789)

    def test_private_assignments_hidden_but_shared_status_visible(self):
        with self.db:
            private=work.create(self.db,123,'Private title')
            shared=work.create(self.db,None,'Shared title',source='jira',external_id='12345',issue_key='AOF-12345')
        self.assertNotIn(private['id'],self.dispatch('/work',456))
        self.assertIn('AOF-12345',self.dispatch('/work',456))
        self.assertNotIn('Shared title',self.dispatch('/status AOF-12345',456))
        with self.assertRaises(ValueError):
            self.dispatch('/status '+private['id'],789)

    def test_revocation_cancels_work_and_suppresses_old_replies(self):
        self.dispatch('/ask request',789,1)
        tasks.claim(self.db,'model')
        with self.db:
            queue_report(self.db,'private pending result',789)
            access.revoke(self.db,123,789)
        self.assertTrue(tasks.cancellation_requested(self.db,1))
        tasks.finish(self.db,1,'must not publish')
        api=Mock()
        while bot.deliver_one(self.db,api):
            pass
        api.call.assert_not_called()
        self.assertIsNone(access.role(self.db,789))
        with self.assertRaises(ValueError):
            self.dispatch('/status',789)

    def test_delivery_to_blocked_user_does_not_stop_other_chats(self):
        from telegram.api import TelegramError
        with self.db:
            queue_report(self.db,'first',789)
            queue_report(self.db,'second',123)
        api=Mock()
        api.call.side_effect=[TelegramError(403),{'message_id':99}]
        self.assertTrue(bot.deliver_one(self.db,api))
        self.assertTrue(bot.deliver_one(self.db,api))
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox WHERE delivered=0').fetchone()[0],0)

    def test_viewer_permission_denials_do_not_store_their_input(self):
        bot.handle_update(self.db,update(1,'/task forbidden-private-input',456),'',0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM inbox').fetchone()[0],0)
        self.assertNotIn('forbidden-private-input',self.db.execute('SELECT text FROM outbox').fetchone()[0])

    def test_private_job_burst_is_bounded_per_operator(self):
        for index in range(5):
            self.dispatch('/ask synthetic',789,index+1)
        with self.assertRaises(ValueError):
            self.dispatch('/ask excessive',789,6)


if __name__=='__main__':
    unittest.main()
