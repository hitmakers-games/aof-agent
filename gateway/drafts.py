"""Reviewable task drafts; the model can draft but cannot authorize."""
import hashlib
import json
import time
import uuid

from gateway import access, buttons, work
from telegram.storage import queue_report


def validate(items):
    if not isinstance(items,list) or not 1<=len(items)<=10:
        raise ValueError('A proposal must contain 1 to 10 tasks.')
    cleaned=[]
    for item in items:
        if not isinstance(item,dict) or set(item)-{'title','description','repositories'}:
            raise ValueError('Only title, description and repositories are allowed in a draft.')
        title,description=item.get('title'),item.get('description','')
        if not isinstance(title,str) or not isinstance(description,str) or not title.strip() or len(title)>200 or len(description)>3000:
            raise ValueError('Draft title/description exceeds its limit.')
        draft={'title':title.strip(),'description':description.strip() or title.strip()}
        if item.get('repositories'):
            from coding.config import repositories
            draft['repositories']=repositories(item['repositories'])
        cleaned.append(draft)
    payload=json.dumps(cleaned,ensure_ascii=False)
    if len(payload.encode())>24000:
        raise ValueError('Draft batch exceeds 24 KiB.')
    return cleaned, payload


def propose(db, actor, items, notify=True, introduction=''):
    access.require(db,actor,'operator')
    cleaned, payload = validate(items)
    request_id=uuid.uuid4().hex
    revision=hashlib.sha256(payload.encode()).hexdigest()
    db.execute('INSERT INTO task_drafts(id,actor_id,payload,revision,created_at) VALUES (?,?,?,?,?)',
               (request_id,actor,payload,revision,time.time()))
    access.audit(db,actor,'draft.proposed',request_id,{'count':len(cleaned)})
    if notify:
        key='proposal:'+request_id
        text=f'Task draft: {len(cleaned)} task(s). Not registered yet. Registration shares task status with the team.\n'
        if introduction:
            text = introduction[:300]+'\n\n'+text
        text+='\n'.join(f'{index+1}. {item["title"]}' for index,item in enumerate(cleaned))
        text=text[:1400]+f'\nFull details: /draft {request_id}'
        markup=buttons.keyboard(db,actor,[('Register (ask first)','register'),('Register and queue','register_run'),('Discard','discard')],
                                {'request':request_id,'revision':revision},key)
        queue_report(db,text,actor,reply_markup=markup,message_key=key)
    return {'id':request_id,'revision':revision}


def confirm(db, actor, request_id, revision, action):
    access.require(db,actor,'operator')
    suffix=getattr(db,'row_lock','')
    row=db.execute('SELECT actor_id,payload,revision,state FROM task_drafts WHERE id=?'+suffix,(request_id,)).fetchone()
    if not row or row[0]!=actor or row[2]!=revision:
        raise ValueError('Draft not found, changed, or access denied.')
    if row[3]!='proposed':
        return 'This draft was already processed.'
    if action=='discard':
        db.execute("UPDATE task_drafts SET state='cancelled' WHERE id=?",(request_id,))
        return 'Draft discarded.'
    if action not in ('register','register_run'):
        raise ValueError('Unsupported draft action.')
    run=action=='register_run'
    db.execute("UPDATE task_drafts SET state='registered',queue_after_register=? WHERE id=?",(int(run),request_id))
    items=json.loads(row[1])
    for position,item in enumerate(items):
        assignment=work.create(db,actor,item['title'],item['description'],'agent',
                               origin_action=request_id+':'+str(position),shared=True,repositories=item.get('repositories'))
        if run:
            work.approve(db,actor,assignment['id'],assignment['revision'])
    access.audit(db,actor,'draft.registered',request_id,{'count':len(items),'queue_after_register':run})
    return f'{len(items)} task(s) registered.' + (' Queued; waiting for the coding worker.' if run else ' Awaiting approval.')


def describe(db, actor, request_id):
    access.require(db,actor,'operator')
    row=db.execute('SELECT actor_id,payload,state FROM task_drafts WHERE id=?',(request_id,)).fetchone()
    if not row or row[0]!=actor:
        raise ValueError('Draft not found or access denied.')
    return f'State: {row[2]}\n'+ '\n\n'.join(f'{i+1}. {item["title"]}\n{item["description"]}'
                                               for i,item in enumerate(json.loads(row[1])))
