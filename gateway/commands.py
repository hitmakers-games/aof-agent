"""Explicit, permission-checked gateway commands; ordinary text is private chat."""
import time

from gateway import access, tasks, work
from telegram.storage import setting, put


STANDS = {'dev': 'aof-dev', 'feature': 'aof-feature', 'release': 'aof-release'}
COMPONENTS = {'backend': 'aof-back', 'frontend': 'frontend-gateway'}
VIEWER = {'/start', '/help', '/whoami', '/status', '/tasks', '/work'}
ADMIN = {'/grant', '/revoke', '/users', '/autostart', '/pause', '/resume'}


def minimum(text):
    head = text.strip().split(maxsplit=1)[0].split('@', 1)[0].lower() if text.strip() else ''
    return 'viewer' if head in VIEWER else 'admin' if head in ADMIN else 'operator'


def help_text(db, actor):
    text = 'Developer gateway\n/whoami — your numeric ID and role\n/status [task] — availability/status\n/work or /tasks — assignments'
    if access.allowed(db, actor, 'operator'):
        text += '''\nPlain text or /ask — private conversation
/task description — shared assignment, awaiting approval by default
/task-run description — explicitly register and queue an assignment
/approve task — queue the current task scope
/amend task description — change scope; requires fresh approval
/hold task — hold a task; /cancel task — cancel
/report id — your saved result
/bug description — private bug triage
/health dev; /changes dev — read-only stand diagnostics
/logs dev backend 5m; /analyze dev backend 5m
/watch on|off — shared assignment summaries
/draft id — inspect your private task drafts'''
    if access.allowed(db, actor, 'admin'):
        text += '\n/grant id viewer|operator|admin\n/revoke id; /users\n/autostart on|off — queue new /task requests automatically\n/pause; /resume — pause/resume new coding starts'
    return text + '\nJira and coding availability are shown in /status. Approval never grants deployment or shared-database access.'


def identifier(value):
    value = value.lstrip('#')
    if not value.isdigit() or len(value) > 16:
        raise ValueError('Use the numeric job ID or assignment key shown in /tasks.')
    return int(value)


def own_job(db, actor, value):
    item = tasks.task(db, identifier(value))
    owner = item['chat_id'] or setting(db, 'owner') if item else None
    if not item or str(owner) != str(actor):
        raise ValueError('Task not found or access denied.')
    return item


