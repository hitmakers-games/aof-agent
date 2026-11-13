import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from telegram.media import process_one, context
from telegram.history import record
from telegram.storage import connect, put, setting
from gateway import access


class MediaTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=connect(str(Path(self.tmp.name)/'test.sqlite3'))
        with self.db:
            put(self.db,'owner',10)
            access.ensure_owner(self.db)
        self.api=Mock()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def job(self,key,**values):
        with self.db:
            put(self.db,'telegram-media-job:'+key,json.dumps({'state':'pending','created':time.time(),**values}))

    def test_send_uses_gateway_upload_and_records_delivery(self):
        self.api.call.return_value={'message_id':42}
        self.job('send',action='send',chat=10,filename='screen.png',caption='AOF-1',content=base64.b64encode(b'png').decode())
        self.assertTrue(process_one(self.db,self.api))
        self.assertEqual(self.api.call.call_args.kwargs['_upload'],('document','screen.png',b'png'))
        self.assertEqual(json.loads(setting(self.db,'telegram-media-job:send'))['result']['message_id'],42)

    def test_uncertain_send_is_never_replayed(self):
        self.api.call.side_effect=OSError()
        self.job('send',action='send',chat=10,filename='x',caption='',content='')
        process_one(self.db,self.api)
        process_one(self.db,self.api)
        self.assertEqual(self.api.call.call_count,1)

    def test_completed_job_does_not_block_later_download(self):
        self.job('a',state='completed')
        self.job('b',action='download',file_id='file')
        self.api.download.return_value=b'image'
        self.assertTrue(process_one(self.db,self.api))
        self.assertEqual(json.loads(setting(self.db,'telegram-media-job:b'))['result']['content'],'aW1hZ2U=')

    def test_photo_without_caption_is_retained_and_exposed_in_context(self):
        message={'chat':{'id':10},'from':{'id':10},'message_id':7,'photo':[{'file_id':'small'},{'file_id':'large'}]}
        with self.db: record(self.db,message)
        self.assertEqual(json.loads(setting(self.db,'telegram-media:10:7'))[0]['file_id'],'large')
        self.assertIn('jira_upload',context(message))

    def test_unknown_private_chat_cannot_store_files(self):
        with self.db: record(self.db,{'chat':{'id':20},'from':{'id':20},'message_id':7,'document':{'file_id':'secret'}})
        self.assertIsNone(setting(self.db,'telegram-media:20:7'))
