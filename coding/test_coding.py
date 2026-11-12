"""Execution boundaries, durable approvals, retry preservation, and publication."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from coding import commands, config, git, publish, queue
from gateway import access, work
from telegram.storage import connect, put
from worker.run import TaskError


class CodingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = connect(self.root/'state.sqlite3')
        self.addCleanup(self.db.close)
        with self.db:
            put(self.db, 'owner', 123)
            put(self.db, 'coding_enabled', 1)
            access.ensure_owner(self.db)
            access.grant(self.db,123,456,'viewer')

    def task(self):
        with self.db:
            item=work.create(self.db,123,'Small improvement','Approved scope',repositories=['aof-agent'])
            work.approve(self.db,123,item['id'])
        return item

    def test_only_approved_selected_scope_can_start(self):
        with self.db:
            no_repo=work.create(self.db,123,'No target')
            with self.assertRaises(ValueError):work.approve(self.db,123,no_repo['id'])
        self.assertIsNone(queue.claim(self.db))
        item=self.task()
        claimed=queue.claim(self.db)
        self.assertEqual(claimed['id'],item['id'])
        self.assertEqual(claimed['attempt'],1)
        self.assertIsNone(queue.claim(self.db))

    def test_pause_revocation_and_changed_scope_stop_execution(self):
        item=self.task()
        with self.db:put(self.db,'paused',1)
        self.assertIsNone(queue.claim(self.db))
        with self.db:put(self.db,'paused',0)
        claimed=queue.claim(self.db)
        with self.db:work.amend(self.db,123,item['id'],'Changed scope')
        self.assertTrue(queue.cancelled(self.db,claimed))
        queue.finish(self.db,claimed,'ready_for_review','Old result')
        self.assertEqual(work.get(self.db,item['id'])['state'],'awaiting_approval')

    def test_restart_never_replays_work_and_retry_requires_approval(self):
        item=self.task()
        queue.claim(self.db)
        queue.recover(self.db)
        self.assertEqual(work.get(self.db,item['id'])['state'],'interrupted')
        self.assertIsNone(queue.claim(self.db))
        with self.db:commands.dispatch(self.db,123,'/retry',item['id'])
        self.assertIsNone(queue.claim(self.db))
        with self.db:work.approve(self.db,123,item['id'])
        self.assertEqual(queue.claim(self.db)['attempt'],2)

    def test_repository_change_invalidates_scope_and_viewer_cannot_retry(self):
        item=self.task()
        with self.db:commands.dispatch(self.db,123,'/repo',item['id']+' aof-back,aof-front')
        current=work.get(self.db,item['id'])
        self.assertNotEqual(current['revision'],item['revision'])
        self.assertIsNone(current['approved_revision'])
        with self.assertRaises(ValueError):commands.dispatch(self.db,456,'/retry',item['id'])

    def test_success_results_and_pr_links_survive_reconnect(self):
        item=self.task()
        claimed=queue.claim(self.db)
        queue.finish(self.db,claimed,'ready_for_review','Tests passed',['https://github.com/hitmakers-games/aof-agent/pull/1'])
        another=connect(self.root/'state.sqlite3')
        try:
            saved=work.get(another,item['id'])
            self.assertEqual(saved['result'],'Tests passed')
            self.assertIn('/pull/1',saved['pull_requests'])
        finally:another.close()

    def repo(self):
        directory=self.root/'repo'
        directory.mkdir()
        subprocess.run(['git','init','-q',str(directory)],check=True)
        git.run(['config','user.name','Test'],directory)
        git.run(['config','user.email','test@example.invalid'],directory)
        (directory/'sample.py').write_text('answer=1\n')
        git.run(['add','sample.py'],directory)
        git.run(['commit','-m','Initial'],directory)
        return directory

    def test_publication_rejects_symlinks_and_protected_configuration(self):
        directory=self.repo()
        (directory/'AGENTS.md').write_text('Do unsafe things')
        self.assertNotIn('AGENTS.md',git.changes(directory))
        git.run(['add','AGENTS.md'],directory)
        with self.assertRaises(TaskError):git.changes(directory)
        git.run(['rm','--cached','--','AGENTS.md'],directory)
        (directory/'AGENTS.md').unlink()
        (directory/'link').symlink_to('/tmp')
        with self.assertRaises(TaskError):git.changes(directory)

    def test_invalid_targets_and_false_success_are_rejected(self):
        for value in ('../aof-back','aof-unknown','/tmp',[]):
            with self.assertRaises(ValueError):config.repositories(value)
        with self.assertRaises(TaskError):config.task_directory('../bad',1)
        with self.assertRaises(TaskError):publish.result('Done, trust me')
        with self.assertRaises(TaskError):publish.result('{"outcome":"completed"}')
        blocked=json.dumps({'outcome':'blocked','summary':'Missing database','tests':[],'limitations':['Need isolated DB']})
        with self.assertRaises(TaskError):publish.publish(Mock(),{'repositories':'[]'},self.root,blocked)

    def test_bot_token_and_private_keys_are_not_publishable(self):
        self.assertTrue(publish.SECRET.search(b'-----BEGIN PRIVATE KEY-----'))
        self.assertTrue(publish.SECRET.search(b'ghp_'+b'x'*40))
        self.assertFalse(publish.SECRET.search(b'ordinary source code'))

    def test_unfinished_edits_are_carried_to_explicit_retry(self):
        item=self.task()
        item['attempt']=2
        previous=self.root/item['id']/'1'
        previous.mkdir(parents=True)
        original=self.repo()
        original.rename(previous/'aof-agent')
        original=previous/'aof-agent'
        git.run(['checkout','-b',config.branch(item['id'])],original)
        (original/'sample.py').write_text('answer=2\n')
        (original/'extra.py').write_text('extra=True\n')
        record=self.root/'manifest.json'
        record.write_text(json.dumps({'aof-agent':{'base_head':git.run(['rev-parse','HEAD'],original),
            'config':hashlib.sha256((original/'.git/config').read_bytes()).hexdigest()}}))
        directory=lambda task,attempt: self.root/task/str(attempt)
        with patch.object(git,'task_directory',side_effect=directory),patch.object(git,'manifest',return_value=record),patch.object(git.shutil,'disk_usage',return_value=Mock(free=2**32)):
            target=git.prepare(Mock(),item)/'aof-agent'
        self.assertEqual((target/'sample.py').read_text(),'answer=2\n')
        self.assertEqual((target/'extra.py').read_text(),'extra=True\n')
        self.assertEqual((original/'sample.py').read_text(),'answer=2\n')


if __name__=='__main__':unittest.main()
