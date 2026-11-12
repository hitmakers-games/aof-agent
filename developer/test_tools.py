"""Checks for conversation privacy and repeated external writes."""
import tempfile
import unittest
from pathlib import Path

from developer.effects import once
from developer.kubernetes import Kubernetes, clean
from gateway import access
from telegram.history import read, record, chats
from telegram.storage import connect, put


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = connect(str(Path(self.directory.name) / 'state.sqlite3'))
        with self.db:
            put(self.db, 'owner', 10)
            put(self.db, 'group:-20:enabled', 1)
            access.grant(self.db, 10, 11, 'operator')

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def message(self, chat, text, edited=False):
        message = {'chat': {'id': chat}, 'from': {'id': 10, 'first_name': 'Owner'},
                   'message_id': 1, 'date': 100, 'text': text}
        if edited:
            message['edit_date'] = 101
        with self.db:
            record(self.db, message)

    def test_unmentioned_group_message_and_edit_are_searchable(self):
        self.message(-20, 'first message')
        self.message(-20, 'updated message', edited=True)
        result = read(self.db, 10, -20, query='updated')
        self.assertEqual(len(result['messages']), 1)
        self.assertEqual(result['messages'][0]['text'], 'updated message')

    def test_private_history_cannot_be_disclosed_into_group(self):
        self.message(10, 'private information')
        with self.assertRaises(ValueError):
            read(self.db, 11, -20, chat_id=11)

    def test_other_users_private_history_is_denied(self):
        with self.assertRaises(ValueError):
            read(self.db, 11, 11, chat_id=10)

    def test_admin_can_find_and_read_other_retained_chats(self):
        self.message(11, 'requested conversation')
        self.assertIn(11, [item['chat_id'] for item in chats(self.db, 10, 10)['chats']])
        self.assertEqual(read(self.db, 10, 10, chat_id=11)['messages'][0]['text'], 'requested conversation')

    def test_history_can_be_paged_without_repeating_messages(self):
        self.message(-20, 'one')
        with self.db:
            record(self.db, {'chat': {'id': -20}, 'from': {'id': 10}, 'message_id': 2, 'text': 'two'})
        first = read(self.db, 10, -20, limit=1)
        second = read(self.db, 10, -20, limit=1, before_message_id=first['next_before_message_id'])
        self.assertEqual(first['messages'][0]['text'], 'two')
        self.assertEqual(second['messages'][0]['text'], 'one')

    def test_retry_returns_saved_jira_result(self):
        calls = []
        def create():
            calls.append(True)
            return {'key': 'AOF-1'}
        self.assertEqual(once(self.db, 1, 'jira.create', {}, create), {'key': 'AOF-1'})
        self.assertEqual(once(self.db, 1, 'jira.create', {}, create), {'key': 'AOF-1'})
        self.assertEqual(len(calls), 1)

    def test_uncertain_write_is_not_repeated(self):
        def disconnected():
            raise OSError('Connection lost after sending request')
        with self.assertRaises(OSError):
            once(self.db, 2, 'jira.create', {}, disconnected)
        with self.assertRaises(ValueError):
            once(self.db, 2, 'jira.create', {}, lambda: {'key': 'AOF-2'})

    def test_jira_status_records_api_success_and_failure_without_error_details(self):
        from unittest.mock import Mock
        from developer.jira import Jira
        from telegram.storage import setting
        jira = Jira.__new__(Jira)
        jira.db = self.db
        jira._request = Mock(return_value={'accountId': 'private-account'})
        self.assertEqual(jira.request('/myself'), {'accountId': 'private-account'})
        self.assertEqual(setting(self.db, 'jira_last_outcome'), 'successful')
        self.assertIsNotNone(setting(self.db, 'jira_last_request'))
        jira._request.side_effect = OSError('private-error')
        with self.assertRaises(OSError):
            jira.request('/myself')
        self.assertEqual(setting(self.db, 'jira_last_outcome'), 'failed')
        self.assertNotIn('private-', str(self.db.execute(
            "SELECT value FROM settings WHERE key LIKE 'jira_%'").fetchall()))

    def test_host_access_is_rejected_and_secret_values_are_hidden(self):
        with self.assertRaises(ValueError):
            Kubernetes.safe_workload({'spec': {'volumes': [{'hostPath': {'path': '/'}}]}})
        self.assertNotIn('private-value', str(clean({'kind': 'Secret', 'data': {'token': 'private-value'}})))


if __name__ == '__main__':
    unittest.main()
