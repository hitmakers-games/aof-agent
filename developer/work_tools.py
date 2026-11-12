"""Small trusted interface to the one durable work queue."""
import json
import time

from gateway import access, models, work, subscriptions
from telegram.storage import put


def execute(db, actor, chat, action, reference=None, title=None, description='', repositories=None,
            notify=False, topic='mine', minutes=0, events='changes', model_options=None):
    access.require(db,actor,'viewer')
    if action=='list':
        return {'queue':work.listing(db,actor)}
    if action=='status':
        item=work.require_item(db,actor,reference)
        result={'task':item['issue_key'],'state':item['state']}
        if access.allowed(db,actor,'operator'):
            result.update(title=item['title'],progress=item['checkpoint'],result=item['result'])
        return result
    access.require(db,actor,'operator')
    if action in {'watch','unwatch'}:
        return subscriptions.watch(db,actor,chat,reference or topic,action=='watch',minutes,events)
    if action=='start':
        if repositories and set(repositories)&{'aof-agent','aof-infra'}:
            access.require(db,actor,'admin')
        if reference:
            item=work.require_item(db,actor,reference,'operator',lock=True)
            if description and description!=item['description']:
                work.amend(db,actor,item['id'],description)
            elif item['state'] in {'failed','interrupted','cancelled','held'}:
                db.execute("UPDATE work_items SET state='awaiting_approval',held=0,cancel_requested=0 WHERE id=?",(item['id'],))
        else:
            item=work.create(db,actor,title or description[:100],description,shared=True,repositories=repositories)
        work.approve(db,actor,item['id'])
        models.snapshot(db,item['id'],chat,model_options)
        put(db,'work-chat:'+item['id'],chat)
        if notify:subscriptions.watch(db,actor,chat,item['id'],events='finished')
        return {'task':item['issue_key'],'state':work.get(db,item['id'])['state']}
    if action=='cancel':
        item=work.require_item(db,actor,reference,'operator',lock=True)
        if item['state'] in work.TERMINAL:return {'task':item['issue_key'],'state':item['state']}
        work.hold(db,actor,item['id'])
        db.execute('UPDATE work_items SET state=?,held=0,updated_at=? WHERE id=?',
            ('cancelling' if item['state']=='running' else 'cancelled',time.time(),item['id']))
        return {'task':item['issue_key'],'state':'cancellation requested'}
    raise ValueError('Unknown work action')
