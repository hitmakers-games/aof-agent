import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gateway import access, buttons, commands, notices, work
from developer.work_tools import execute as execute_work
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
            access.ensure_owner(self.db)
            put(self.db,'watch:123','on')
            access.grant(self.db,123,456,'operator')
            access.grant(self.db,123,789,'viewer')

    def register(self,number=1,mode='ask'):
        with self.db,patch('gateway.notices.time.time',return_value=100):
            return work.create(self.db,123,f'Task {number}','Details',mode=mode,shared=True,notify=True)

    def flush(self,actor=123):
        notices.flush(self.db,131)
        row=self.db.execute('SELECT reply_markup,message_key FROM outbox ORDER BY id DESC LIMIT 1').fetchone()
        with self.db:self.db.execute('INSERT INTO message_handles VALUES (?,?,?)',(actor,row[1],42))
        return json.loads(row[0])['inline_keyboard'][0]

    def click(self,value,actor=123,now=132):
        with self.db:return buttons.handle(self.db,actor,value,42,now)

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

    def test_denied_batch_rolls_back_earlier_approvals(self):
        with self.db,patch('gateway.notices.time.time',return_value=100):
            put(self.db,'watch:123','off')
            put(self.db,'watch:456','on')
            work.create(self.db,456,'First','Details',shared=True,notify=True)
            work.create(self.db,456,'Second','Details',shared=True,notify=True)
        start=self.flush(actor=456)[0]['callback_data']
        ordered=self.db.execute('SELECT id FROM work_items ORDER BY id').fetchall()
        with self.db:
            self.db.execute("UPDATE work_items SET visibility='private',requester_id=123 WHERE id=?",(ordered[-1][0],))
            result=action(self.db,lambda:buttons.handle(self.db,456,start,42,132))
        self.assertIn('access denied',result)
        self.assertEqual(self.db.execute("SELECT count(*) FROM work_items WHERE state='queued'").fetchone()[0],0)

    def test_delete_only_hides_closed_local_queue_item_and_keeps_history(self):
        self.assertNotIn('deleted_at', {row[1] for row in self.db.execute('PRAGMA table_info(work_items)')})
        item=self.register()
        with self.db:self.db.execute("UPDATE work_items SET state='ready_for_review' WHERE id=?",(item['id'],))
        result=execute_work(self.db,123,123,'delete',reference=item['issue_key'])
        self.assertEqual(result,{'task':item['issue_key'],'state':'deleted'})
        self.assertIsNotNone(work.get(self.db,item['id'])['deleted_at'])
        self.assertNotIn(item['issue_key'],work.listing(self.db,123))
        with self.assertRaisesRegex(ValueError,'Task not found'):
            work.require_item(self.db,123,item['issue_key'])
        self.assertEqual(self.db.execute("SELECT count(*) FROM audit_events WHERE entity=? AND action='work.deleted'",(item['id'],)).fetchone()[0],1)
        from gateway.preflight import check
        check(self.db)

    def test_delete_rejects_running_and_restartable_items(self):
        item=self.register()
        for state in ('running','held','awaiting_approval','queued','cancelling'):
            with self.subTest(state=state):
                with self.db:self.db.execute('UPDATE work_items SET state=? WHERE id=?',(state,item['id']))
                with self.assertRaisesRegex(ValueError,'Only closed tasks'):
                    execute_work(self.db,123,123,'delete',reference=item['id'])
        self.assertIsNone(work.get(self.db,item['id'])['deleted_at'])


if __name__=='__main__':unittest.main()
