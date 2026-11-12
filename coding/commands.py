"""Repository selection and explicit retries retain the approval boundary."""
import json
import time

from coding.config import repositories
from gateway import access, work
from gateway.notices import add


def dispatch(db, actor, head, argument):
    access.require(db, actor, 'operator')
    args = argument.split(maxsplit=1)
    if not args:
        raise ValueError('Include the task ID from /work.')
    item = work.require_item(db, actor, args[0], 'operator', lock=True)
    if head == '/result':
        return work.describe(db, actor, item['id']) + '\n' + (item['result'] or 'No completed result yet.')
    if item['state'] in {'running', 'cancelling'}:
        raise ValueError('Hold/cancel this run and wait for it to stop before changing the task.')
    if head == '/repo':
        if len(args) != 2:
            raise ValueError('Usage: /repo task_id aof-back,aof-front')
        names = repositories(args[1])
        rev = work.revision(item['title'], item['description'], repositories=names)
        if rev == item['revision']:
            return 'Repositories unchanged.'
        db.execute('UPDATE work_items SET repositories=?,revision=? WHERE id=?', (json.dumps(names), rev, item['id']))
    elif head == '/revise':
        if len(args) != 2 or len(args[1]) > 16000:
            raise ValueError('Usage: /revise task_id concrete follow-up request')
        description = item['description'][:16000] + '\n\nFollow-up:\n' + args[1]
        rev = work.revision(item['title'], description, repositories=json.loads(item['repositories']))
        db.execute('UPDATE work_items SET description=?,revision=? WHERE id=?', (description, rev, item['id']))
    elif head == '/retry':
        if item['state'] not in {'failed', 'blocked', 'interrupted', 'cancelled', 'held'}:
            raise ValueError('Retry applies to failed, interrupted, held, or cancelled tasks.')
    else:
        raise ValueError('Unsupported coding command')
    db.execute("""UPDATE work_items SET state='awaiting_approval',approved_by=NULL,approved_revision=NULL,
        approval_origin=NULL,held=0,cancel_requested=0,updated_at=? WHERE id=?""", (time.time(), item['id']))
    access.audit(db, actor, head, item['id'])
    add(db, item['id'])
    return 'Task updated; fresh approval required. The previous workspace and task branch are preserved.'
