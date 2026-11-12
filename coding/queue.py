"""Transactional execution claims, cancellation, and durable results."""
import json
import time
import uuid

from gateway import access, work
from gateway.notices import add
from telegram.storage import put, queue_report, setting


def claim(db):
    with db:
        if getattr(db,'row_lock',''):
            db.execute('SELECT pg_advisory_xact_lock(3350005)')
        if setting(db, 'coding_enabled', '0') != '1' or setting(db, 'paused', '0') == '1' or setting(db,'self_deploy',''):
            return None
        row = db.execute("""SELECT id FROM work_items WHERE state='queued'
            AND approved_revision=revision AND held=0 AND cancel_requested=0
            ORDER BY created_at LIMIT 1""" + getattr(db, 'claim_lock', '')).fetchone()
        if not row:
            return None
        item = work.get(db, row[0])
        if not access.allowed(db, item['approved_by'], 'operator'):
            db.execute("UPDATE work_items SET state='awaiting_approval',approved_revision=NULL WHERE id=?", row)
            return None
        attempt, now, run_id = item['attempt'] + 1, time.time(), uuid.uuid4().hex
        db.execute("""UPDATE work_items SET state='running',attempt=?,checkpoint='Preparing isolated checkout',
                      result=NULL,updated_at=? WHERE id=?""", (attempt, now, item['id']))
        db.execute('INSERT INTO coding_runs VALUES (?,?,?,?,?,?,?,?)',
                   (run_id, item['id'], attempt, item['revision'], 'preparing', None, now, now))
        item.update(attempt=attempt, run_id=run_id)
        from gateway.models import for_request
        item['model_options']=for_request(db,item['id'],item['requester_id'])
        return item


def cancelled(db, item):
    current = work.get(db, item['id'])
    return bool(not current or current['state'] != 'running' or current['cancel_requested']
                or current['revision'] != item['revision'] or current['approved_revision'] != item['revision']
                or not access.allowed(db, item['approved_by'], 'operator'))


def checkpoint(db, item, text):
    with db:
        db.execute('UPDATE coding_runs SET phase=?,updated_at=? WHERE id=?', (text, time.time(), item['run_id']))
        db.execute('UPDATE work_items SET checkpoint=?,updated_at=? WHERE id=?', (text, time.time(), item['id']))


def finish(db, item, state, result, links=()):
    with db:
        current = work.get(db, item['id'], lock=True)
        # A changed scope always needs a fresh authorization, even after a late result.
        if current['revision'] != item['revision']:
            state = 'held' if current['held'] else 'awaiting_approval'
        elif cancelled(db, item) and state != 'interrupted':
            state = 'cancelled' if current['state'] in {'cancelled', 'cancelling'} and not current['held'] else 'held'
        old_links = json.loads(current['pull_requests'])
        links = list(dict.fromkeys([*old_links, *links]))
        db.execute('UPDATE coding_runs SET phase=?,result=?,updated_at=? WHERE id=?',
                   (state, result, time.time(), item['run_id']))
        db.execute('UPDATE work_items SET state=?,result=?,pull_requests=?,checkpoint=?,updated_at=? WHERE id=?',
                   (state, result, json.dumps(links), state, time.time(), item['id']))
        add(db,item['id'],'update')


def recover(db):
    rows = db.execute("SELECT work_id,id,revision FROM coding_runs WHERE phase NOT IN ('ready_for_review','failed','cancelled','held','interrupted','awaiting_approval')").fetchall()
    for work_id, run_id, revision in rows:
        item = work.get(db, work_id)
        item.update(run_id=run_id, revision=revision)
        finish(db, item, 'interrupted', 'Worker restarted. Checkout and any published branch were preserved. Use /retry to explicitly continue; no work was replayed automatically.')


def heartbeat(db):
    with db:
        put(db, 'coding_heartbeat', time.time())
