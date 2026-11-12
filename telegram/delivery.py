"""Per-recipient durable delivery, with permission checks immediately before send."""
import json

from gateway import access
from telegram.api import TelegramError
from telegram.storage import setting


def deliver_one(db, api):
    row = db.execute('''SELECT id,text,chat_id,required_role,reply_markup,message_key,authorized_by FROM outbox
                        WHERE delivered=0 ORDER BY id LIMIT 1''').fetchone()
    if row is None:
        return False
    report_id, text, actor, minimum, markup, key, authorized_by = row
    actor = actor or setting(db, 'owner')
    if actor is None:
        return False
    actor = int(actor)
    from telegram.groups import enabled
    permitted = access.allowed(db,actor,minimum) if actor>0 else enabled(db,actor) and access.allowed(db,authorized_by,minimum)
    if not permitted:
        with db:
            db.execute('UPDATE outbox SET delivered=1 WHERE id=?', (report_id,))
        return True
    markup = json.loads(markup) if markup else None
    previous = db.execute('SELECT message_id FROM message_handles WHERE recipient=? AND message_key=?',
                          (actor, key)).fetchone() if key else None
    payload = {'chat_id': actor, 'text': text, 'link_preview_options': {'is_disabled': True}}
    if markup:
        payload['reply_markup'] = markup
    message_id = None
    try:
        if previous:
            try:
                result = api.call('editMessageText', message_id=previous[0], **payload)
                message_id = previous[0]
            except TelegramError as error:
                if error.not_modified:
                    result, message_id = {}, previous[0]
                elif error.code == 400:
                    result = api.call('sendMessage', protect_content=True, **payload)
                else:
                    raise
        else:
            result = api.call('sendMessage', protect_content=True, **payload)
        if isinstance(result, dict) and type(result.get('message_id')) is int:
            message_id = result['message_id']
    except TelegramError as error:
        if error.code != 403:
            raise
        # One recipient blocking the bot must not stop everyone else's gateway.
        result = None
    with db:
        db.execute('UPDATE outbox SET delivered=1,delivered_message_id=? WHERE id=?', (message_id, report_id))
        if key and message_id:
            db.execute('''INSERT INTO message_handles VALUES (?,?,?) ON CONFLICT(recipient,message_key)
                          DO UPDATE SET message_id=excluded.message_id''', (actor, key, message_id))
    return True
