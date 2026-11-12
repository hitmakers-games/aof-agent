"""Restore paused workers if a detached controller dies before its cleanup."""
import json
import time

from operations.kube import Kube
from operations.client import directory
from operations.runner import resume, save
from telegram.storage import setting, queue_report


def recover(db):
    operation_id = setting(db,'self_deploy','')
    if not operation_id:
        return
    kube = Kube()
    try:
        job = kube.get('jobs',operation_id)
    except ValueError as error:
        if '(NotFound)' not in str(error):raise
        previous=json.loads(setting(db,'operation:'+operation_id,'{}'))
        if time.time()-previous.get('updated',time.time())<120:return
        job={'status':{'failed':1}}
    if not (job.get('status',{}).get('failed') or job.get('status',{}).get('succeeded')):
        return
    request = json.loads((directory(operation_id)/'request.json').read_text())
    resume(db,kube,request)
    save(db,request,'failed',error='Controller stopped before finishing cleanup. Workers were restored; inspect the saved operation.')
    with db:
        queue_report(db,'Operation '+operation_id+' stopped unexpectedly. Workers were restored; inspect /ops '+operation_id,
                     request['chat_id'],required_role='admin',event_key='operation:'+operation_id,authorized_by=request['actor'])
