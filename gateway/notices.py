"""Coalesce task events into short digests; update existing status messages."""
import json
import time
import uuid

from gateway import access, buttons, work
from telegram.storage import queue_report, setting


def recipients(db, item):
    if item['visibility'] == 'private':
        return [item['requester_id']] if access.allowed(db, item['requester_id'], 'operator') else []
    owner = setting(db, 'owner')
    people = {int(owner)} if owner else set()
    if access.allowed(db,item['requester_id'],'operator'):
        people.add(item['requester_id'])
    for (user,) in db.execute("SELECT user_id FROM members WHERE active=1 AND role IN ('operator','admin')"):
        if setting(db, f'watch:{user}', 'off') == 'on':
            people.add(user)
    return sorted(people)


def add(db, work_id, kind='intake', now=None):
    now = time.time() if now is None else now
    item = work.get(db, work_id)
    # The lock serializes short batching decisions, never network/model calls.
    if getattr(db, 'row_lock', ''):
        db.execute('SELECT pg_advisory_xact_lock(3350002)')
    for actor in recipients(db, item):
        row = db.execute('''SELECT id FROM notice_batches WHERE recipient=? AND kind=? AND published=0
                             AND due_at>? ORDER BY due_at LIMIT 1''', (actor, kind, now)).fetchone()
        batch_id = row[0] if row else uuid.uuid4().hex
        if row is None:
            db.execute('INSERT INTO notice_batches(id,recipient,kind,due_at) VALUES (?,?,?,?)',
                       (batch_id, actor, kind, now+30))
        db.execute('''INSERT INTO notice_items VALUES (?,?,?) ON CONFLICT(batch_id,work_id)
                      DO UPDATE SET revision=excluded.revision''', (batch_id, work_id, item['revision']))


def flush(db, now=None):
    now = time.time() if now is None else now
    rows = db.execute('''SELECT id,recipient,kind,published,last_render FROM notice_batches
                         WHERE due_at<=? AND (published=0 OR (due_at>? AND EXISTS (
                           SELECT 1 FROM notice_items n JOIN work_items w ON w.id=n.work_id
                           WHERE n.batch_id=notice_batches.id AND w.updated_at>notice_batches.rendered_at)))
                         ORDER BY published,due_at LIMIT 50''',
                      (now, now-86400)).fetchall()
    for batch_id, actor, kind, published, previous in rows:
        with db:
            current = db.execute('SELECT published,last_render,version FROM notice_batches WHERE id=?'+getattr(db,'row_lock',''), (batch_id,)).fetchone()
            published, previous, version = current
            entries = db.execute('SELECT work_id,revision FROM notice_items WHERE batch_id=? ORDER BY work_id', (batch_id,)).fetchall()
            items = [work.get(db, uid) for uid, _ in entries]
            items = [item for item in items if work.visible(db, actor, item)]
            if not items or not access.allowed(db, actor, 'operator'):
                db.execute('UPDATE notice_batches SET published=1 WHERE id=?', (batch_id,))
                continue
            states = {state: sum(item['state']==state for item in items) for state in {i['state'] for i in items}}
            summary = ', '.join(f'{count} {state}' for state, count in sorted(states.items()))
            text = f"{'Received' if kind=='intake' else 'Task update'} {len(items)} task(s)\n{summary}\n"
            text += '\n'.join(f"{i['issue_key'] or i['id']}: {i['title'][:90]}" for i in items[:10])
            if len(items)>10:
                text += f'\nAnd {len(items)-10} more. Use /work.'
            if states.get('queued'):
                text += '\nQueued work is waiting for the coding worker.'
            text = text[:1500]
            if text == previous:
                db.execute('UPDATE notice_batches SET rendered_at=? WHERE id=?',(now,batch_id))
                continue
            key = 'batch:'+batch_id
            markup = None
            if not published:
                snapshot = {'items': [{'id': i['id'], 'revision': i['revision']} for i in items]}
                markup = buttons.keyboard(db, actor, [('Start pending batch','approve'),('View','view'),('Hold','hold')], snapshot, key)
            queue_report(db, text, actor, event_key=f'{key}:v{version+1}', reply_markup=markup, message_key=key)
            db.execute('UPDATE notice_batches SET published=1,last_render=?,version=?,rendered_at=? WHERE id=?',
                       (text,version+1,now,batch_id))
