"""Explicit synthetic-only security/workflow checks against real PostgreSQL."""
import os
from pathlib import Path
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from database.errors import StorageError
from database.postgres import Connection, initialize
from gateway import access, buttons, commands, notices, work
from telegram.storage import put
from worker.chat import prompt_for


def connect():
    if os.environ.get('AOF_DATABASE_NAME')!='aof_agent_gateway_test':
        raise RuntimeError('Refusing to test outside the explicitly named disposable database')
    return Connection(Path(os.environ['AOF_DATABASE_DSN_FILE']).read_text().strip(),
                      os.environ['AOF_DATABASE_CA_FILE'],'aof_agent_gateway_test')


class GatewayPostgresTests(unittest.TestCase):
    def setUp(self):
        self.db=connect()
        self.addCleanup(self.db.close)
        self.assertEqual(self.db.execute('SELECT current_database(),current_user').fetchone(),('aof_agent_gateway_test','aof_agent'))
        initialize(self.db)
        with self.db:
            self.db.execute('''TRUNCATE callback_tokens,message_handles,notice_items,notice_batches,task_drafts,
                work_items,audit_events,members,settings,inbox,outbox RESTART IDENTITY CASCADE''')
            put(self.db,'owner',123)
            access.ensure_owner(self.db)
            access.grant(self.db,123,456,'viewer')
            access.grant(self.db,123,789,'operator')

    def parallel(self,fn):
        barrier=threading.Barrier(2)
        results,errors=[],[]
        def run():
            db=connect()
            try:
                barrier.wait(timeout=5)
                results.append(fn(db))
            except BaseException as error:
                errors.append(type(error).__name__)
            finally:
                db.close()
        threads=[threading.Thread(target=run) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors,[])
        return results

    def prepare_batch(self):
        with self.db:
            for number in range(1,11):
                work.create(self.db,123,f'Task {number}','Details',shared=True)
        notices.flush(self.db,time.time()+31)
        row=self.db.execute('SELECT reply_markup,message_key FROM outbox').fetchone()
        import json
        with self.db:
            self.db.execute('INSERT INTO message_handles VALUES (?,?,?)',(123,row[1],42))
        return json.loads(row[0])['inline_keyboard'][0][0]['callback_data']

    def test_concurrent_callback_replay_approves_exact_batch_once(self):
        token=self.prepare_batch()
        def approve(db):
            with db:
                return buttons.handle(db,123,token,42)
        results=self.parallel(approve)
        self.assertEqual(sum('Already' in result for result in results),1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM work_items WHERE state='queued'").fetchone()[0],10)
        self.assertEqual(self.db.execute("SELECT count(*) FROM audit_events WHERE action='button.approve'").fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],2)

    def test_concurrent_notification_flush_creates_one_digest(self):
        with self.db:
            for number in range(1,11):work.create(self.db,123,f'Task {number}','Details',shared=True)
        self.parallel(lambda db:notices.flush(db,time.time()+31))
        self.assertEqual(self.db.execute('SELECT count(*) FROM outbox').fetchone()[0],1)

    def test_concurrent_draft_registration_creates_tasks_once(self):
        from gateway import drafts
        with self.db:
            proposal=drafts.propose(self.db,123,[{'title':'First'},{'title':'Second'}],notify=False)
        def register(db):
            with db:
                return drafts.confirm(db,123,proposal['id'],proposal['revision'],'register_run')
        self.parallel(register)
        self.assertEqual(self.db.execute('SELECT count(*) FROM work_items').fetchone()[0],2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM work_items WHERE state='queued'").fetchone()[0],2)

    def test_private_chat_and_viewer_permissions_on_real_backend(self):
        with self.db:
            commands.dispatch(self.db,1,'/ask owner-secret',123)
            self.db.execute("UPDATE inbox SET status='completed',response='owner-result' WHERE update_id=1")
            commands.dispatch(self.db,2,'/ask colleague-secret',789)
            self.db.execute("UPDATE inbox SET status='completed',response='colleague-result' WHERE update_id=2")
        self.assertNotIn('owner-secret',prompt_for(self.db,'new',789))
        self.assertNotIn('colleague-secret',prompt_for(self.db,'new',123))
        with self.assertRaises(ValueError):commands.dispatch(self.db,3,'/report 1',789)
        with self.assertRaises(ValueError):commands.dispatch(self.db,4,'/ask forbidden',456)
        self.assertIn('Developer:',commands.dispatch(self.db,5,'/status',456))

    def test_denied_batch_rolls_back_all_prior_items(self):
        from telegram.updates import action
        token=self.prepare_batch()
        last=self.db.execute('SELECT id FROM work_items ORDER BY id DESC LIMIT 1').fetchone()[0]
        with self.db:
            self.db.execute("UPDATE work_items SET visibility='private',requester_id=789 WHERE id=?",(last,))
            response=action(self.db,lambda:buttons.handle(self.db,123,token,42))
        self.assertIn('access denied',response)
        self.assertEqual(self.db.execute("SELECT count(*) FROM work_items WHERE state='queued'").fetchone()[0],0)

    def old_schema(self):
        name='upgrade_'+uuid.uuid4().hex
        self.db.execute('CREATE SCHEMA '+name)
        self.db.execute('SET search_path TO '+name)
        self.addCleanup(lambda:self.db.raw.execute('DROP SCHEMA '+name+' CASCADE'))
        self.db.execute(Path('/opt/aof-agent/database/schema.sql').read_text())
        with self.db:
            put(self.db,'owner',123)
            self.db.execute("INSERT INTO inbox(update_id,text,status) VALUES (17,'Synthetic saved conversation','completed')")
            self.db.execute("INSERT INTO outbox(text,delivered) VALUES ('Synthetic pending report',0)")

    def test_version_one_upgrade_preserves_history_pairing_and_pending_reports(self):
        self.old_schema()
        before=self.db.execute('SELECT update_id,text,status,created_at FROM inbox').fetchall()
        initialize(self.db)
        self.assertEqual(self.db.execute('SELECT update_id,text,status,created_at FROM inbox').fetchall(),before)
        self.assertEqual(self.db.execute('SELECT chat_id,requested_by FROM inbox').fetchone(),(123,123))
        self.assertEqual(self.db.execute('SELECT text,delivered,chat_id FROM outbox').fetchone(),('Synthetic pending report',0,123))
        self.assertEqual(self.db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0],2)
        initialize(self.db)
        self.assertEqual(self.db.execute('SELECT update_id,text,status,created_at FROM inbox').fetchall(),before)

    def test_failed_upgrade_rolls_back_ddl_and_migration_record(self):
        self.old_schema()
        original=self.db.execute
        def fail(query,parameters=None):
            if query==Path('/opt/aof-agent/database/gateway.sql').read_text():
                raise RuntimeError('synthetic DDL failure')
            return original(query,parameters)
        with patch.object(self.db,'execute',side_effect=fail):
            with self.assertRaises(RuntimeError):initialize(self.db)
        self.assertEqual(self.db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0],1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='inbox' AND column_name='chat_id'").fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT text FROM inbox').fetchone()[0],'Synthetic saved conversation')


if __name__=='__main__':
    unittest.main(verbosity=2)
