"""Jira/Telegram attachment workflows, called after dispatcher authorization."""
import base64
import json

from developer.effects import once
from telegram.media import LIMIT, transfer
from telegram.storage import setting


def send(db, jira, chat, request_id, args):
    file,content=jira.attachment(**args)
    def deliver():
        return transfer(db,{'action':'send','chat':chat,'filename':file['filename'],
            'caption':args['issue']+' — '+file['filename'],'content':base64.b64encode(content).decode()})
    return once(db,request_id,'telegram.jira_attachment',args,deliver)


def upload(db, jira, chat, args):
    files=json.loads(setting(db,f"telegram-media:{chat}:{args['message_id']}",'[]'))
    index=args.get('index',0)
    if index<0 or index>=len(files):raise ValueError('No attachment at that message/index in this chat.')
    file=files[index]
    if file.get('file_size',0)>LIMIT:raise ValueError('Attachment exceeds 20 MiB.')
    jira.execute('get',issue=args['issue'])
    content=transfer(db,{'action':'download','file_id':file['file_id']})
    return once(db,'telegram:'+str(chat),'jira.upload',args,
        lambda:jira.upload(args['issue'],file['filename'],base64.b64decode(content['content'])))
