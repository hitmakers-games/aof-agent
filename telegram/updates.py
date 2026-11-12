"""Authenticate Telegram updates before command, callback, or conversation routing."""
import hmac
import time

from gateway import access, buttons
from gateway.commands import dispatch, minimum
from telegram.storage import setting, put, queue_report


def action(db, operation):
    """A denied batch must roll back its earlier items, while retaining the reply."""
    db.execute('SAVEPOINT gateway_action')
    try:
        result=operation()
    except ValueError as error:
        db.execute('ROLLBACK TO SAVEPOINT gateway_action')
        result=str(error)
    finally:
        db.execute('RELEASE SAVEPOINT gateway_action')
    return result


def identity(message):
    if not isinstance(message, dict):
        return None
    sender, chat = message.get('from'), message.get('chat')
    if not isinstance(sender,dict) or not isinstance(chat,dict):
        return None
    user=sender.get('id')
    return user if (type(user) is int and user>0 and chat.get('id')==user and
                    chat.get('type')=='private' and not sender.get('is_bot',False)) else None


def handle_update(db, update, pairing_code, pairing_expires_at, now=None):
    if not isinstance(update,dict) or type(update.get('update_id')) is not int:
        return None
    update_id=update['update_id']
    if update_id<int(setting(db,'offset','0')):
        return None
    now=time.time() if now is None else now
    notice=None
    with db:
        access.ensure_owner(db)
        callback=update.get('callback_query')
        if isinstance(callback,dict):
            message=callback.get('message')
            sender=callback.get('from')
            private=dict(message, **{'from':sender}) if isinstance(message,dict) else {}
            actor=identity(private)
            result='Access denied.'
            if actor:
                result=action(db,lambda:buttons.handle(db,actor,callback.get('data'),message.get('message_id'),now))
            if isinstance(callback.get('id'),str):
                notice={'callback_query_id':callback['id'],'text':result[:180]}
        else:
            message=update.get('message')
            from telegram.groups import handle
            if isinstance(message,dict) and handle(db,update_id,message):
                put(db,'offset',update_id+1)
                return None
            actor=identity(message)
            text=message.get('text','') if isinstance(message,dict) else ''
            if actor and isinstance(text,str):
                head=text.strip().split(maxsplit=1)[0].split('@',1)[0].lower() if text.strip() else ''
                owner=setting(db,'owner')
                if owner is None:
                    parts=text.strip().split(maxsplit=1)
                    supplied=parts[1].strip() if head=='/start' and len(parts)==2 else ''
                    if pairing_code and now<pairing_expires_at and hmac.compare_digest(supplied.encode(),pairing_code.encode()):
                        put(db,'owner',actor)
                        put(db,'pairing_last_result','paired')
                        access.ensure_owner(db)
                        queue_report(db,'Paired. You are the administrator. Use /help.',actor)
                    else:
                        expired=now>=pairing_expires_at
                        put(db,'pairing_last_result','expired' if expired else 'invalid_code' if supplied else 'missing_code')
                        notice={'chat_id':actor,'text':'Your chat is not paired yet. '+
                                ('The pairing code has expired. Ask for a fresh pairing link.' if expired else
                                 'Send the complete /start command from the setup chat, including its code.')}
                elif not access.allowed(db,actor):
                    if head in {'/whoami','/start'}:
                        notice={'chat_id':actor,'text':f'Your Telegram user ID: {actor}\nNo access. Ask the administrator to grant your numeric ID a role.'}
                else:
                    def command():
                        if not text.strip() and any(k in message for k in ('photo','document','voice','video')):
                            access.require(db,actor,'operator')
                            return 'Please describe the issue in text; attachment analysis is not enabled.'
                        from gateway.discussion import context
                        reply=message.get('reply_to_message') or {}
                        discussion=context(db,actor,reply.get('message_id')) if isinstance(reply,dict) else None
                        return dispatch(db,update_id,text,actor,discussion)
                    response=action(db,command)
                    if response:
                        # Permission denials can be read by any active member;
                        # successful privileged replies retain their required role.
                        required=minimum(text)
                        if not access.allowed(db,actor,required):
                            required='viewer'
                        queue_report(db,response,actor,required_role=required)
        put(db,'offset',update_id+1)
    return notice
