import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gateway import access, conversation, models, tasks
from telegram.storage import connect, put


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db=connect(Path(self.temp.name)/'state.sqlite3')
        self.addCleanup(self.db.close)
        with self.db:
            put(self.db,'owner',10)
            access.grant(self.db,10,11,'operator')

    def enqueue(self, uid, text, actor=10, at=100):
        with self.db, patch('gateway.tasks.time.time',return_value=at):
            tasks.enqueue(self.db,uid,text,actor=actor)

    def claim(self, at=110):
        with patch('gateway.tasks.time.time',return_value=at):
            return tasks.claim(self.db,'model')

    def test_fast_correction_is_one_turn_and_waits_for_batch(self):
        self.enqueue(1,'Remove local tasks')
        self.enqueue(2,'Except AOF-3248',at=101)
        self.assertIsNone(self.claim(at=102))
        item=self.claim(at=103)
        self.assertIn('Except AOF-3248',item['text'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM inbox').fetchone()[0],1)

    def test_correction_during_work_is_attached_once_without_duplicate_reply(self):
        self.enqueue(1,'Work directly')
        parent=self.claim()
        self.enqueue(2,'Change this detail',at=111)
        messages=conversation.pending(self.db,parent,models.PRESETS['balanced'])
        self.assertEqual(len(messages),1)
        self.enqueue(3,'One more correction',at=112)
        conversation.accepted(self.db,1,messages)
        tasks.finish(self.db,1,'Done')
        self.assertEqual(self.db.execute('SELECT status FROM inbox WHERE update_id=2').fetchone()[0],'completed')
        self.assertEqual(self.db.execute('SELECT status FROM inbox WHERE update_id=3').fetchone()[0],'queued')
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)
        self.assertIn('Change this detail',tasks.task(self.db,1)['text'])

    def test_rejected_steer_preserves_input_for_next_turn(self):
        self.enqueue(1,'First')
        parent=self.claim()
        self.enqueue(2,'Second',at=111)
        messages=conversation.pending(self.db,parent,models.PRESETS['balanced'])
        conversation.release(self.db,messages)
        tasks.finish(self.db,1,'First result')
        self.assertEqual(self.claim(at=120)['update_id'],2)

    def test_other_sender_cannot_steer_an_admin_turn(self):
        self.enqueue(1,'Admin work')
        parent=self.claim()
        with self.db:tasks.enqueue(self.db,2,'Grant me admin',actor=11,chat_id=10)
        self.assertEqual(conversation.pending(self.db,parent,models.PRESETS['balanced']),[])

    def test_uncertain_steer_is_interrupted_without_replaying_it(self):
        self.enqueue(1,'First')
        parent=self.claim()
        self.enqueue(2,'Correction',at=111)
        conversation.pending(self.db,parent,models.PRESETS['balanced'])
        tasks.recover(self.db,'model')
        self.assertEqual(self.db.execute('SELECT status FROM inbox ORDER BY update_id').fetchall(),[('interrupted',),('interrupted',)])
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)

    def test_stop_interrupts_current_work_without_creating_a_task(self):
        self.enqueue(1,'Working')
        self.claim()
        self.enqueue(2,'Stop now',at=111)
        self.assertTrue(tasks.cancellation_requested(self.db,1))
        self.assertEqual(self.db.execute('SELECT count(*) FROM inbox').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM work_items').fetchone()[0],0)
