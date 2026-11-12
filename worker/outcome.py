"""Translate a worker result into a state backed by actual artifacts."""
import json

from developer import artifacts
from telegram.storage import setting
from worker.run import TaskError


def verify(db, item, response):
    try:
        result=json.loads(response)
        if result['outcome'] not in {'completed','blocked'} or not isinstance(result['summary'],str):
            raise ValueError('Invalid outcome')
    except (ValueError,KeyError,TypeError):
        raise TaskError('Worker returned no structured outcome; completion was not confirmed') from None
    summary=result['summary']
    if result['outcome']=='blocked':return 'blocked',summary
    expected=setting(db,'work-delivery:'+item['id'],'changes' if json.loads(item['repositories']) else 'answer')
    evidence=artifacts.inspect(db,item['id'])
    if expected=='changes' and not any(row['committed'] or row['changed_files'] for row in evidence):
        return 'blocked','No code changes were found in the actual checkouts.\n'+summary
    operation=setting(db,'request-operation:'+item['id'])
    if operation:
        state=json.loads(setting(db,'operation:'+operation,'{}')).get('state')
        if state=='failed':return 'failed','Deployment failed: '+operation+'\n'+summary
        if state!='completed':return 'deploying','Deployment is running: '+operation+'\n'+summary
    elif expected=='deployed':
        return 'blocked','No deployment operation was started.\n'+summary
    elif expected=='published' and not any(row['published'] and row['committed'] for row in evidence):
        return 'blocked','No new published commit was verified.\n'+summary
    return 'completed',summary


def deployment_finished(db, request, succeeded, message):
    from gateway import work
    from coding import queue
    work_id=str(request.get('request_id') or '')
    if len(work_id)!=32 or setting(db,'request-operation:'+work_id)!=request['id']:
        return
    item=work.get(db,work_id)
    if not item or item['state']!='deploying':return
    row=db.execute("SELECT id FROM coding_runs WHERE work_id=? AND phase='deploying' ORDER BY created_at DESC LIMIT 1",(work_id,)).fetchone()
    if not row:return
    item['run_id']=row[0]
    queue.finish(db,item,'completed' if succeeded else 'failed',message+'\n'+(item['result'] or ''))
