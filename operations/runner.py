"""Detached build/plan/apply, with rollback and Telegram reporting after restart."""
import json
import os
from pathlib import Path
import sys
import time

from gateway import access
from gateway.redaction import redact
from operations import archive, jobs, terraform
from operations.client import directory
from operations.kube import Kube
from telegram.storage import connect, put, queue_report, setting


WORKERS = ['aof-agent-coding-controller', 'aof-agent-coding-executor']


def save(db, request, state, **values):
    path = directory(request['id'])/'status.json'
    current = json.loads(path.read_text()) if path.exists() else {}
    current.update(id=request['id'], action=request['action'], state=state, updated=time.time(), **values)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(current))
    temporary.replace(path)
    with db:
        put(db, 'operation:'+request['id'], json.dumps(current))
        put(db, 'latest_operation', request['id'])
    print(json.dumps({'id':request['id'],'state':state}), flush=True)
    return current


def pause(db, kube, request):
    with db:
        db.execute('SELECT pg_advisory_xact_lock(3350005)')
        if setting(db,'self_deploy','') not in ('',request['id']):
            raise ValueError('Another infrastructure operation owns the deployment lock.')
        put(db,'self_deploy',request['id'])
    save(db,request,'waiting-for-active-work')
    deadline = time.monotonic()+1800
    while db.execute("SELECT count(*) FROM work_items WHERE state IN ('running','cancelling')").fetchone()[0]:
        if time.monotonic()>deadline:
            raise ValueError('Existing coding work did not finish; it was not interrupted.')
        time.sleep(3)
    replicas = {name:kube.get('deployments',name)['spec'].get('replicas',1) for name in WORKERS}
    save(db,request,'preparing',worker_replicas=replicas)
    for name in WORKERS:
        kube.scale(name,0)
    for name in WORKERS:
        kube.wait_deployment(name)


def wait_for_conversation(db):
    deadline = time.monotonic()+1800
    while db.execute("SELECT count(*) FROM inbox WHERE status IN ('running','cancelling') AND kind IN ('message','bug','task')").fetchone()[0]:
        if time.monotonic()>deadline:
            raise ValueError('The current conversation has not finished; self-deployment was not applied.')
        time.sleep(2)


def resume(db, kube, request):
    path = directory(request['id'])/'status.json'
    values = json.loads(path.read_text()) if path.exists() else {}
    for name,count in values.get('worker_replicas',{}).items():
        kube.scale(name,count)
    with db:
        if setting(db,'self_deploy','')==request['id']:
            put(db,'self_deploy','')


def build(db, kube, request):
    save(db,request,'building',build_job=request['id']+'-build')
    kube.create(jobs.build(request,request['runtime_image']))
    kube.wait_job(request['id']+'-build')
    pods = json.loads(kube.run(['get','pods','-l','job-name='+request['id']+'-build','-o','json']))['items']
    container = next(item for item in pods[0]['status']['containerStatuses'] if item['name']=='build')
    digest = container['state']['terminated']['message'].strip()
    import re
    if not re.fullmatch('sha256:[a-f0-9]{64}',digest):
        raise ValueError('Builder did not return an immutable image digest.')
    return digest


def execute(db, kube, request):
    access.require(db,request['actor'],'admin')
    target = directory(request.get('plan_id') or request['id'])
    if request['action']!='apply':
        with open('/source/infra.tar.gz','rb') as source:
            archive.unpack(source,target/'infra')
    root = target/'infra/k8s/selectel'
    env = terraform.environment(kube.config)
    save(db,request,'initializing')
    terraform.initialize(root,env)
    if request['action']=='plan':
        result = terraform.plan(root,env)
        save(db,request,'planned',**result)
        return 'Terraform plan ready: '+request['id']+'\n'+json.dumps(result['changes'])
    pause(db,kube,request)
    previous = request['runtime_image'].split('@')[1]
    if request['action']=='deploy':
        digest = build(db,kube,request)
        save(db,request,'planning-deployment',digest=digest,commit=request['aof-agent'])
        plan = terraform.plan(root,env,digest,deploy=True)
    else:
        plan = json.loads((target/'status.json').read_text())
    wait_for_conversation(db)
    access.require(db,request['actor'],'admin')
    save(db,request,'applying')
    try:
        terraform.apply(root,env,plan['plan_sha256'])
        kube.wait_deployment('aof-agent-telegram')
    except Exception:
        if request['action']=='deploy':
            save(db,request,'rolling-back')
            rollback = terraform.plan(root,env,previous,deploy=True)
            terraform.apply(root,env,rollback['plan_sha256'])
            kube.wait_deployment('aof-agent-telegram')
        raise
    save(db,request,'completed')
    return 'Self-deployment completed.' if request['action']=='deploy' else 'Terraform apply completed.'


def main():
    os.umask(0o077)
    request = json.loads(Path(sys.argv[1]).read_text())
    db, kube = connect('/unused'), Kube()
    succeeded=True
    try:
        message = execute(db,kube,request)
    except Exception as error:
        succeeded=False
        message = 'Operation '+request['id']+' failed: '+redact(str(error))[:1800]
        save(db,request,'failed',error=message)
    finally:
        if setting(db,'self_deploy','')==request['id']:
            resume(db,kube,request)
    with db:
        queue_report(db,message+'\n/ops '+request['id'],request['chat_id'],required_role='admin',
                     event_key='operation:'+request['id'],authorized_by=request['actor'])
    return 0 if succeeded else 1


if __name__=='__main__':sys.exit(main())
