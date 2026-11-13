"""Trusted stdio tools for the developer conversation; no credentials in replies."""
import sys
sys.path.insert(0,'/opt/aof-agent')

import json
import os
import time

from developer.git_tools import Repositories
from developer.jira import Jira
from developer.kubernetes import Kubernetes,clean
from developer.session import workspace
from gateway import access
from gateway.redaction import redact
from telegram.history import read,chats
from telegram.storage import connect,put


def schema(properties,required=()):
    return {'type':'object','properties':properties,'required':list(required),'additionalProperties':False}


STRING={'type':'string'}
TOOLS=[
    {'name':'settings','description':'Admin only: change the shared model or reasoning level, or grant/revoke Telegram user roles. Read or list access with action=users.','inputSchema':schema({'action':{'type':'string','enum':['model','reasoning','grant','revoke','users','pause','resume']},'value':STRING},['action'])},
    {'name':'work','description':'The shared work queue. Use start only when the user explicitly requests queued or background work. Ordinary conversation and direct development do not require a task. Reuse reference for an existing Jira/Telegram task. Delete hides a closed local queue item while retaining its record and audit history; it never deletes an external Jira issue. Only set notify when asked for reports. Watch/unwatch subscribes this chat to a task, mine, all (admin), or jira; minutes=0 means changes only. Status includes the saved result. Background tasks use progress with a short description at meaningful stages.','inputSchema':schema({'action':{'type':'string','enum':['start','list','status','cancel','delete','watch','unwatch','progress']},'reference':STRING,'title':STRING,'description':STRING,'repositories':{'type':'array','items':STRING},'notify':{'type':'boolean'},'topic':{'type':'string','enum':['mine','all','jira']},'minutes':{'type':'integer','minimum':0,'maximum':1440},'events':{'type':'string','enum':['changes','finished']},'delivery':{'type':'string','enum':['answer','changes','published','deployed']}},['action'])},
    {'name':'memory','description':'Read shared versioned project memory or immediately remember a confirmed nonsecret fact in aof-agent master. Admins may write; never save secrets, personal information, or raw chat transcripts. Edit shared rules directly with Git when asked.','inputSchema':schema({'action':{'type':'string','enum':['read','remember']},'text':STRING},['action'])},
    {'name':'health','description':'Safe service availability, without logs or sensitive configuration. Available to viewers.','inputSchema':schema({})},
    {'name':'capabilities','description':'Read actual developer identity and connected capabilities. Use before claiming missing access.','inputSchema':schema({})},
    {'name':'self_deploy','description':'Build and deploy committed aof-agent code in a detached job, with rollback and Telegram reporting. Defaults to master; branch selects another committed app branch. It pulls and publishes clean checkouts first. Start returns immediately; acknowledge the queued job and finish the chat turn so it can restart this pod. Use status or /ops for progress.','inputSchema':schema({'action':{'type':'string','enum':['start','status']},'operation_id':STRING,'branch':STRING,'task':STRING,'dockerfile':{'type':'string','enum':['worker/app.Dockerfile','worker/Dockerfile']}},['action'])},
    {'name':'terraform','description':'Plan/apply the agent module from aof-infra/k8s/selectel using the EXISTING company S3 state aof-k8s.tfstate and remote locking. Plan snapshots committed source; apply takes that saved operation_id. Destructive or unrelated changes are stopped. Runs outside this conversation and reports to Telegram.','inputSchema':schema({'action':{'type':'string','enum':['plan','apply','status']},'operation_id':STRING},['action'])},
    {'name':'git','description':'List/checkout corporate repositories, inspect diff/log/status, fetch, commit and push the working branch. Checkout accepts branch, for example a Jira task key. Existing edits are preserved. task selects an existing task checkout for recovery or follow-up, without creating a new task. No PRs. Shell edits and tests use returned checkout paths. Set deployment_reviewed only after inspecting workflow triggers and verifying that the push is authorized.','inputSchema':schema({'action':{'type':'string','enum':['list','checkout','status','diff','log','fetch','commit','push']},'repository':STRING,'message':STRING,'branch':STRING,'task':STRING,'deployment_reviewed':{'type':'boolean'}},['action'])},
    {'name':'jira','description':'Read/search/create/update/comment/assign/transition AOF Jira tasks; users searches assignable teammates. account_id=self assigns to this developer. Only create tasks when requested.','inputSchema':schema({'action':{'type':'string','enum':['myself','users','search','get','create','update','comment','assign','transitions','transition']},'issue':STRING,'query':STRING,'summary':STRING,'description':STRING,'account_id':STRING,'transition_id':STRING,'issue_type':STRING},['action'])},
    {'name':'jira_upload','description':'Attach a received Telegram screenshot/document to a Jira issue. Use message_id from Telegram attachment context or history, with optional zero-based index. Only when the user requests attaching it.','inputSchema':schema({'issue':STRING,'message_id':{'type':'integer'},'index':{'type':'integer','minimum':0}},['issue','message_id'])},
    {'name':'jira_attachment','description':'Send a Jira attachment to the current Telegram chat when the user requests it. Omit attachment_id only for a single attachment.','inputSchema':schema({'issue':STRING,'attachment_id':STRING},['issue'])},
    {'name':'kubernetes','description':'Inspect cluster workloads/logs and administer aof-agent: apply, patch, delete, scale, restart, or exec commands in its pods. Secrets are redacted. Production writes are protected.','inputSchema':schema({'action':{'type':'string','enum':['get','logs','apply','delete','patch','scale','restart','exec']},'namespace':STRING,'resource':STRING,'name':STRING,'manifest':{'type':'object','additionalProperties':True},'patch':{'type':'object','additionalProperties':True},'replicas':{'type':'integer','minimum':0,'maximum':5},'container':STRING,'tail':{'type':'integer','minimum':1,'maximum':300},'command':{'type':'array','items':STRING,'maxItems':40}},['action'])},
    {'name':'telegram_chats','description':'List conversations whose messages this bot has received and saved. Administrators can discover all retained chats.','inputSchema':schema({})},
    {'name':'telegram_history','description':'Read/search all retained Telegram messages, including unmentioned group messages. Administrators may select any connected chat. Use next_before_message_id to page through older messages without a fixed total limit.','inputSchema':schema({'chat_id':{'type':'integer'},'query':STRING,'limit':{'type':'integer','minimum':1,'maximum':150},'before_message_id':{'type':'integer'}})}]


