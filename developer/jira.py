"""AOF Jira service-account access, with automatic short-lived OAuth tokens."""
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode
from urllib.request import Request,build_opener,ProxyHandler
from urllib.error import HTTPError

from repositories.github_app import NoRedirects


def document(text):
    return {'type':'doc','version':1,'content':[{'type':'paragraph','content':[{'type':'text','text':text}]}]}


class Jira:
    def __init__(self):
        self.config=json.loads(Path('/run/secrets/jira/credentials.json').read_text())
        c=self.config
        if c['tokenUrl']!='https://auth.atlassian.com/oauth/token' or c['apiBaseUrl']!='https://api.atlassian.com/ex/jira/'+c['cloudId']+'/rest/api/3':
            raise ValueError('Unexpected Jira credential endpoint')
        self.opener=build_opener(ProxyHandler({}),NoRedirects())
        self.token=None
        self.expires=0

    def request(self,path,body=None,method=None):
        try:
            if time.time()>self.expires:
                data=urlencode({'client_id':self.config['clientId'],'client_secret':self.config['clientSecret'],'grant_type':'client_credentials'}).encode()
                with self.opener.open(Request(self.config['tokenUrl'],data=data,headers={'Content-Type':'application/x-www-form-urlencoded'}),timeout=20) as response:token=json.load(response)
                self.token=token['access_token'];self.expires=time.time()+token['expires_in']-60
            request=Request(self.config['apiBaseUrl']+path,data=None if body is None else json.dumps(body).encode(),method=method,
                headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json','Accept':'application/json'})
            with self.opener.open(request,timeout=25) as response:
                raw=response.read(512*1024)
            return json.loads(raw) if raw else {'success':True}
        except HTTPError as error:
            raise ValueError(f'Jira returned HTTP {error.code}; credential and private error body omitted.') from None

    def execute(self,action,issue=None,query='',summary='',description='',account_id=None,transition_id=None,issue_type='Task'):
        if action=='myself':return self.request('/myself')
        if action=='users':return self.request('/user/assignable/search?'+urlencode({'project':self.config['projectKey'],'query':query,'maxResults':20}))
        if action=='search':
            parts=re.split(r'\border\s+by\b',query or 'statusCategory != Done',maxsplit=1,flags=re.IGNORECASE)
            jql='project = '+self.config['projectKey']+' AND ('+parts[0]+')'+(' ORDER BY '+parts[1] if len(parts)>1 else '')
            result=self.request('/search/jql',{'jql':jql,'fields':['summary','status','assignee','description'],'maxResults':30})
            result['issues']=[issue for issue in result.get('issues',[]) if issue['key'].startswith(self.config['projectKey']+'-')]
            return result
        if action=='create':
            if not summary:raise ValueError('Include the task summary.')
            fields={'project':{'key':self.config['projectKey']},'summary':summary[:255],
                    'issuetype':{'name':issue_type},'description':document(description or summary)}
            if account_id:fields['assignee']={'accountId':self.config['accountId'] if account_id=='self' else account_id}
            return self.request('/issue',{'fields':fields})
        if not issue or not re.fullmatch(re.escape(self.config['projectKey'])+r'-[1-9][0-9]*',issue):
            raise ValueError('Use an issue key from the configured AOF project.')
        path='/issue/'+issue
        if action=='get':return self.request(path+'?fields=summary,description,status,assignee,comment')
        if action=='update':
            fields={}
            if summary:fields['summary']=summary[:255]
            if description:fields['description']=document(description)
            return self.request(path,{'fields':fields},'PUT')
        if action=='comment':return self.request(path+'/comment',{'body':document(description)})
        if action=='assign':return self.request(path+'/assignee',{'accountId':self.config['accountId'] if account_id=='self' else account_id},'PUT')
        if action=='transitions':return self.request(path+'/transitions')
        if action=='transition':return self.request(path+'/transitions',{'transition':{'id':str(transition_id)}})
        raise ValueError('Unknown Jira action')
