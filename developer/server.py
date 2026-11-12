"""Trusted stdio tools for the developer conversation; no credentials in replies."""
import sys
sys.path.insert(0,'/opt/aof-agent')

import json
import os

from developer.git_tools import Repositories
from developer.jira import Jira
from developer.kubernetes import Kubernetes,clean
from developer.session import workspace
from gateway import access
from gateway.redaction import redact
from telegram.history import read,chats
from telegram.storage import connect


def schema(properties,required=()):
    return {'type':'object','properties':properties,'required':list(required),'additionalProperties':False}


STRING={'type':'string'}
TOOLS=[
    {'name':'capabilities','description':'Read actual developer identity and connected capabilities. Use before claiming missing access.','inputSchema':schema({})},
    {'name':'git','description':'List/checkout corporate repositories, inspect diff/log/status, fetch, commit and push the working branch. Checkout accepts branch, for example a Jira task key. Existing edits are preserved. No PRs. Shell edits and tests use returned checkout paths. Set deployment_reviewed only after inspecting workflow triggers and verifying that the push is authorized.','inputSchema':schema({'action':{'type':'string','enum':['list','checkout','status','diff','log','fetch','commit','push']},'repository':STRING,'message':STRING,'branch':STRING,'deployment_reviewed':{'type':'boolean'}},['action'])},
    {'name':'jira','description':'Read/search/create/update/comment/assign/transition AOF Jira tasks; users searches assignable teammates. account_id=self assigns to this developer. Only create tasks when requested.','inputSchema':schema({'action':{'type':'string','enum':['myself','users','search','get','create','update','comment','assign','transitions','transition']},'issue':STRING,'query':STRING,'summary':STRING,'description':STRING,'account_id':STRING,'transition_id':STRING,'issue_type':STRING},['action'])},
    {'name':'kubernetes','description':'Inspect cluster workloads/logs and administer aof-agent: apply, patch, delete, scale, restart, or exec commands in its pods. Secrets are redacted. Production writes are protected.','inputSchema':schema({'action':{'type':'string','enum':['get','logs','apply','delete','patch','scale','restart','exec']},'namespace':STRING,'resource':STRING,'name':STRING,'manifest':{'type':'object','additionalProperties':True},'patch':{'type':'object','additionalProperties':True},'replicas':{'type':'integer','minimum':0,'maximum':5},'container':STRING,'tail':{'type':'integer','minimum':1,'maximum':300},'command':{'type':'array','items':STRING,'maxItems':40}},['action'])},
    {'name':'telegram_chats','description':'List conversations whose messages this bot has received and saved. Administrators can discover all retained chats.','inputSchema':schema({})},
    {'name':'telegram_history','description':'Read/search all retained Telegram messages, including unmentioned group messages. Administrators may select any connected chat. Use next_before_message_id to page through older messages without a fixed total limit.','inputSchema':schema({'chat_id':{'type':'integer'},'query':STRING,'limit':{'type':'integer','minimum':1,'maximum':150},'before_message_id':{'type':'integer'}})}]


class Developer:
    def __init__(self):
        self.actor=int(os.environ['AOF_MCP_ACTOR']);self.chat=int(os.environ['AOF_MCP_CHAT'])
        self.db=connect('/unused')
        self.repositories=None;self.jira=None;self.kubernetes=None

    def call(self,name,args):
        access.require(self.db,self.actor,'operator')
        if name=='capabilities':
            return {'role':access.role(self.db,self.actor),'workspace':str(workspace(self.actor,self.chat)),
                    'shell':'workspace edits and commands enabled','git':'all corporate repositories; working branch commits and pushes',
                    'jira':'AOF read and write','kubernetes':'aof-agent read/write; cluster workload reads','telegram':'connected conversation history available','pull_requests':'disabled'}
        if name=='git':
            if self.repositories is None:self.repositories=Repositories(workspace(self.actor,self.chat))
            result=self.repositories.execute(**args)
        elif name=='jira':
            if self.jira is None:self.jira=Jira(self.db)
            if args.get('action') in {'create','comment'}:
                from developer.effects import once
                result=once(self.db,os.environ.get('AOF_MCP_REQUEST','0'),'jira.'+args['action'],args,lambda:self.jira.execute(**args))
            else:result=self.jira.execute(**args)
        elif name=='kubernetes':
            if args.get('action') in {'apply','delete','patch','scale','restart','exec'}:access.require(self.db,self.actor,'admin')
            if self.kubernetes is None:self.kubernetes=Kubernetes()
            result=self.kubernetes.execute(**args)
        elif name=='telegram_history':result=read(self.db,self.actor,self.chat,**args)
        elif name=='telegram_chats':result=chats(self.db,self.actor,self.chat)
        else:raise ValueError('Unknown developer tool')
        with self.db:access.audit(self.db,self.actor,'tool.'+name,args.get('action','read'))
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
