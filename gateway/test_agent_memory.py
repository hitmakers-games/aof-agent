"""Knowledge routing, outage recovery and conditional writes without live services."""
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from gateway import agent_memory as memory
from repositories.github_app import AccessError


def file(text, sha='current'):
    return {'type': 'file', 'encoding': 'base64', 'size': len(text.encode()),
            'sha': sha, 'content': base64.b64encode(text.encode()).decode()}


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.object(memory, 'CACHE', Path(directory.name)/'context.json')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = Mock()
        self.app.token.return_value = 'test-token'
        patcher = patch.object(memory, 'GitHubApp', return_value=self.app)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_refresh_keeps_unavailable_repo_but_updates_other_indexes_and_rules(self):
        memory.CACHE.write_text(json.dumps({'md-common/README.md': 'last good'}))
        def request(path, token):
            if '/md-common/' in path:
                raise AccessError('unavailable')
            return file('updated')
        self.app.request.side_effect = request
        memory.refresh()
        context = memory.context()
        self.assertEqual(context['md-common/README.md'], 'last good')
        self.assertIn('stale', context['md-common/status'])
        self.assertEqual(context['md-aof/README.md'], 'updated')
        self.assertEqual(context['requirements.md'], 'updated')
        self.assertIn('Fetched at', context['md-aof/status'])

    def test_missing_cache_is_explicit_and_viewer_gets_no_knowledge(self):
        self.assertIn('Unavailable', memory.context()['md-aof/README.md'])
        memory.CACHE.write_text(json.dumps({'identity.md': 'identity',
                                           'md-aof/README.md': 'private project index'}))
        self.assertEqual(memory.context(include_knowledge=False), {'identity.md': 'identity'})

    def test_read_rejects_other_repos_traversal_and_non_files(self):
        for repository, path in [('aof-agent', 'agent/memory.md'),
                                 ('md-aof', '../secrets.md'), ('md-aof', '/dev.md')]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                memory.read(repository, path)
        self.app.request.assert_not_called()
        self.app.request.return_value = {'type': 'symlink'}
        with self.assertRaises(ValueError):
            memory.read('md-aof', 'dev.md')

    def test_remember_uses_chosen_document_latest_sha_and_preserves_content(self):
        self.app.request.side_effect = [file('# Development\nExisting fact\n', 'latest-sha'),
                                       {'commit': {'html_url': 'https://example.invalid/commit'}}]
        result = memory.remember('Verified fact', 'md-aof', 'dev.md')
        call = self.app.request.call_args
        self.assertEqual(call.args[0], '/repos/hitmakers-games/md-aof/contents/dev.md')
        body = call.args[2]
        self.assertEqual(body['sha'], 'latest-sha')
        self.assertEqual(body['branch'], 'master')
        self.assertEqual(base64.b64decode(body['content']).decode(),
                         '# Development\nExisting fact\n\n- Verified fact\n')
        self.assertIn('commit', result)
        self.app.token.assert_any_call('md-aof', {'contents': 'write'})

    def test_owner_files_indexes_and_missing_targets_cannot_be_appended(self):
        for path, text in [('ux.md', 'Документ ведёт владелец.'),
                           ('README.md', '# Index'), ('personas/example.md', '# Persona')]:
            self.app.request.return_value = file(text)
            with self.subTest(path=path), self.assertRaises(ValueError):
                memory.remember('A fact', 'md-common', path)
        with self.assertRaises(ValueError):
            memory.remember('A fact')
        self.assertFalse(any(c.kwargs.get('method') == 'PUT'
                             for c in self.app.request.call_args_list))

    def test_duplicate_fact_does_not_write_and_conflicts_are_not_retried(self):
        self.app.request.return_value = file('Already known')
        self.assertEqual(memory.remember('Already known', 'md-common', 'engineering.md'),
                         {'status': 'already remembered'})
        self.app.request.reset_mock()
        self.app.request.side_effect = [file('Existing'), AccessError('HTTP 409')]
        with self.assertRaises(AccessError):
            memory.remember('New fact', 'md-common', 'engineering.md')
        self.assertEqual(self.app.request.call_count, 2)


if __name__ == '__main__':
    unittest.main()
