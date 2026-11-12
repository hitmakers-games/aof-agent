"""Retain received messages in authorized chats, including unmentioned group text."""
import time

from gateway import access
from gateway.redaction import redact
from telegram.storage import setting, put


def record(db, message):
    if not isinstance(message,dict):
        return
    chat,sender=message.get('chat'),message.get('from',{})
    if not isinstance(chat,dict) or not isinstance(sender,dict):
        return
    chat_id,message_id=chat.get('id'),message.get('message_id')
    if type(chat_id) is not int or type(message_id) is not int:
        return
    if chat_id>0:
        if not access.allowed(db,chat_id):
            return
    elif setting(db,f'group:{chat_id}:enabled','0')!='1':
        return
    text=message.get('text') or message.get('caption')
    if not isinstance(text,str) or not text:
        return
    title=chat.get('title') or chat.get('username') or chat.get('first_name')
    if title:
        put(db,f'telegram:chat:{chat_id}:title',str(title)[:200])
    name=sender.get('username') or ' '.join(sender.get(key,'') for key in ('first_name','last_name')).strip()
    db.execute('''INSERT INTO telegram_messages VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(chat_id,message_id) DO UPDATE SET text=excluded.text,edited=excluded.edited''',
        (chat_id,message_id,sender.get('id'),name[:200],message.get('date',time.time()),text[:32000],int('edit_date' in message)))


def chats(db, actor, current_chat):
    """Discover retained conversations before searching their messages."""
    access.require(db,actor,'operator')
    admin=access.allowed(db,actor,'admin')
    rows=db.execute('''SELECT chat_id,count(*) FROM telegram_messages GROUP BY chat_id
        UNION ALL SELECT chat_id,count(*) FROM inbox WHERE chat_id IS NOT NULL GROUP BY chat_id''').fetchall()
    counts={}
    for chat_id,count in rows:
        if chat_id in counts:
            continue
        if not admin:
            if current_chat<0 and chat_id!=current_chat:
                continue
            if chat_id>0 and chat_id!=actor:
                continue
            if chat_id<0 and chat_id!=current_chat and not db.execute(
                'SELECT 1 FROM telegram_messages WHERE chat_id=? AND sender_id=? LIMIT 1',(chat_id,actor)).fetchone():
                continue
        counts[chat_id]=count
    return {'chats':[{'chat_id':chat_id,'title':setting(db,f'telegram:chat:{chat_id}:title',''),
                     'type':'group' if chat_id<0 else 'private','retained_messages':count}
                    for chat_id,count in sorted(counts.items())],
            'coverage':'Received and saved messages only; Telegram does not provide a bot with arbitrary past history.'}


def read(db, actor, current_chat, chat_id=None, query='', limit=50, before_message_id=None):
    access.require(db,actor,'operator')
    target=int(chat_id) if chat_id is not None else current_chat
    admin=access.allowed(db,actor,'admin')
    if target>0 and target!=actor and not admin:
        raise ValueError('Private history belongs to its participant.')
    if current_chat<0 and target!=current_chat and not admin:
        raise ValueError('Other chats cannot be disclosed into this group.')
    if target<0:
        if setting(db,f'group:{target}:enabled','0')!='1':
            raise ValueError('This group is not connected.')
        participated=db.execute('SELECT 1 FROM telegram_messages WHERE chat_id=? AND sender_id=? LIMIT 1',(target,actor)).fetchone()
        if target!=current_chat and not access.allowed(db,actor,'admin') and not participated:
            raise ValueError('Group history is available to its participants.')
    limit=max(1,min(int(limit),150))
    cursor=' AND message_id<?' if before_message_id is not None else ''
    parameters=(target,'%'+query.lower()[:200]+'%')
    if before_message_id is not None:
        parameters+=(int(before_message_id),)
    rows=db.execute('''SELECT message_id,sender_name,sent_at,text FROM telegram_messages
        WHERE chat_id=? AND lower(text) LIKE ?'''+cursor+' ORDER BY message_id DESC LIMIT ?',
        (*parameters,limit)).fetchall()
    messages=[{'message_id':mid,'sender':name,'time':date,'text':redact(text)} for mid,name,date,text in reversed(rows)]
    if not messages and before_message_id is None:
        # Previously stored conversations remain searchable without inventing
        # Telegram message IDs or claiming complete historical group coverage.
        legacy=db.execute('''SELECT text,response,created_at FROM inbox WHERE
            (chat_id=? OR (chat_id IS NULL AND ?=?)) AND (lower(text) LIKE ? OR lower(COALESCE(response,'')) LIKE ?)
            ORDER BY created_at DESC LIMIT ?''',(target,str(target),setting(db,'owner'),'%'+query.lower()[:200]+'%','%'+query.lower()[:200]+'%',limit)).fetchall()
        messages=[{'source':'saved_conversation','time':date,'message':redact(text),'reply':redact(response or '')} for text,response,date in reversed(legacy)]
    return {'chat_id':target,'messages':messages,'next_before_message_id':rows[-1][0] if len(rows)==limit else None,
            'coverage':'Messages received since history recording was enabled; earlier Telegram history is unavailable through Bot API. Use the cursor to read earlier retained messages.'}
