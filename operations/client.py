"""Launch operations outside the conversation pod and inspect durable results."""
import base64
import fcntl
import json
import os
from pathlib import Path
import re
import time
import uuid

from coding import git
from developer.git_tools import Repositories
from developer.session import workspace
from operations import archive, jobs
from operations.kube import Kube


ROOT = Path('/workspace/operations')


def directory(operation_id):
    if not re.fullmatch(r'aof-op-[a-f0-9]{12}', operation_id or ''):
        raise ValueError('Use the operation ID returned when starting the job.')
    return ROOT/operation_id


def status(operation_id):
    path = directory(operation_id)/'status.json'
    if path.exists():
        return json.loads(path.read_text())
    return {'id': operation_id, 'state': 'starting', 'job': Kube().get('jobs', operation_id).get('status', {})}


def execute(actor, chat_id, action='status', operation_id=None, dockerfile='worker/app.Dockerfile', branch='master'):
    if action=='status':
        return status(operation_id)
    if action not in {'deploy','plan','apply'} or dockerfile not in {'worker/app.Dockerfile','worker/Dockerfile'}:
        raise ValueError('Choose deploy, plan, apply, or status and a supported Dockerfile.')
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open('/transport/developer-control/operation.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return start(actor, chat_id, action, operation_id, dockerfile,branch)


def start(actor, chat_id, action, plan_id, dockerfile,branch):
    kube = Kube()
    for item in kube.get('jobs').get('items', []):
        labels = item['metadata'].get('labels', {})
        state = item.get('status', {})
        if labels.get('aof-agent/role')=='controller' and not state.get('succeeded') and not state.get('failed'):
            return {'id': item['metadata']['name'], 'state': 'already-running'}
    operation_id = 'aof-op-'+uuid.uuid4().hex[:12]
    target = directory(operation_id)
    target.mkdir(mode=0o700)
    request = {'id': operation_id, 'actor': actor, 'chat_id': chat_id, 'action': action,
               'dockerfile': dockerfile, 'source': operation_id+'-source', 'created': time.time(),
               'request_id':os.environ.get('AOF_MCP_REQUEST')}
    binary = {}
    if action=='apply':
        previous = status(plan_id)
        if previous.get('state')!='planned':
            raise ValueError('Only a successfully saved plan can be applied. Run terraform plan first.')
        request['plan_id'] = plan_id
    else:
        repositories = Repositories(workspace(actor, chat_id))
        sources=[('aof-infra','infra.tar.gz',archive.INFRA_PATHS)]
        if action=='deploy':sources.insert(0,('aof-agent','app.tar.gz',None))
        for name, filename, paths in sources:
            path = repositories.root/name
            if path.exists() and git.run(['status','--porcelain'],path):
                raise ValueError('Commit the '+name+' changes before deploying or planning.')
            repositories.execute('checkout',repository=name,branch=branch if name=='aof-agent' else 'master')
            repositories.execute('push',repository=name)
            request[name] = git.run(['rev-parse','HEAD'],path)
            binary[filename] = base64.b64encode(archive.package(path,paths)).decode()
    deployment = kube.get('deployments','aof-agent-telegram')
    image = deployment['spec']['template']['spec']['containers'][0]['image']
    request['runtime_image'] = image
    (target/'request.json').write_text(json.dumps(request))
    (target/'status.json').write_text(json.dumps({'id':operation_id,'state':'queued','action':action}))
    source = kube.create({'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':request['source'],'namespace':'aof-agent'},
                          'data':{'request.json':json.dumps(request)},'binaryData':binary})
    try:
        job = kube.create(jobs.controller(request,image,source['metadata']['name']))
    except Exception:
        kube.run(['delete','configmap',request['source']])
        raise
    kube.patch('configmaps',request['source'],{'metadata':{'ownerReferences':[
        {'apiVersion':'batch/v1','kind':'Job','name':operation_id,'uid':job['metadata']['uid']}]}})
    return {'id':operation_id,'state':'queued','action':action,
            'message':'Detached job will report here when finished; it survives conversation-pod restarts.'}
