"""Batch nearby messages and attach live corrections to the same Codex turn."""
import json
import re
import time

from telegram.storage import put, setting


def stop(db, actor, chat, text):
    if not re.fullmatch(r'(stop|stop now|стоп|остановись|останови работу|cancel current task)[.!\s]*',text.strip(),re.IGNORECASE):
        return False
    row=db.execute("SELECT update_id FROM inbox WHERE requested_by=? AND chat_id=? AND kind='message' AND status='running' LIMIT 1",(actor,chat)).fetchone()
    if not row:return False
    db.execute("UPDATE inbox SET cancel_requested=1,status='cancelling' WHERE update_id=?",row)
    return True


def batch(db, actor, chat, text, context, model_options):
    if model_options:
        return False
    row=db.execute("""SELECT update_id,text,context FROM inbox WHERE requested_by=? AND chat_id=?
        AND kind='message' AND status='queued' ORDER BY created_at DESC,update_id DESC LIMIT 1"""
        +getattr(db,'claim_lock',''),(actor,chat)).fetchone()
    if not row:
        return False
    request,previous,previous_context=row
    db.execute('UPDATE inbox SET text=?,context=? WHERE update_id=?',
        (previous+'\n\n'+text,'\n'.join(value for value in (previous_context,context) if value) or None,request))
    put(db,'chat-ready:'+str(request),time.time()+1.5)
    return True


def pending(db, parent, selected):
    """Reserve same-sender inputs before sending; they cannot be merged mid-send."""
    messages=[]
    with db:
        db.execute('BEGIN IMMEDIATE')
        rows=db.execute("""SELECT update_id,text,context FROM inbox WHERE chat_id=? AND requested_by=?
            AND kind='message' AND status='queued' ORDER BY created_at,update_id LIMIT 20"""
            +getattr(db,'claim_lock',''),(parent['chat_id'],parent['requested_by'])).fetchall()
        for uid,text,context in rows:
            if float(setting(db,'chat-ready:'+str(uid),'0'))>time.time():continue
            choice=json.loads(setting(db,'request-model:'+str(uid),'{}'))
            if choice and choice!=selected:break
            db.execute("UPDATE inbox SET status='steering',checkpoint='Updating the active conversation' WHERE update_id=?",(uid,))
            put(db,'chat-parent:'+str(uid),parent['update_id'])
            messages.append({'id':uid,'text':text+('\n'+context if context else '')})
    return messages


def accepted(db, parent, messages):
    with db:
        for message in messages:
            db.execute("UPDATE inbox SET status='attached',checkpoint='Included in the active conversation' WHERE update_id=? AND status='steering'",(message['id'],))
        text=db.execute('SELECT text FROM inbox WHERE update_id=?',(parent,)).fetchone()[0]
        db.execute('UPDATE inbox SET text=? WHERE update_id=?',(text+'\n\n'+'\n\n'.join(m['text'] for m in messages),parent))


def release(db, messages):
    with db:
        for message in messages:
            db.execute("UPDATE inbox SET status='queued' WHERE update_id=? AND status='steering'",(message['id'],))
            db.execute('DELETE FROM settings WHERE key=?',('chat-parent:'+str(message['id']),))


def finish(db, parent, response, status):
    db.execute("""UPDATE inbox SET status=?,response=?,finished_at=CURRENT_TIMESTAMP,checkpoint=?
        WHERE status IN ('attached','steering') AND EXISTS (
          SELECT 1 FROM settings WHERE key='chat-parent:' || CAST(inbox.update_id AS TEXT) AND value=?)""",
        (status,response,status.capitalize(),str(parent)))
