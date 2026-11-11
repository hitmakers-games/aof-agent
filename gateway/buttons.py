"""Actor-bound, expiring buttons. Callback text never supplies authority or scope."""
import json
import secrets
import time

from gateway import access
from telegram.storage import queue_report


def token(db, recipient, action, payload, message_key):
    value = secrets.token_urlsafe(18)
    db.execute('INSERT INTO callback_tokens VALUES (?,?,?,?,?,?,0)',
               (value, recipient, action, json.dumps(payload), message_key, time.time()+86400))
    return 'w:' + value


def keyboard(db, actor, actions, payload, message_key):
    return {'inline_keyboard': [[{'text': title, 'callback_data': token(db, actor, action, payload, message_key)}
                                 for title, action in actions]]}


def handle(db, actor, data, message_id, now=None):
    access.require(db, actor, 'operator')
    now = time.time() if now is None else now
    # Every field after the nonce is loaded from the database, never the callback.
    if not isinstance(data, str) or not data.startswith('w:') or len(data) > 64:
        raise ValueError('Invalid button.')
    suffix = getattr(db, 'row_lock', '')
    row = db.execute('SELECT recipient,action,payload,message_key,expires_at,used FROM callback_tokens WHERE token=?'+suffix,
                     (data[2:],)).fetchone()
    if not row or row[0] != actor or row[4] <= now:
        raise ValueError('This button has expired or belongs to another person.')
    recipient, action, payload, key, _, used = row
    handle = db.execute('SELECT message_id FROM message_handles WHERE recipient=? AND message_key=?', (actor, key)).fetchone()
    if not handle or handle[0] != message_id:
        raise ValueError('The message is stale. Use /work to see current tasks.')
    if used:
        return 'Already processed.'
    payload = json.loads(payload)
    if action in ('register', 'register_run', 'discard'):
        from gateway.drafts import confirm
        result = confirm(db, actor, payload['request'], payload['revision'], action)
    else:
        from gateway import work
        results = []
        for item in payload['items']:
            current = work.require_item(db, actor, item['id'], 'operator')
            if action == 'approve':
                results.append(work.approve(db, actor, current['id'], item['revision']))
            elif action == 'hold':
                results.append(work.hold(db, actor, current['id'], item['revision']))
            elif action in ('view', 'discuss'):
                results.append(work.describe(db, actor, current['id']))
            else:
                raise ValueError('Unsupported button action.')
        if action in ('view', 'discuss'):
            refreshed = {'items': [{'id': i['id'], 'revision': work.get(db,i['id'])['revision']} for i in payload['items']]}
            markup = keyboard(db,actor,[('Start pending batch','approve'),('Hold','hold')],refreshed,key)
            queue_report(db, '\n\n'.join(results)[:1500], actor, reply_markup=markup, message_key=key)
            return 'Current task status shown.'
        result = f"{sum(r.startswith('queued') or r=='already queued' for r in results)} queued; " + \
                 f"{sum(r in ('held','cancelling') for r in results)} held; " + \
                 f"{sum(r=='changed' for r in results)} changed since this message."
    db.execute('UPDATE callback_tokens SET used=1 WHERE token=?', (data[2:],))
    access.audit(db, actor, 'button.'+action, key)
    queue_report(db, result, actor, message_key=key)
    return 'Updated.'
