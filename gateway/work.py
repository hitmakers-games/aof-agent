"""Durable assignments. Approval and execution are separate transitions."""
import hashlib
import json
import secrets
import time
import uuid

from gateway import access
from telegram.storage import setting


TERMINAL = {'cancelled', 'failed', 'interrupted', 'ready_for_review'}
ALIAS_WORDS = ('amber', 'birch', 'comet', 'delta', 'ember', 'fable', 'glade', 'harbor',
               'indigo', 'juniper', 'kestrel', 'lantern', 'meadow', 'nova', 'orbit', 'pebble',
               'quartz', 'raven', 'saffron', 'timber', 'umber', 'violet', 'willow', 'zephyr')


def new_issue_key(db):
    # Three word choices give over 13,000 distinct short aliases; the database
    # constraint remains the authority if simultaneous requests collide.
    return 'TG-' + '-'.join(secrets.choice(ALIAS_WORDS) for _ in range(3))


def revision(title, description, mode='ask', repositories=None):
    payload = json.dumps([title.strip(), description.strip(), repositories or []], ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def get(db, reference, lock=False):
    suffix = getattr(db, 'row_lock', '') if lock else ''
    cursor = db.execute('SELECT * FROM work_items WHERE id=? OR issue_key=?' + suffix, (reference, reference))
    row = cursor.fetchone()
    return dict(zip([c[0] for c in cursor.description], row)) if row else None


def visible(db, actor, item):
    return bool(item and item.get('deleted_at') is None and access.allowed(db, actor) and
                (item['visibility'] == 'shared' or item['requester_id'] == actor or access.allowed(db,actor,'admin')))


def require_item(db, actor, reference, minimum='viewer', lock=False):
    access.require(db, actor, minimum)
    item = get(db, reference, lock)
    if not visible(db, actor, item):
        raise ValueError('Task not found or access denied.')
    return item


def create(db, actor, title, description='', source='telegram', mode='ask', external_id=None,
           issue_key=None, origin_action=None, notify=False, shared=False, repositories=None):
    if actor is not None:
        access.require(db, actor, 'operator')
    if not title.strip() or len(title) > 500 or len(description) > 32000:
        raise ValueError('Task title or description exceeds its limit.')
    task_id = uuid.uuid4().hex
    generated_key = issue_key is None
    issue_key = issue_key or new_issue_key(db)
    names = []
    if repositories:
        from coding.config import repositories as validate
        names = validate(repositories)
    rev = revision(title, description, mode, names)
    automatic = actor is not None and mode == 'auto' and setting(db, 'autostart', 'off') == 'on' and not origin_action
    state = 'queued' if automatic else 'awaiting_approval'
    now = time.time()
    values = (task_id, source, external_id, issue_key, title.strip(), description.strip(), rev, state, mode,
              'shared' if shared or source == 'jira' else 'private', actor, rev if automatic else None,
              origin_action, now, now, actor if automatic else None, 'automatic' if automatic else None)
    insert = '''INSERT INTO work_items(id,source,external_id,issue_key,title,description,revision,state,mode,
                  visibility,requester_id,approved_revision,origin_action,created_at,updated_at,approved_by,approval_origin)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'''
    try:
        db.execute(insert, values)
    except Exception as error:
        if not generated_key or 'issue_key' not in str(error):
            raise
        issue_key = new_issue_key(db)
        db.execute(insert, values[:3] + (issue_key,) + values[4:])
    db.execute('UPDATE work_items SET repositories=? WHERE id=?', (json.dumps(names), task_id))
    access.audit(db, actor, 'work.received', task_id, {'source': source, 'automatic': automatic})
    if notify:
        from gateway.notices import add
        add(db, task_id, 'intake')
    return get(db, task_id)


def approve(db, actor, reference, expected_revision=None):
    item = require_item(db, actor, reference, 'operator', lock=True)
    if expected_revision and item['revision'] != expected_revision:
        return 'changed'
    if not access.allowed(db,actor,'admin') and set(json.loads(item['repositories'])) & {'aof-agent','aof-infra'}:
        raise ValueError('Only administrators can change the agent or its infrastructure.')
    if item['state'] in TERMINAL or item['state'] == 'running':
        return item['state']
    if item['state'] == 'cancelling':
        return 'Waiting for the previous worker to stop.'
    if item['state'] == 'queued' and item['approved_revision'] == item['revision']:
        return 'already queued'
    db.execute('''UPDATE work_items SET state='queued',approved_by=?,approved_revision=revision,
                  held=0,cancel_requested=0,needs_approval=0,approval_origin='telegram',updated_at=? WHERE id=?''', (actor, time.time(), item['id']))
    access.audit(db, actor, 'work.approved', item['id'], {'revision': item['revision']})
    return 'queued; waiting for the coding worker'


def hold(db, actor, reference, expected_revision=None):
    item = require_item(db, actor, reference, 'operator', lock=True)
    if expected_revision and item['revision'] != expected_revision:
        return 'changed'
    if item['state'] in TERMINAL:
        return item['state']
    state = 'cancelling' if item['state'] == 'running' else 'held'
    db.execute('''UPDATE work_items SET state=?,held=1,cancel_requested=1,approved_by=NULL,
                  approved_revision=NULL,updated_at=? WHERE id=?''', (state, time.time(), item['id']))
    access.audit(db, actor, 'work.held', item['id'])
    return state


def describe(db, actor, reference):
    item = require_item(db, actor, reference)
    text = f"{item['issue_key'] or item['id']}: {item['state']}"
    if access.allowed(db, actor, 'operator'):
        text += '\n' + item['title']
        names = json.loads(item['repositories'])
        text += '\nRepositories: ' + (', '.join(names) or 'not selected; use /repo')
        if item['checkpoint']:
            text += '\nProgress: ' + item['checkpoint']
        text += ''.join('\n' + link for link in json.loads(item['pull_requests']))
    if item['state'] == 'queued':
        text += '\nApproved; waiting for an available coding worker.'
    return text


def listing(db, actor, shared_only=False):
    rows = db.execute('''SELECT id,issue_key,state,visibility,requester_id,title,checkpoint FROM work_items
                         WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT 100''').fetchall()
    return '\n'.join(f'{key or uid}: {state}'+(' — '+title[:90] if access.allowed(db,actor,'operator') else '')
                     +(f' ({checkpoint})' if state=='running' and checkpoint and access.allowed(db,actor,'operator') else '')
                     for uid, key, state, visibility, owner,title,checkpoint in rows
                     if visibility == 'shared' or (not shared_only and (owner == actor or access.allowed(db,actor,'admin'))))[:3500] or 'No assignments yet.'


def delete(db, actor, reference):
    item = require_item(db, actor, reference, 'operator', lock=True)
    if item['state'] not in TERMINAL:
        raise ValueError('Only closed tasks can be deleted. Cancel or finish the task first.')
    db.execute('UPDATE work_items SET deleted_at=? WHERE id=? AND deleted_at IS NULL', (time.time(), item['id']))
    access.audit(db, actor, 'work.deleted', item['id'], {'source': item['source']})
    return {'task': item['issue_key'], 'state': 'deleted'}


def amend(db, actor, reference, description):
    item = require_item(db,actor,reference,'operator',lock=True)
    if not description.strip() or len(description)>32000:
        raise ValueError('Please include the updated scope (at most 32000 characters).')
    new_revision=revision(item['title'],description,repositories=json.loads(item['repositories']))
    if new_revision==item['revision']:
        return 'Scope unchanged.'
    state='cancelling' if item['state']=='running' else 'held' if item['held'] else 'awaiting_approval'
    db.execute('''UPDATE work_items SET description=?,revision=?,state=?,approved_by=NULL,approved_revision=NULL,
                  approval_origin=NULL,needs_approval=1,cancel_requested=?,updated_at=? WHERE id=?''',
               (description.strip(),new_revision,state,int(item['state']=='running'),time.time(),item['id']))
    access.audit(db,actor,'work.scope_changed',item['id'])
    from gateway.notices import add
    add(db,item['id'])
    return 'Scope updated; fresh approval is required.'


def withdraw(db, reference):
    item = get(db, reference, lock=True)
    if item and item['state'] in TERMINAL:
        db.execute('UPDATE work_items SET assigned=0 WHERE id=?', (item['id'],))
        return
    if item and item['state'] not in TERMINAL:
        db.execute("UPDATE work_items SET state='cancelled',assigned=0,cancel_requested=1,approved_revision=NULL,updated_at=? WHERE id=?",
                   (time.time(), item['id']))
        access.audit(db, None, 'work.unassigned', item['id'])
        from gateway.notices import add
        add(db, item['id'], 'completion')
