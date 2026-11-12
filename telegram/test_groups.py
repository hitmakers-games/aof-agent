"""Group mentions never activate other users or disclose private history."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock

from gateway import access, tasks, work
from telegram.delivery import deliver_one
from telegram.storage import connect, put
from telegram.updates import handle_update
from worker.chat import prompt_for


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db=connect(Path(self.temp.name)/'state.sqlite3')
        self.addCleanup(self.db.close)
        self.number=0
        with self.db:
            put(self.db,'owner',123)
            put(self.db,'bot_username','AofAgentBot')
            put(self.db,'bot_id',777)
            access.ensure_owner(self.db)
            access.grant(self.db,123,456,'viewer')

    def message(self,text,actor=123,chat=-900,reply=None):
        self.number+=1
        handle_update(self.db,{'update_id':self.number,'message':{'text':text,'from':{'id':actor,'is_bot':False},
            'chat':{'id':chat,'type':'supergroup' if chat<0 else 'private'},'reply_to_message':reply}},'',0)

    def test_reply_to_bot_queues_message_with_quoted_context(self):
        self.message('Please explain',reply={'message_id':42,'from':{'id':777,'is_bot':True},'text':'Earlier answer'})
        row=self.db.execute('SELECT text,context,chat_id,requested_by FROM inbox').fetchone()
        self.assertEqual((row[0],row[2],row[3]),('Please explain',-900,123))
        self.assertIn('Earlier answer',row[1])
        self.assertIn('42',row[1])

    def test_replies_to_other_senders_and_malformed_replies_are_ignored(self):
        for reply in (None,[],{'from':[]},{'from':{'id':888,'username':'AofAgentBot','is_bot':True}},
                      {'from':{'id':456}}, {'from':{'id':'777'}}):
            self.message('hello',reply=reply)
        self.assertEqual(self.db.execute('SELECT count(*) FROM inbox').fetchone()[0],0)

    def test_reply_permissions_and_private_commands_are_preserved(self):
        reply={'from':{'id':777},'text':'Earlier answer'}
        self.message('hello',actor=999,reply=reply)
        self.message('hello',actor=456,reply=reply)
        self.assertEqual(self.db.execute('SELECT count(*) FROM inbox').fetchone()[0],0)
        self.message('/users',reply=reply)
        self.assertTrue(any('private chat' in row[0] for row in self.db.execute('SELECT text FROM outbox')))

    def test_only_exact_mentions_and_directed_commands_receive_replies(self):
        for text in ('Hello','/status','@AnotherBot hi','@AofAgentBot_other hi'):
            self.message(text)
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],0)
        self.message('/status@AofAgentBot')
        self.assertEqual(self.db.execute('SELECT chat_id,authorized_by FROM outbox').fetchone(),(-900,123))

    def test_unknown_people_and_viewers_cannot_run_ai(self):
        self.message('@AofAgentBot /status')
        self.message('@AofAgentBot hello',999)
        self.message('@AofAgentBot hello',456)
        self.assertEqual(self.db.execute('SELECT count(*) FROM inbox').fetchone()[0],0)

    def test_private_chat_does_not_require_a_mention(self):
        self.message('hello',chat=123)
        self.assertEqual(self.db.execute('SELECT chat_id FROM inbox').fetchone()[0],123)

    def test_group_and_private_model_context_are_separate(self):
        self.message('Private secret',chat=123)
        with self.db:self.db.execute("UPDATE inbox SET status='completed',response='Private answer'")
        self.message('@AofAgentBot Public topic')
        group_id=self.number
        self.assertNotIn('Private secret',prompt_for(self.db,'new question',123,-900))
        with self.db:self.db.execute("UPDATE inbox SET status='completed',response='Public answer' WHERE update_id=?",(group_id,))
        self.assertNotIn('Public topic',prompt_for(self.db,'private question',123,123))

    def test_group_reply_delivers_to_group_and_rechecks_permission(self):
        self.message('@AofAgentBot hello')
        tasks.finish(self.db,self.number,'Group reply')
        api=Mock()
        api.call.return_value={'message_id':42}
        while deliver_one(self.db,api):pass
        self.assertEqual(api.call.call_args.kwargs['chat_id'],-900)

    def test_private_reports_admin_commands_and_assignments_stay_private(self):
        self.message('Secret',chat=123)
        self.message('@AofAgentBot /report 1')
        self.message('@AofAgentBot /users')
        with self.db:item=work.create(self.db,123,'Confidential assignment')
        self.message('@AofAgentBot /work')
        self.message('@AofAgentBot /status '+item['id'])
        texts=[row[0] for row in self.db.execute('SELECT text FROM outbox WHERE chat_id=-900')]
        self.assertFalse(any('Confidential' in text for text in texts))
        self.assertTrue(any('private chat' in text for text in texts))


if __name__=='__main__':unittest.main()
