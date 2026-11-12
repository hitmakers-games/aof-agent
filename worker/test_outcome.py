import json
from pathlib import Path
import tempfile
import unittest

from coding import git,queue
from developer import artifacts
from developer.work_tools import execute
from gateway import work
from telegram.storage import connect,put
from worker.outcome import verify,deployment_finished


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.db=connect(self.root/'state.sqlite3')
        self.addCleanup(self.db.close)
        with self.db:
            put(self.db,'owner',10);put(self.db,'coding_enabled',1)
            self.item=work.create(self.db,10,'Fix the task list',repositories=['aof-agent'])
            work.approve(self.db,10,self.item['id'])
        self.response=json.dumps({'outcome':'completed','summary':'Implemented','tests':[],'limitations':[]})

    def test_final_answer_with_no_files_cannot_be_completed_code(self):
        state,_=verify(self.db,self.item,self.response)
        self.assertEqual(state,'blocked')

    def test_real_diff_is_evidence_but_not_evidence_of_a_push(self):
        root=self.root/'aof-agent';root.mkdir()
        git.run(['init','-q'],root)
        git.run(['config','user.name','Test'],root)
        git.run(['config','user.email','test@example.invalid'],root)
        git.run(['remote','add','origin','https://github.com/hitmakers-games/aof-agent.git'],root)
        (root/'example.py').write_text('value=1\n')
        git.run(['add','example.py'],root);git.run(['commit','-qm','Initial'],root)
        artifacts.checkout(self.db,self.item['id'],'aof-agent',root)
        (root/'example.py').write_text('value=2\n')
        self.assertEqual(verify(self.db,self.item,self.response)[0],'completed')
        with self.db:put(self.db,'work-delivery:'+self.item['id'],'published')
        self.assertEqual(verify(self.db,self.item,self.response)[0],'blocked')

    def test_deployment_stays_in_progress_until_the_operation_finishes(self):
        item=queue.claim(self.db)
        with self.db:
            put(self.db,'work-delivery:'+item['id'],'deployed')
            put(self.db,'request-operation:'+item['id'],'aof-op-123456789abc')
            put(self.db,'operation:aof-op-123456789abc',json.dumps({'state':'queued'}))
        state,summary=verify(self.db,item,self.response)
        self.assertEqual(state,'deploying')
        queue.finish(self.db,item,state,summary)
        queue.recover(self.db)
        self.assertEqual(work.get(self.db,item['id'])['state'],'deploying')
        deployment_finished(self.db,{'id':'aof-op-123456789abc','request_id':item['id']},True,'Deployment completed')
        self.assertEqual(work.get(self.db,item['id'])['state'],'completed')

    def test_same_request_retry_does_not_create_another_task(self):
        with self.db:
            first=execute(self.db,10,10,'start',title='Another fix',description='Details',request_id='request-1')
            second=execute(self.db,10,10,'start',title='Another fix',description='Details',request_id='request-1')
        self.assertEqual(first['task'],second['task'])
        self.assertTrue(second['existing'])