class Developer:
    def __init__(self):
        self.actor=int(os.environ['AOF_MCP_ACTOR']);self.chat=int(os.environ['AOF_MCP_CHAT'])
        self.db=connect('/unused')
        self.work_id=os.environ.get('AOF_MCP_WORK') or None
        self.root=workspace(self.actor,self.chat,self.work_id)
        self.repositories=None;self.jira=None;self.kubernetes=None

    def call(self,name,args):
        access.require(self.db,self.actor,'viewer')
        if name=='settings':
            access.require(self.db,self.actor,'admin')
            action=args['action']
            with self.db:
                if action in {'pause','resume'}:
                    from gateway.commands import dispatch
                    return dispatch(self.db,0,'/'+action,self.actor,chat_id=self.chat)
                if action in {'model','reasoning'}:
                    from gateway.models import command
                    return command(self.db,self.actor,self.chat,'/'+action,args.get('value',''))
                return access.command(self.db,self.actor,'/'+action,args.get('value',''))
        if name=='work':
            if args['action']=='progress':
                access.require(self.db,self.actor,'operator')
                if not self.work_id:raise ValueError('Progress belongs to a running background task.')
                with self.db:
                    self.db.execute("UPDATE work_items SET checkpoint=?,updated_at=? WHERE id=? AND state='running' AND approved_by=?",
                        (args.get('description','')[:500],time.time(),self.work_id,self.actor))
                return {'status':'progress saved'}
            from developer.work_tools import execute
            with self.db:
                if args['action']=='start':
                    from gateway.models import for_request
                    selected=for_request(self.db,os.environ.get('AOF_MCP_REQUEST','0'),self.chat)
                    return execute(self.db,self.actor,self.chat,model_options=selected,
                        request_id=os.environ.get('AOF_MCP_REQUEST'),**args)
                return execute(self.db,self.actor,self.chat,**args)
        if name=='memory':
            access.require(self.db,self.actor,'operator')
            from gateway.agent_memory import context,remember
            if args['action']=='read':return context()
            access.require(self.db,self.actor,'admin')
            return remember(args.get('text',''))
        if name=='health':
            from telegram.storage import setting
            result={'chat':('degraded' if setting(self.db,'engine_state')=='degraded' else 'online') if time.time()-float(setting(self.db,'engine_heartbeat','0'))<180 else 'offline',
                    'background':'online' if time.time()-float(setting(self.db,'coding_heartbeat','0'))<180 else 'offline',
                    'active_tasks':self.db.execute("SELECT count(*) FROM work_items WHERE state IN ('queued','running','cancelling')").fetchone()[0]}
            return result
        if name=='capabilities':
            runtime={'model':os.environ.get('AOF_MCP_MODEL'),'effort':os.environ.get('AOF_MCP_REASONING')}
            if not access.allowed(self.db,self.actor,'operator'):
                return {'role':'viewer','runtime':runtime,'tools':['health','work.list','work.status']}
            return {'role':access.role(self.db,self.actor),'workspace':str(self.root),
                    'runtime':runtime,
                    'work':'shared queue; background tasks; opt-in reports',
                    'shell':'workspace edits and commands enabled','git':'all corporate repositories; working branch commits and pushes',
                    'jira':'AOF read and write','kubernetes':'aof-agent read/write; cluster workload reads' if access.allowed(self.db,self.actor,'admin') else 'read-only workloads and log summaries',
                    'self_deployment':'detached build, Terraform apply, rollback and restart reporting',
                    'terraform':'agent module in the existing aof-k8s.tfstate',
                    'telegram':'connected conversation history available to permitted roles','pull_requests':'disabled',
                    'permissions':'admin: full agent control; operator: regular work only; viewer: safe status/health only'}
        access.require(self.db,self.actor,'operator')
        if name in {'self_deploy','terraform'}:
            if args.get('action')!='status':access.require(self.db,self.actor,'admin')
            from operations.client import execute
            values=dict(args)
            if name=='self_deploy' and values['action']=='start':values['action']='deploy'
            reference=values.pop('task',None)
            selected_work=self.work_id
            if reference:
                from gateway.work import get
                access.require(self.db,self.actor,'admin')
                item=get(self.db,reference)
                if not item:raise ValueError('Task checkout not found')
                selected_work=item['id']
            result=execute(self.actor,self.chat,work_id=selected_work,**values)
            if name=='self_deploy' and result.get('state')=='queued':
                with self.db:put(self.db,'request-operation:'+os.environ.get('AOF_MCP_REQUEST','0'),result['id'])
        elif name=='git':
            if args.get('repository') in {'aof-agent','aof-infra'} and args.get('action') in {'commit','push'}:
                access.require(self.db,self.actor,'admin')
            reference=args.pop('task',None)
            root=self.root
            if reference:
                from gateway.work import get,require_item
                item=get(self.db,reference) if access.allowed(self.db,self.actor,'admin') else require_item(self.db,self.actor,reference,'operator')
                if not item:raise ValueError('Task checkout not found')
                root=workspace(self.actor,self.chat,item['id'])
            if self.repositories is None or self.repositories.root!=root:self.repositories=Repositories(root)
            result=self.repositories.execute(**args)
            from developer import artifacts
            request=os.environ.get('AOF_MCP_REQUEST','0')
            if args['action']=='checkout':artifacts.checkout(self.db,request,args['repository'],self.repositories.root/args['repository'])
            if args['action']=='push':artifacts.published(self.db,request,args['repository'],self.repositories.root/args['repository'])
        elif name=='jira':
            if self.jira is None:self.jira=Jira()
            if args.get('action') in {'create','comment'}:
                from developer.effects import once
                result=once(self.db,os.environ.get('AOF_MCP_REQUEST','0'),'jira.'+args['action'],args,lambda:self.jira.execute(**args))
            else:result=self.jira.execute(**args)
        elif name in {'jira_attachment','jira_upload'}:
            from developer import media
            if self.jira is None:self.jira=Jira()
            if name=='jira_attachment':
                result=media.send(self.db,self.jira,self.chat,os.environ.get('AOF_MCP_REQUEST','0'),args)
            else:result=media.upload(self.db,self.jira,self.chat,args)
        elif name=='kubernetes':
            if args.get('action') in {'apply','delete','patch','scale','restart','exec'}:
                access.require(self.db,self.actor,'admin')
            if self.kubernetes is None:self.kubernetes=Kubernetes()
            result=self.kubernetes.execute(**args)
        elif name=='telegram_history':result=read(self.db,self.actor,self.chat,**args)
        elif name=='telegram_chats':result=chats(self.db,self.actor,self.chat)
        else:raise ValueError('Unknown developer tool')
        with self.db:
            if name in {'self_deploy','terraform'} and result.get('state')=='queued':
                self.db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO NOTHING',
                    ('operation:'+result['id'],json.dumps({**result,'updated':time.time()})))
                put(self.db,'latest_operation',result['id'])
            access.audit(self.db,self.actor,'tool.'+name,args.get('action','read'))
        return clean(result)


def main():
    os.umask(0o077)
    developer=Developer()
    for line in sys.stdin:
        if len(line)>1024*1024:continue
        try:
            request=json.loads(line)
            if 'id' not in request:continue
            method=request.get('method')
            if method=='initialize':result={'protocolVersion':request.get('params',{}).get('protocolVersion','2024-11-05'),'capabilities':{'tools':{}},'serverInfo':{'name':'aof-developer','version':'1.0'}}
            elif method=='ping':result={}
            elif method=='tools/list':result={'tools':TOOLS}
            elif method=='tools/call':
                try:
                    params=request['params'];data=developer.call(params['name'],params.get('arguments',{}))
                    result={'content':[{'type':'text','text':json.dumps(data,ensure_ascii=False)[:64000]}]}
                except Exception as error:
                    message=redact(str(error)) if isinstance(error,ValueError) else 'Developer tool failed; credentials and private details omitted.'
                    result={'content':[{'type':'text','text':message[:2000]}],'isError':True}
            else:
                print(json.dumps({'jsonrpc':'2.0','id':request['id'],'error':{'code':-32601,'message':'Method not found'}}),flush=True);continue
            print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}),flush=True)
        except (ValueError,KeyError,TypeError):continue


if __name__=='__main__':main()
