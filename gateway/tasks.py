"""Private chat/diagnostic jobs. Assignments and approvals live in gateway.work."""
import time

from gateway.redaction import redact
from gateway import access
from telegram.storage import queue_report, setting


ACTIVE = ("queued", "running", "cancelling")
OPERATIONS = ("health", "logs", "changes", "analyze")


def task(db, task_id, lock=False):
    suffix = getattr(db, "row_lock", "") if lock else ""
    cursor = db.execute("SELECT * FROM inbox WHERE update_id=?" + suffix, (task_id,))
    row = cursor.fetchone()
    return dict(zip([c[0] for c in cursor.description], row)) if row else None


def enqueue(db, update_id, text, kind="message", target=None, component="backend", window=15, actor=None, context=None, chat_id=None, model_options=None):
    actor = actor if actor is not None else setting(db, 'owner')
    actor = int(actor) if actor is not None else None
    if actor is not None:
        access.require(db, actor, 'viewer' if kind=='message' else 'operator')
        pending = db.execute("SELECT count(*) FROM inbox WHERE requested_by=? AND status IN ('queued','running','cancelling')", (actor,)).fetchone()[0]
        if pending >= 5:
            raise ValueError('You already have five pending requests. Use /tasks or wait for a result.')
    count = db.execute("SELECT count(*) FROM inbox WHERE status IN (?,?,?)", ACTIVE).fetchone()[0]
    if count >= 100:
        raise ValueError("The task queue is full. Use /tasks and /cancel before adding work.")
    db.execute("""INSERT INTO inbox(update_id,text,status,kind,target,component,"window",checkpoint,chat_id,requested_by,context)
                  VALUES (?,?,'queued',?,?,?,?, 'Waiting for a worker',?,?,?)""",
               (update_id, text, kind, target, component, window, chat_id or actor, actor, context))
    from gateway.models import snapshot
    snapshot(db, update_id, chat_id or actor, model_options)
    worker_online = time.time() - float(setting(db, "engine_heartbeat", "0")) < 180
    if kind != "message":
        queue_report(db, f"Received #{update_id}: {kind}" + (f" on {target}" if target else "") +
                     f". Use /status {update_id} or /cancel {update_id} in private chat.", chat_id or actor,authorized_by=actor)


def claim(db, worker):
    if worker=='model' and setting(db,'self_deploy',''):
        import json
        operation=json.loads(setting(db,'operation:'+setting(db,'self_deploy'),'{}'))
        if operation.get('state') in {'restarting','applying','rolling-back'}:
            return None
    if worker == "model":
        condition = "(kind IN ('message','task','bug') OR (kind='analyze' AND context IS NOT NULL))"
    elif worker == "operations":
        condition = "kind IN ('health','logs','changes','analyze') AND context IS NULL"
    else:
        raise ValueError("Unknown worker")
    db.execute("BEGIN IMMEDIATE")
    try:
        if worker=='model' and getattr(db,'row_lock',''):
            db.execute('SELECT pg_advisory_xact_lock(3350006)')
        query = f"""SELECT update_id FROM inbox q WHERE status='queued' AND {condition}
            AND NOT EXISTS (SELECT 1 FROM inbox active WHERE active.chat_id=q.chat_id
                AND active.status IN ('running','cancelling')) ORDER BY created_at,update_id LIMIT 1"""
        row = db.execute(query + getattr(db, "claim_lock", "")).fetchone()
        if row:
            db.execute("UPDATE inbox SET status='running',started_at=CURRENT_TIMESTAMP,checkpoint=? WHERE update_id=?",
                       ("Collecting stand evidence" if worker == "operations" else "Preparing the developer's response", row[0]))
            item = task(db, row[0])
            actor = item['requested_by'] or setting(db, 'owner')
            if actor is not None and not access.allowed(db, int(actor), 'viewer' if item['kind']=='message' else 'operator'):
                db.execute("UPDATE inbox SET status='cancelled',cancel_requested=1 WHERE update_id=?", (row[0],))
                db.commit()
                return None
            if item["kind"] != "message":
                queue_report(db, f"Started #{row[0]}: {item['checkpoint']}.", item['chat_id'],authorized_by=item['requested_by'])
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return task(db, row[0]) if row else None


