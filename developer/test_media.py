"""Attachment workflow boundaries: chat isolation and uncertain external writes."""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from developer import media
from telegram.storage import connect, put


class MediaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = connect(str(Path(self.tmp.name) / 'test.sqlite3'))
        self.jira = Mock()
        self.transfer = patch('developer.media.transfer').start()
        self.addCleanup(patch.stopall)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.db.close)
        with self.db:
            put(self.db, 'telegram-media:10:7', json.dumps([
                {'file_id': 'photo', 'filename': 'screen.png'}]))

    def test_send_preserves_caption_and_does_not_repeat_delivery(self):
        self.jira.attachment.return_value = ({'filename': 'screen.png'}, b'image')
        self.transfer.return_value = {'sent': True, 'message_id': 42}
        for _ in range(2):
            self.assertEqual(media.send(self.db, self.jira, 10, 'request', {'issue': 'AOF-1'})['message_id'], 42)
        self.transfer.assert_called_once_with(self.db, {
            'action': 'send', 'chat': 10, 'filename': 'screen.png',
            'caption': 'AOF-1 — screen.png', 'content': base64.b64encode(b'image').decode()})

    def test_upload_uses_received_filename_and_is_not_repeated_across_requests(self):
        self.transfer.return_value = {'content': base64.b64encode(b'image').decode()}
        self.jira.upload.return_value = [{'id': '123'}]
        for _ in range(2):
            self.assertEqual(media.upload(self.db, self.jira, 10, {'issue': 'AOF-1', 'message_id': 7}), [{'id': '123'}])
        self.jira.upload.assert_called_once_with('AOF-1', 'screen.png', b'image')

    def test_other_chat_and_invalid_index_cannot_download(self):
        for chat, index in [(11, 0), (10, -1), (10, 1)]:
            with self.subTest(chat=chat, index=index), self.assertRaises(ValueError):
                media.upload(self.db, self.jira, chat, {'issue': 'AOF-1', 'message_id': 7, 'index': index})
        self.transfer.assert_not_called()
        self.jira.upload.assert_not_called()

    def test_oversized_file_is_rejected_before_download(self):
        with self.db:
            put(self.db, 'telegram-media:10:7', json.dumps([{'file_size': media.LIMIT + 1}]))
        with self.assertRaises(ValueError):
            media.upload(self.db, self.jira, 10, {'issue': 'AOF-1', 'message_id': 7})
        self.transfer.assert_not_called()

    def test_uncertain_upload_is_not_replayed(self):
        self.transfer.return_value = {'content': ''}
        self.jira.upload.side_effect = OSError('Disconnected after sending')
        args = {'issue': 'AOF-1', 'message_id': 7}
        with self.assertRaises(OSError):
            media.upload(self.db, self.jira, 10, args)
        with self.assertRaises(ValueError):
            media.upload(self.db, self.jira, 10, args)
        self.assertEqual(self.jira.upload.call_count, 1)
