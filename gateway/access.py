"""Telegram identities and server-side permissions; usernames grant no access."""
import json
import time
import uuid

from telegram.storage import setting


LEVEL = {'viewer': 1, 'operator': 2, 'admin': 3}


def role(db, user_id):
    if user_id is None:
        return None
    if str(user_id) == setting(db, 'owner'):
        return 'admin'
    row = db.execute('SELECT role FROM members WHERE user_id=? AND active=1', (user_id,)).fetchone()
    return row[0] if row else None


def allowed(db, user_id, minimum='viewer'):
    return LEVEL.get(role(db, user_id), 0) >= LEVEL[minimum]


def require(db, user_id, minimum='viewer'):
    if not allowed(db, user_id, minimum):
        raise ValueError('Access denied. This action requires ' + minimum + ' access.')


def audit(db, actor, action, entity, details=None):
    db.execute('INSERT INTO audit_events VALUES (?,?,?,?,?,?)',
               (uuid.uuid4().hex, actor, action, str(entity), json.dumps(details or {}), time.time()))


def ensure_owner(db):
    owner = setting(db, 'owner')
    if owner:
        db.execute('''INSERT INTO members(user_id,role,active,created_at) VALUES (?,'admin',1,?)
                      ON CONFLICT(user_id) DO NOTHING''', (int(owner), time.time()))


def grant(db, actor, target, target_role):
    require(db, actor, 'admin')
    if target_role not in LEVEL or not 0 < target < 2**63:
        raise ValueError('Usage: /grant numeric_user_id viewer|operator|admin')
    if str(target) == setting(db, 'owner') and target_role != 'admin':
        raise ValueError('The original owner cannot be demoted.')
    old = role(db, target)
    db.execute('''INSERT INTO members(user_id,role,active,invited_by,created_at) VALUES (?,?,1,?,?)
                  ON CONFLICT(user_id) DO UPDATE SET role=excluded.role,active=1,invited_by=excluded.invited_by''',
               (target, target_role, actor, time.time()))
    if LEVEL.get(old, 0) > LEVEL[target_role] and target_role == 'viewer':
        stop_authorized_work(db, target)
    audit(db, actor, 'access.grant', target, {'role': target_role})
    return f'{target}: {target_role}. They can open the bot and send /start.'


def stop_authorized_work(db, target):
    db.execute('UPDATE callback_tokens SET used=1 WHERE recipient=?', (target,))
    db.execute("UPDATE task_drafts SET state='cancelled' WHERE actor_id=? AND state='proposed'", (target,))
    db.execute('''UPDATE inbox SET cancel_requested=1,
                  status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END
                  WHERE requested_by=? AND status IN ('queued','running','cancelling')''', (target,))
    db.execute('''UPDATE work_items SET state='awaiting_approval',approved_by=NULL,approved_revision=NULL,
                  cancel_requested=CASE WHEN state='running' THEN 1 ELSE cancel_requested END,updated_at=?
                  WHERE approved_by=? AND state IN ('queued','running')''', (time.time(),target))


def revoke(db, actor, target):
    require(db, actor, 'admin')
    if str(target) == setting(db, 'owner'):
        raise ValueError('The original owner cannot be revoked.')
    db.execute('UPDATE members SET active=0 WHERE user_id=?', (target,))
    stop_authorized_work(db, target)
    audit(db, actor, 'access.revoke', target)
    return f'{target}: access revoked. Pending approvals and requests were stopped.'


def command(db, actor, head, argument):
    if head == '/whoami':
        return f'Your Telegram user ID: {actor}\nRole: {role(db, actor) or "no access"}'
    require(db, actor, 'admin')
    if head == '/users':
        ensure_owner(db)
        rows = db.execute('SELECT user_id,role,active FROM members ORDER BY user_id').fetchall()
        return '\n'.join(f'{uid}: {name}' + ('' if active else ' (revoked)') for uid, name, active in rows)
    args = argument.split()
    try:
        target = int(args[0])
    except (IndexError, ValueError):
        raise ValueError('Use the numeric ID from the person’s /whoami reply.') from None
    if head == '/grant' and len(args) == 2:
        return grant(db, actor, target, args[1])
    if head == '/revoke' and len(args) == 1:
        return revoke(db, actor, target)
    raise ValueError('Usage: /grant id viewer|operator|admin or /revoke id')