def cancellation_requested(db, task_id):
    row = db.execute("SELECT cancel_requested,requested_by,kind FROM inbox WHERE update_id=?", (task_id,)).fetchone()
    return bool(row and (row[0] or (row[1] is not None and not access.allowed(db, row[1], 'viewer' if row[2]=='message' else 'operator'))))


def cancel(db, task_id):
    item = task(db, task_id)
    if item is None:
        return "Task not found. Use /tasks."
    if item["status"] not in ACTIVE:
        return f"#{task_id} is already {item['status']}."
    changed = db.execute("""UPDATE inbox SET cancel_requested=1,
                            status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END,
                            checkpoint='Cancellation requested'
                            WHERE update_id=? AND status IN ('queued','running','cancelling')""", (task_id,)).rowcount
    status = task(db, task_id)["status"]
    if not changed:
        return f"#{task_id} is already {status}."
    return f"#{task_id}: {status}." + (" Waiting for the worker to stop." if status == "cancelling" else "")


def finish(db, task_id, response, status="completed", proposals=None):
    response = redact(response)
    with db:
        db.execute("BEGIN IMMEDIATE")
        item = task(db, task_id, lock=True)
        if item["response"] is not None and item["status"] not in ACTIVE:
            return
        if cancellation_requested(db, task_id):
            response, status = "Cancelled. No further work will run for this request.", "cancelled"
        if proposals and status == 'completed':
            from gateway.drafts import propose
            actor = item['requested_by'] or setting(db, 'owner')
            propose(db, int(actor), proposals, introduction=response)
        db.execute("UPDATE inbox SET status=?,response=?,finished_at=CURRENT_TIMESTAMP,checkpoint=? WHERE update_id=?",
                   (status, response, status.capitalize(), task_id))
        text = response if item["kind"] == "message" and status == "completed" else f"#{task_id} — {status}\n{response}"
        if response.strip()=='[[SILENT]]' and status=='completed':
            return
        if not proposals or status != 'completed':
            queue_report(db, text, item['chat_id'],authorized_by=item['requested_by'],required_role='viewer' if item['kind']=='message' else 'operator')
        elif item['chat_id'] and item['chat_id']<0:
            queue_report(db,'Task drafts are ready. Review the private message from the bot; nothing was registered yet.',
                         item['chat_id'],authorized_by=item['requested_by'])


def save_evidence(db, task_id, evidence):
    with db:
        changed = db.execute("""UPDATE inbox SET status='queued',context=?,
                                checkpoint='Evidence collected; waiting for analysis'
                                WHERE update_id=? AND cancel_requested=0 AND status='running'""",
                             (evidence, task_id)).rowcount
    if not changed:
        finish(db, task_id, "Cancelled", "cancelled")


def recover(db, worker):
    condition = ("kind IN ('health','logs','changes','analyze') AND context IS NULL"
                 if worker == "operations" else "(kind IN ('message','task','bug') OR (kind='analyze' AND context IS NOT NULL))")
    ids = [r[0] for r in db.execute(f"SELECT update_id FROM inbox WHERE status IN ('running','cancelling') AND {condition}")]
    for task_id in ids:
        finish(db, task_id, "The worker restarted before completion. The request was preserved; send a new request to retry.", "interrupted")


def describe(db, task_id):
    item = task(db, task_id)
    if not item:
        return "Task not found. Use /tasks."
    return (f"#{task_id}: {item['status']} ({item['kind']})\n"
            f"Target: {item['target'] or 'developer conversation'}\n"
            f"Created: {item['created_at']} UTC\n"
            f"Progress: {item['checkpoint'] or item['status']}")
