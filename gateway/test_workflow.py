import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gateway import access, buttons, commands, notices, work
from telegram.storage import connect, put
from telegram.updates import action


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db=connect(Path(self.folder.name)/'workflow.sqlite3')
        self.addCleanup(self.db.close)
        with self.db:
            put(self.db,'owner',123)
            access.grant(self.db,123,456,'operator')
            access.grant(self.db,123,789,'viewer')

    def register(self,number=1,mode='ask'):
        with self.db,patch('gateway.notices.time.time',return_value=100):
            return work.create(self.db,123,f'Task {number}','Details',mode=mode,shared=True)

    def test_generated_task_alias_is_readable_unique_and_resolves_with_legacy_uuid(self):
        first=self.register()
        second=self.register(2)
        self.assertRegex(first['issue_key'], r'^TG-(?:[a-z]+-){2}[a-z]+$')
        self.assertNotEqual(first['issue_key'], second['issue_key'])
        self.assertEqual(work.get(self.db, first['issue_key'])['id'], first['id'])
        self.assertEqual(work.get(self.db, first['id'])['issue_key'], first['issue_key'])

    def flush(self):
        notices.flush(self.db,131)
        row=self.db.execute('SELECT reply_markup,message_key FROM outbox ORDER BY id DESC LIMIT 1').fetchone()
        with self.db:self.db.execute('INSERT INTO message_handles VALUES (?,?,?)',(123,row[1],42))
        return json.loads(row[0])['inline_keyboard'][0]

    def click(self,value,actor=123,now=132):
        with self.db:return buttons.handle(self.db,actor,value,42,now)

    def test_ten_assignments_make_one_notification_without_ai_jobs(self):
        for number in range(1,11):self.register(number)
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
        self.flush()
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM work_items').fetchone()[0],10)
        self.assertEqual(self.db.execute('SELECT count(*) FROM inbox').fetchone()[0],0)
        notices.flush(self.db,132)
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)

    def test_batch_approval_does_not_include_later_arrivals_or_replay(self):
        for number in range(1,11):self.register(number)
        start=self.flush()[0]['callback_data']
        later=self.register(11)
        self.click(start)
        self.assertEqual(self.db.execute("SELECT count(*) FROM work_items WHERE state='queued'").fetchone()[0],10)
        self.assertEqual(work.get(self.db,later['id'])['state'],'awaiting_approval')
        count=self.db.execute('SELECT count(*) FROM outbox').fetchone()[0]
        self.assertIn('Already',self.click(start))
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],count)

    def test_forged_cross_user_viewer_expired_and_wrong_message_buttons_denied(self):
        self.register()
        start=self.flush()[0]['callback_data']
        for actor in (456,789,999):
            with self.assertRaises(ValueError):self.click(start,actor)
        with self.assertRaises(ValueError):self.click(start,now=10**12)
        with self.db,self.assertRaises(ValueError):buttons.handle(self.db,123,start,999,132)
        self.assertEqual(self.db.execute("SELECT count(*) FROM work_items WHERE state='queued'").fetchone()[0],0)

    def test_scope_change_invalidates_snapshot(self):
        item=self.register()
        start=self.flush()[0]['callback_data']
        with self.db:work.amend(self.db,123,item['id'],'New scope')
        self.click(start)
        self.assertEqual(work.get(self.db,item['id'])['state'],'awaiting_approval')
        self.assertIsNone(work.get(self.db,item['id'])['approved_revision'])

    def test_auto_start_is_admin_controlled_and_does_not_enable_coding(self):
        first=self.register(mode='auto')
        self.assertEqual(first['state'],'awaiting_approval')
        with self.db:commands.dispatch(self.db,1,'/autostart on',123)
        automatic=self.register(2,mode='auto')
        manual=self.register(3)
        self.assertEqual(automatic['state'],'queued')
        self.assertEqual(manual['state'],'awaiting_approval')
        with self.db:commands.dispatch(self.db,2,'/autostart off',123)
        self.assertEqual(work.get(self.db,automatic['id'])['state'],'awaiting_approval')
        self.assertIn('not enabled',commands.dispatch(self.db,3,'/status',123))

    def test_hold_and_cancel_do_not_restart_work_accidentally(self):
        item=self.register()
        with self.db:work.hold(self.db,123,item['id'])
        self.assertEqual(work.get(self.db,item['id'])['state'],'held')
        with self.db:commands.dispatch(self.db,1,'/cancel '+item['id'],123)
        self.assertEqual(work.get(self.db,item['id'])['state'],'cancelled')
        with self.db:work.approve(self.db,123,item['id'])
        self.assertEqual(work.get(self.db,item['id'])['state'],'cancelled')

    def test_repeat_render_edits_the_same_message_instead_of_spamming(self):
        item=self.register()
        self.flush()
        notices.flush(self.db,132)
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
        with self.db:work.approve(self.db,123,item['id'])
        notices.flush(self.db,133)
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],2)
        keys=self.db.execute('SELECT message_key FROM outbox').fetchall()
        self.assertEqual(keys[0],keys[1])
        notices.flush(self.db,134)
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],2)

    def test_denied_batch_rolls_back_earlier_approvals(self):
        first=self.register(1)
        second=self.register(2)
        start=self.flush()[0]['callback_data']
        # Change visibility after the button was rendered, as if permissions changed.
        ordered=self.db.execute('SELECT id FROM work_items ORDER BY id').fetchall()
        with self.db:
            self.db.execute("UPDATE work_items SET visibility='private',requester_id=456 WHERE id=?",(ordered[-1][0],))
            result=action(self.db,lambda:buttons.handle(self.db,123,start,42,132))
        self.assertIn('access denied',result)
        self.assertEqual(self.db.execute("SELECT count(*) FROM work_items WHERE state='queued'").fetchone()[0],0)


if __name__=='__main__':unittest.main()
