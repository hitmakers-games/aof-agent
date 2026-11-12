"""Real Git regressions for ordinary branch changes and credential boundaries."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from coding import git
from developer.git_tools import Repositories, validate_checkout


class GitToolsTest(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.root=Path(self.directory.name)
        self.repo=self.root/'aof-agent'
        self.repo.mkdir()
        self.remote='https://github.com/hitmakers-games/aof-agent.git'
        git.run(['init','-b','master'],self.repo)
        git.run(['config','user.name','Developer'],self.repo)
        git.run(['config','user.email','developer@example.invalid'],self.repo)
        git.run(['remote','add','origin',self.remote],self.repo)
        (self.repo/'source.txt').write_text('first\n')
        git.run(['add','source.txt'],self.repo)
        git.run(['commit','-m','Initial source'],self.repo)
        self.tools=Repositories.__new__(Repositories)
        self.tools.root=self.root
        self.tools.app=Mock()
        self.tools.records=self.root/'control.json'
        self.tools.records.write_text(json.dumps({'aof-agent':{'branch':'master','config':'legacy-checksum'}}))

    def tearDown(self):
        self.directory.cleanup()

    def test_native_branch_tracking_change_can_commit_and_push(self):
        git.run(['switch','-c','fix/group-bot-replies'],self.repo)
        git.run(['config','branch.fix/group-bot-replies.remote','origin'],self.repo)
        git.run(['config','branch.fix/group-bot-replies.merge','refs/heads/master'],self.repo)
        (self.repo/'source.txt').write_text('fixed\n')
        result=self.tools.execute('commit',repository='aof-agent',message='Fix reply handling')
        self.assertEqual(result['commit'],git.run(['rev-parse','HEAD'],self.repo))
        actual_run=git.run
        def no_network(args,*positional,**kwargs):
            return '' if args[0]=='push' else actual_run(args,*positional,**kwargs)
        with patch('developer.git_tools.git.run',side_effect=no_network) as run:
            published=self.tools.execute('push',repository='aof-agent')
        self.assertEqual(published['branch'],'fix/group-bot-replies')
        self.assertTrue(any(call.args[0][-1]=='HEAD:refs/heads/fix/group-bot-replies'
                            for call in run.call_args_list))

    def test_remote_redirection_and_executable_settings_are_blocked(self):
        for key,value in [('url.https://example.invalid/.insteadOf','https://github.com/'),
                          ('include.path','/tmp/untrusted-git-config'),
                          ('core.sshCommand','untrusted-command'),
                          ('filter.source.clean','untrusted-command')]:
            with self.subTest(key=key):
                git.run(['config',key,value],self.repo)
                with self.assertRaises(ValueError):
                    validate_checkout(self.repo,self.remote)
                git.run(['config','--unset',key],self.repo)
        git.run(['remote','set-url','origin','https://example.invalid/aof-agent.git'],self.repo)
        with self.assertRaises(ValueError):
            validate_checkout(self.repo,self.remote)

    def test_changed_author_preferences_do_not_freeze_the_checkout(self):
        git.run(['config','user.name','Updated Developer'],self.repo)
        validate_checkout(self.repo,self.remote)
        self.assertEqual(self.tools.execute('status',repository='aof-agent')['branch'],'master')


if __name__=='__main__':unittest.main()
