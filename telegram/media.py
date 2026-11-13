"""Bounded media transfers through the gateway that owns the Telegram token."""
import base64
import json
import time
import uuid
from telegram.storage import put, setting

LIMIT = 20 * 1024 * 1024


def transfer(db, payload):
    key = 'telegram-media-job:' + uuid.uuid4().hex
    with db:
        put(db, key, json.dumps({'state':'pending', 'created':time.time(), **payload}))
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        value = json.loads(setting(db, key, '{}'))
        if value.get('state') in {'completed','failed'}:
            with db:
                db.execute('DELETE FROM settings WHERE key=?', (key,))
            if value['state']=='failed':
                raise ValueError(value['error'])
            return value['result']
        time.sleep(.5)
    raise ValueError('Telegram transfer timed out; delivery may be uncertain. Do not repeat automatically.')


def process_one(db, api):
    rows = db.execute("SELECT key,value FROM settings WHERE key LIKE 'telegram-media-job:%' ORDER BY key").fetchall()
    selected=None
    for key,raw in rows:
        job=json.loads(raw)
        if job['state']=='pending':
            selected=(key,job)
            break
        if time.time()-job['created']>300:
            with db: db.execute('DELETE FROM settings WHERE key=?',(key,))
    if selected is None: return False
    key,job=selected
    with db: put(db,key,json.dumps({**job,'state':'sending'}))
    try:
        if time.time()-job['created'] > 120:
            raise ValueError('Expired Telegram transfer was not replayed.')
        if job['action']=='send':
            content=base64.b64decode(job['content'],validate=True)
            if len(content)>LIMIT: raise ValueError('Attachment exceeds 20 MiB.')
            result=api.call('sendDocument',chat_id=job['chat'],caption=job['caption'],
                            _upload=('document',job['filename'],content))
            result={'sent':True,'message_id':result['message_id']}
        else:
            content=api.download(job['file_id'])
            result={'content':base64.b64encode(content).decode()}
        value={'state':'completed','created':job['created'],'result':result}
    except Exception:
        value={'state':'failed','created':job['created'],'error':'Telegram file transfer failed; a send may have succeeded. Check the chat before retrying.'}
    with db: put(db,key,json.dumps(value))
    return True


def references(message):
    files=[]
    photos=message.get('photo') or []
    if photos:
        files.append({**photos[-1], 'filename':'screenshot-'+str(message['message_id'])+'.jpg'})
    document=message.get('document')
    if document:
        files.append({**document,'filename':document.get('file_name','attachment')})
    return [{k:f[k] for k in ('file_id','filename','file_size') if k in f} for f in files]


def retain(db,message):
    files=references(message)
    if files:
        put(db,f"telegram-media:{message['chat']['id']}:{message['message_id']}",json.dumps(files))
    return files


def context(message):
    files=references(message)
    reply=message.get('reply_to_message') or {}
    if not files and references(reply):
        message=reply
        files=references(reply)
    if not files: return ''
    return '\nTelegram attachments: '+json.dumps({'message_id':message['message_id'],'files':files})+'\nUse jira_upload to attach these files to the requested Jira issue.'
