import json
import time
import unittest
from unittest.mock import MagicMock
from developer.jira import Jira


class JiraMediaTest(unittest.TestCase):
    def test_upload_uses_multipart_and_no_check_header(self):
        jira=Jira.__new__(Jira)
        jira.config={'apiBaseUrl':'https://example.invalid/rest/api/3'}
        jira.expires=time.time()+600
        jira.token='test-token'
        jira.opener=MagicMock()
        response=jira.opener.open.return_value.__enter__.return_value
        response.read.return_value=json.dumps([{'id':'123'}]).encode()
        result=jira.request('/issue/AOF-1/attachments',method='POST',upload=('screen.png',b'IMAGE'))
        request=jira.opener.open.call_args.args[0]
        self.assertEqual(request.get_header('X-atlassian-token'),'no-check')
        self.assertIn(b'name="file"; filename="screen.png"',request.data)
        self.assertIn(b'IMAGE',request.data)
        self.assertEqual(result[0]['id'],'123')
