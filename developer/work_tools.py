"""Small trusted interface to the one durable work queue."""
import json
import re
import hashlib
import time

from gateway import access, models, work, subscriptions
from telegram.storage import put


GENERIC_TITLES = {
    'task', 'new task', 'follow up', 'follow-up', 'work', 'request',
}


def task_title(title, description):
    """Keep queue titles specific; infer a short action when a caller omitted one."""
    candidate = title.strip() if isinstance(title, str) else ''
    if candidate and len(candidate) <= 120 and candidate.casefold() not in GENERIC_TITLES:
        return candidate
    request = description.strip()
    # Requests are often written as an imperative. Use one concise action phrase,
    # rather than blindly copying the first N characters of a long prompt.
    first = re.split(r'(?<=[.!?])\s+|\n', request, maxsplit=1)[0].strip()
    first = re.sub(r'^(please\s+)?(can you|could you|would you|please)\s+', '', first, flags=re.I)
    first = re.sub(r'\s+', ' ', first).strip(' .!?')
    if not first:
        return 'Clarify requested change'
    words = first.split()
    if len(words) > 12:
        first = ' '.join(words[:12]).rstrip(',;:')
    return first[:120].rstrip(' ,;:') or 'Clarify requested change'


def execute(db, actor, chat, action, reference=None, title=None, description='', repositories=None,
            notify=False, topic='mine', minutes=0, events='changes', model_options=None, delivery=None, request_id=None):
    access.require(db,actor,'viewer')
    if action=='list':
        return {'queue':work.listing(db,actor)}
    if action=='status':
        item=work.require_item(db,actor,reference)
        key=item['issue_key'] if access.allowed(db,actor,'operator') or item['source']=='jira' else item['id']
        result={'task':key,'state':item['state']}
        if access.allowed(db,actor,'operator'):
            result.update(title=item['title'],progress=item['checkpoint'],result=item['result'])
        return result
    access.require(db,actor,'operator')
    if action in {'watch','unwatch'}:
        return subscriptions.watch(db,actor,chat,reference or topic,action=='watch',minutes,events)
    if action=='start':
        if not reference:
            title=task_title(title, description)
        expected=delivery or ('changes' if repositories else 'answer')
        if expected not in {'answer','changes','published','deployed'}:raise ValueError('Invalid expected result')
        origin=None
        if request_id:
            fingerprint=hashlib.sha256(json.dumps([reference,title,description,repositories,expected],sort_keys=True).encode()).hexdigest()
            origin='work-start:'+str(request_id)+':'+fingerprint
            previous=db.execute('SELECT id FROM work_items WHERE origin_action=?',(origin,)).fetchone()
            if previous:
                existing=work.get(db,previous[0])
                return {'task':existing['issue_key'],'state':existing['state'],'existing':True}
        if repositories and set(repositories)&{'aof-agent','aof-infra'}:
            access.require(db,actor,'admin')
        if reference:
            item=work.require_item(db,actor,reference,'operator',lock=True)
            if description and description!=item['description']:
                work.amend(db,actor,item['id'],description)
            elif item['state'] in {'failed','blocked','interrupted','cancelled','held'}:
                db.execute("UPDATE work_items SET state='awaiting_approval',held=0,cancel_requested=0 WHERE id=?",(item['id'],))
        else:
            if getattr(db,'row_lock',''):db.execute('SELECT pg_advisory_xact_lock(3350007)')
            duplicate=db.execute("""SELECT id FROM work_items WHERE requester_id=? AND title=?
                AND state IN ('queued','running','cancelling','deploying') ORDER BY created_at LIMIT 1""",(actor,title or description[:100])).fetchone()
            if duplicate:
                previous=work.get(db,duplicate[0])
                return {'task':previous['issue_key'],'state':previous['state'],'existing':True}
            item=work.create(db,actor,title or description[:100],description,shared=True,repositories=repositories,origin_action=origin)
        work.approve(db,actor,item['id'])
        models.snapshot(db,item['id'],chat,model_options)
        put(db,'work-chat:'+item['id'],chat)
        put(db,'work-delivery:'+item['id'],expected)
        if notify:subscriptions.watch(db,actor,chat,item['id'],events='finished')
        return {'task':item['issue_key'],'state':work.get(db,item['id'])['state']}
    if action=='cancel':
        item=work.require_item(db,actor,reference,'operator',lock=True)
        if item['state']=='deploying':
            raise ValueError('A deployment is already running. Inspect its operation before stopping or rolling it back; cancelling the task alone would not stop the deployment.')
        if item['state'] in work.TERMINAL:return {'task':item['issue_key'],'state':item['state']}
        work.hold(db,actor,item['id'])
        db.execute('UPDATE work_items SET state=?,held=0,updated_at=? WHERE id=?',
            ('cancelling' if item['state']=='running' else 'cancelled',time.time(),item['id']))
        return {'task':item['issue_key'],'state':'cancellation requested'}
    if action=='delete':
        return work.delete(db,actor,reference)
    raise ValueError('Unknown work action')
