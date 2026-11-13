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

    def test_unmentioned_messages_reach_conversation_routing(self):
        for reply in (None,[],{'from':[]}):
            self.message('hello',reply=reply)
        self.assertEqual(self.db.execute('SELECT count(*) FROM inbox').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT text FROM inbox').fetchone()[0],'hello\n\nhello\n\nhello')

    def test_unknown_silent_viewer_can_chat_admin_commands_work_in_group(self):
        self.message('/status')
        self.message('hello',actor=999)
        self.message('status please',actor=456)
        self.assertEqual(self.db.execute('SELECT requested_by FROM inbox').fetchall(),[(456,)])
        self.message('/users')
        self.assertTrue(any('123' in row[0] for row in self.db.execute('SELECT text FROM outbox')))

    def test_private_chat_does_not_require_a_mention(self):
        self.message('hello',chat=123)
        self.assertEqual(self.db.execute('SELECT chat_id FROM inbox').fetchone()[0],123)

    def test_private_admin_context_includes_group_work_but_group_excludes_private_work(self):
        self.message('Private secret',chat=123)
        with self.db:self.db.execute("UPDATE inbox SET status='completed',response='Private answer'")
        self.message('@AofAgentBot Public topic')
        group_id=self.number
        self.assertNotIn('Private secret',prompt_for(self.db,'new question',123,-900))
        with self.db:self.db.execute("UPDATE inbox SET status='completed',response='Public answer' WHERE update_id=?",(group_id,))
        self.assertIn('Public topic',prompt_for(self.db,'private question',123,123))

    def test_group_reply_delivers_to_group_and_rechecks_permission(self):
        self.message('@AofAgentBot hello')
        tasks.finish(self.db,self.number,'Group reply')
        api=Mock()
        api.call.return_value={'message_id':42}
        while deliver_one(self.db,api):pass
        self.assertEqual(api.call.call_args.kwargs['chat_id'],-900)

    def test_admin_chooses_to_read_private_assignment_in_group(self):
        with self.db:item=work.create(self.db,123,'Confidential assignment')
        self.message('/status '+item['id'])
        self.assertIn('Confidential assignment',self.db.execute('SELECT text FROM outbox').fetchone()[0])




    def test_silent_and_reaction_responses_do_not_send_text_or_typing(self):
        from telegram.bot import refresh_typing
        self.message('Thanks')
        request=self.number
        with self.db:
            put(self.db,'telegram-source:'+str(request),55)
            put(self.db,'engine_state','gateway')
            import time
            put(self.db,'engine_heartbeat',time.time())
        api=Mock()
        refresh_typing(self.db,api)
        api.call.assert_not_called()
        tasks.finish(self.db,request,'[[REACT:👍]]')
        self.assertTrue(deliver_one(self.db,api))
        api.call.assert_called_once_with('setMessageReaction',chat_id=-900,message_id=55,
            reaction=[{'type':'emoji','emoji':'👍'}],is_big=False)
        self.message('A note to myself')
        tasks.finish(self.db,self.number,'[[SILENT]]')
        self.assertFalse(deliver_one(self.db,api))

    def test_group_typing_requires_commitment_and_stops_when_finished(self):
        import time
        from telegram.bot import refresh_typing
        from worker.chat import mark_responding
        self.message('Please fix this')
        request=self.number
        with self.db:
            self.db.execute("UPDATE inbox SET status='running' WHERE update_id=?",(request,))
        with self.db:
            put(self.db,'engine_state','gateway')
            put(self.db,'engine_heartbeat',time.time())
        api=Mock()
        refresh_typing(self.db,api)
        api.call.assert_not_called()
        mark_responding(self.db,request)
        deadline=refresh_typing(self.db,api,now=10)
        api.call.assert_called_once_with('sendChatAction',chat_id=-900,action='typing',_timeout=3)
        refresh_typing(self.db,api,deadline,now=11)
        self.assertEqual(api.call.call_count,1)
        refresh_typing(self.db,api,deadline,now=14)
        self.assertEqual(api.call.call_count,2)
        tasks.finish(self.db,request,'Fixed')
        api.reset_mock()
        refresh_typing(self.db,api,now=18)
        api.call.assert_not_called()
        deliver_one(self.db,api)
        self.assertEqual([call.args[0] for call in api.call.call_args_list],['sendMessage'])

    def test_disabled_reactions_do_not_block_delivery_and_revocation_prevents_them(self):
        from telegram.api import TelegramError
        self.message('Thanks',actor=456)
        with self.db:put(self.db,'telegram-source:'+str(self.number),56)
        tasks.finish(self.db,self.number,'[[REACT:🙏]]')
        api=Mock()
        api.call.side_effect=TelegramError(400)
        self.assertTrue(deliver_one(self.db,api))
        self.assertFalse(deliver_one(self.db,api))
        self.message('Thanks again',actor=456)
        with self.db:put(self.db,'telegram-source:'+str(self.number),57)
        tasks.finish(self.db,self.number,'[[REACT:👍]]')
        with self.db:access.revoke(self.db,123,456)
        api.reset_mock()
        self.assertTrue(deliver_one(self.db,api))
        api.call.assert_not_called()
