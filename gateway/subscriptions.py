"""Opt-in task reports stored in existing settings; one digest per destination."""
import json
import time

from gateway import access, work
from telegram.storage import put, queue_report, setting


def watch(db, actor, chat, topic, enabled=True, minutes=0):
    access.require(db, actor, 'operator')
    if topic not in {'all','mine','jira'}:
        topic = work.require_item(db,actor,topic,'operator')['id']
    if topic=='all':
        access.require(db,actor,'admin')
    if type(minutes) is not int or minutes<0 or (minutes and not 5<=minutes<=1440):
        raise ValueError('Reports can repeat every 5 to 1440 minutes, or on changes only.')
    key=f'subscription:{actor}:{chat}:{topic}'
    put(db,key,json.dumps({'actor':actor,'chat':chat,'topic':topic,'enabled':bool(enabled),
        'minutes':minutes,'next':time.time()+max(30,minutes*60),'previous':None}))
    return {'topic':topic,'enabled':bool(enabled),'minutes':minutes}


def flush(db, now=None):
    now=now or time.time()
    rows=db.execute("SELECT key,value FROM settings WHERE key LIKE 'subscription:%'").fetchall()
    destinations={}
    for key,value in rows:
        entry=json.loads(value)
        if not entry['enabled'] or entry['next']>now or not access.allowed(db,entry['actor'],'operator'):
            continue
        topic=entry['topic']
        items=[]
        for (uid,) in db.execute('SELECT id FROM work_items ORDER BY updated_at DESC LIMIT 100'):
            item=work.get(db,uid)
            if not work.visible(db,entry['actor'],item):continue
            if topic=='mine' and item['requester_id']!=entry['actor']:continue
            if topic=='jira' and item['source']!='jira':continue
            if topic not in {'all','mine','jira'} and uid!=topic:continue
            items.append((item['issue_key'],item['state'],item['checkpoint']))
        rendered=json.dumps(items,ensure_ascii=False)
        if items and (rendered!=entry['previous'] or entry['minutes']):
            lines=destinations.setdefault((entry['actor'],entry['chat']),set())
            lines.update(f'{key}: {state}'+(f' — {progress}' if progress and progress!=state else '') for key,state,progress in items[:15])
        entry.update(previous=rendered,next=now+max(30,entry['minutes']*60))
        put(db,key,json.dumps(entry))
    for (actor,chat),lines in destinations.items():
        queue_report(db,'\n'.join(sorted(lines))[:3500],chat,authorized_by=actor,required_role='operator')