def dispatch(db, update_id, text, actor=None, context=None):
    actor = actor if actor is not None else setting(db, 'owner')
    actor = int(actor) if actor is not None else None
    access.require(db, actor, minimum(text))
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return None
    head = parts[0].split('@', 1)[0].lower()
    argument = parts[1].strip() if len(parts)==2 else ''
    if head in {'/grant','/revoke','/users','/whoami'}:
        return access.command(db, actor, head, argument)
    if head in {'/start','/help'}:
        return help_text(db, actor)
    if head in {'/tasks','/work'}:
        text = work.listing(db, actor)
        if access.allowed(db, actor, 'operator'):
            rows = db.execute('''SELECT update_id,status,kind FROM inbox WHERE chat_id=? OR (chat_id IS NULL AND ?=?)
                                 ORDER BY created_at DESC LIMIT 10''', (actor, str(actor), setting(db,'owner'))).fetchall()
            text += '\nYour chat/diagnostic jobs:\n' + '\n'.join(f'#{uid}: {state} ({kind})' for uid,state,kind in rows)
        return text
    if head == '/status':
        if argument:
            if argument.lstrip('#').isdigit():
                access.require(db, actor, 'operator')
                own_job(db, actor, argument)
                return tasks.describe(db, identifier(argument))
            return work.describe(db, actor, argument)
        age = time.time()-float(setting(db,'engine_heartbeat','0'))
        ops_age = time.time()-float(setting(db,'operations_heartbeat','0'))
        coordinator_age = time.time()-float(setting(db,'coordinator_heartbeat','0'))
        active = db.execute("SELECT count(*) FROM inbox WHERE status IN ('queued','running','cancelling')").fetchone()[0]
        result = (f"Developer: {'online' if age<180 else 'offline'}\nStand diagnostics: {'online' if ops_age<30 else 'offline'}\n"
                f"Task coordinator: {'online' if coordinator_age<30 else 'offline'}\n"
                f"Jira: {setting(db,'jira_state','not configured')}\nCoding worker: not enabled\n"
                f"Auto-start: {setting(db,'autostart','off')}; paused: {setting(db,'paused','0')=='1'}\n"
                + (f'{active} active chat/diagnostic jobs.' if active else 'No active tasks.'))
        if access.allowed(db,actor,'operator'):
            own=db.execute("SELECT update_id,status FROM inbox WHERE (chat_id=? OR (chat_id IS NULL AND ?=?)) AND status IN ('queued','running','cancelling') LIMIT 5",
                           (actor,str(actor),setting(db,'owner'))).fetchall()
            result+=''.join(f'\n#{uid}: {state}' for uid,state in own)
        return result
    if head in {'/autostart','/pause','/resume'}:
        if head == '/autostart':
            if argument not in {'on','off'}:
                raise ValueError('Usage: /autostart on|off')
            put(db,'autostart',argument)
            if argument == 'off':
                db.execute("UPDATE work_items SET state='awaiting_approval',approved_revision=NULL,approved_by=NULL,approval_origin=NULL,updated_at=? WHERE state='queued' AND approval_origin='automatic'",(time.time(),))
            result = f'Automatic queuing for new assignments: {argument}. This never enables the coding worker.'
        else:
            put(db,'paused',int(head=='/pause'))
            result = 'New coding starts paused.' if head=='/pause' else 'New coding starts resumed.'
        access.audit(db,actor,head,'policy')
        return result
    if head == '/watch':
        if argument not in {'on','off'}:
            raise ValueError('Usage: /watch on|off')
        put(db,f'watch:{actor}',argument)
        return f'Shared task summaries: {argument}.'
    if head in {'/task','/task-run'}:
        mode='auto' if setting(db,'autostart','off')=='on' else 'ask'
        item = work.create(db,actor,argument[:500],argument,mode=mode,shared=True)
        if head=='/task-run':
            work.approve(db,actor,item['id'],item['revision'])
            return f"Registered {item['id']}. Queued; waiting for the coding worker."
        state = 'Queued; waiting for the coding worker.' if item['state']=='queued' else 'Awaiting approval; buttons will arrive in the task summary.'
        return f"Received {item['id']}. {state} Coding worker is not enabled."
    if head == '/amend':
        args=argument.split(maxsplit=1)
        if len(args)!=2:
            raise ValueError('Usage: /amend task_id new scope')
        return work.amend(db,actor,args[0],args[1])
    if head in {'/approve','/hold'}:
        return work.approve(db,actor,argument) if head=='/approve' else work.hold(db,actor,argument)
    if head == '/cancel':
        if argument and not argument.lstrip('#').isdigit():
            item = work.require_item(db,actor,argument,'operator',lock=True)
            work.hold(db,actor,item['id'])
            db.execute('UPDATE work_items SET state=?,updated_at=? WHERE id=?',
                       ('cancelling' if item['state']=='running' else 'cancelled',time.time(),item['id']))
            return f"{argument}: cancellation requested."
        if not argument:
            row=db.execute("SELECT update_id FROM inbox WHERE chat_id=? AND status IN ('queued','running','cancelling') ORDER BY created_at LIMIT 1",(actor,)).fetchone()
            if not row:
                return 'No active task to cancel. Use /work for assignments.'
            argument=str(row[0])
        own_job(db,actor,argument)
        return tasks.cancel(db,identifier(argument))
    if head == '/report':
        item=own_job(db,actor,argument)
        return f"#{item['update_id']} — {item['status']}\n{item['response']}" if item['response'] else 'No saved result yet.'
    if head == '/draft':
        from gateway import drafts
        return drafts.describe(db,actor,argument)
    if not head.startswith('/') or head in {'/ask','/bug'}:
        value = text if not head.startswith('/') else argument
        if not value.strip():
            raise ValueError('Please include a message.')
        tasks.enqueue(db,update_id,value,kind='bug' if head=='/bug' else 'message',actor=actor,context=context)
        return None
    if head in {'/health','/changes','/logs','/analyze'}:
        args=argument.split()
        detailed=head in {'/logs','/analyze'}
        if not args or args[0] not in STANDS or len(args)>(3 if detailed else 1):
            raise ValueError('Choose an explicit stand: dev, feature, release. Example: /logs dev backend 5m')
        component=args[1] if len(args)>1 else 'backend'
        window=args[2].removesuffix('m') if len(args)>2 else '15'
        if component not in COMPONENTS or not window.isdigit() or not 1<=int(window)<=60:
            raise ValueError('Components: backend/frontend. Log window: 1m to 60m.')
        tasks.enqueue(db,update_id,text,head[1:],args[0],component,int(window),actor=actor)
        return None
    return 'Unknown command. Use /help.'
