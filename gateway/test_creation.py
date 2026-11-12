import json
import tempfile
import unittest
from pathlib import Path

from gateway import access, drafts, tasks, work
from telegram.storage import connect, put
from worker.chat import process_one


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db=connect(Path(self.folder.name)/'drafts.sqlite3')
        self.addCleanup(self.db.close)
        with self.db:
            put(self.db,'owner',123)
            access.grant(self.db,123,456,'operator')

    def prepare(self,actor=123):
        with self.db:return drafts.propose(self.db,actor,[{'title':'Synthetic title','description':'Details'}],notify=False)

    def test_registration_is_idempotent_and_ask_first(self):
        draft=self.prepare()
        for _ in range(3):
            with self.db:drafts.confirm(self.db,123,draft['id'],draft['revision'],'register')
        self.assertEqual(self.db.execute('SELECT count(*) FROM work_items').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT state FROM work_items').fetchone()[0],'awaiting_approval')
        self.assertEqual(self.db.execute('SELECT count(*) FROM notice_items').fetchone()[0],0)

    def test_register_and_queue_preserves_the_human_authorization(self):
        draft=self.prepare()
        with self.db:drafts.confirm(self.db,123,draft['id'],draft['revision'],'register_run')
        item=work.get(self.db,self.db.execute('SELECT id FROM work_items').fetchone()[0])
        self.assertEqual(item['state'],'queued')
        self.assertEqual(item['approved_by'],123)
        self.assertEqual(item['approved_revision'],item['revision'])

    def test_drafts_cannot_smuggle_permissions_or_more_than_ten_tasks(self):
        for payload in [[{'title':'x','role':'admin'}],[{'title':'x','project':'OTHER'}],
                        [{'title':'x'}]*11,[],[{'title':'x','description':'x'*3001}]]:
            with self.subTest(payload=str(payload)[:60]),self.assertRaises(ValueError):
                with self.db:drafts.propose(self.db,123,payload)
        self.assertEqual(self.db.execute('SELECT count(*) FROM task_drafts').fetchone()[0],0)

    def test_private_draft_cannot_be_confirmed_by_another_operator(self):
        draft=self.prepare()
        with self.db,self.assertRaises(ValueError):drafts.confirm(self.db,456,draft['id'],draft['revision'],'register_run')
        with self.assertRaises(ValueError):drafts.describe(self.db,456,draft['id'])

    def test_revocation_and_regrant_do_not_restore_old_authorization(self):
        draft=self.prepare(456)
        with self.db:
            access.revoke(self.db,123,456)
            access.grant(self.db,123,456,'operator')
            drafts.confirm(self.db,456,draft['id'],draft['revision'],'register_run')
        self.assertEqual(self.db.execute('SELECT count(*) FROM work_items').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT state FROM task_drafts').fetchone()[0],'cancelled')

    def test_model_proposal_only_creates_a_draft(self):
        with self.db:
            tasks.enqueue(self.db,1,'Prepare two tasks',actor=123)
            put(self.db,'chat-ready:1',0)
        response=json.dumps({'kind':'task_proposal','reply':'Draft prepared.',
                             'tasks':[{'title':'First task'},{'title':'Second task'}]})
        self.assertTrue(process_one(self.db,lambda _:response))
        self.assertEqual(self.db.execute('SELECT state FROM task_drafts').fetchone()[0],'proposed')
        self.assertEqual(self.db.execute('SELECT count(*) FROM work_items').fetchone()[0],0)


if __name__=='__main__':unittest.main()
