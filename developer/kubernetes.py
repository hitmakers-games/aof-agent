"""Developer kubectl access: own namespace writes, cluster workload reads."""
import json
import os
from pathlib import Path
import subprocess
import re

from gateway.redaction import redact,log_summary


def clean(value):
    if isinstance(value,list):return [clean(item) for item in value]
    if isinstance(value,dict):
        if value.get('kind')=='Secret':
            metadata={key:item for key,item in value.get('metadata',{}).items() if key in {'name','namespace','uid','labels'}}
            return {'kind':'Secret','metadata':metadata,'keys':list(value.get('data',{})),'values':'redacted'}
        if 'name' in value and 'value' in value and re.search('password|secret|token|key',str(value['name']),re.IGNORECASE):
            return {'name':value['name'],'value':'[redacted]'}
        return {key:(item if key in {'accountId','uid'} else '[redacted]' if any(part in key.lower() for part in ('password','token','clientsecret','authorization')) else clean(item))
                for key,item in value.items() if key not in {'managedFields','annotations'}}
    return redact(value) if isinstance(value,str) else value


class Kubernetes:
    def __init__(self):
        self.kubeconfig=Path('/transport/developer-control/kubeconfig.json')
        self.kubeconfig.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.kubeconfig.write_text(json.dumps({'apiVersion':'v1','kind':'Config',
            'clusters':[{'name':'agent','cluster':{'server':'https://kubernetes.default.svc','certificate-authority':'/run/secrets/kubernetes/ca.crt'}}],
            'users':[{'name':'developer','user':{'tokenFile':'/run/secrets/kubernetes/token'}}],
            'contexts':[{'name':'agent','context':{'cluster':'agent','user':'developer','namespace':'aof-agent'}}],'current-context':'agent'}))

    def execute(self,action,namespace='aof-agent',resource='deployments',name=None,manifest=None,patch=None,replicas=None,container=None,tail=100,command=None):
        mutating=action in {'apply','delete','patch','scale','restart','exec'}
        if mutating and namespace!='aof-agent':
            raise ValueError('Writes outside aof-agent require explicit production authorization through the operator.')
        if not isinstance(resource,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9.-]*',resource):
            raise ValueError('Invalid Kubernetes resource type')
        if not isinstance(namespace,str) or not namespace or namespace.startswith('-'):
            raise ValueError('Invalid namespace')
        if name is not None and (not isinstance(name,str) or name.startswith('-') or '/' in name):raise ValueError('Invalid resource name')
        args=['kubectl','--kubeconfig',str(self.kubeconfig),'--request-timeout=20s','-n',namespace]
        data=None
        if action=='get':
            args+=['get',resource]+([name] if name else [])+['-o','json']
        elif action=='logs':
            if not name:raise ValueError('Include the pod name.')
            args+=['logs',name,'--tail='+str(max(1,min(int(tail),300)))]
            if container:args+=['-c',container]
        elif action=='apply':
            if not isinstance(manifest,dict) or manifest.get('kind') in {'Namespace','ClusterRole','ClusterRoleBinding','CustomResourceDefinition'}:
                raise ValueError('Apply one namespaced manifest in aof-agent.')
            manifest.setdefault('metadata',{})['namespace']='aof-agent'
            self.safe_workload(manifest)
            args+=['apply','-f','-','-o','json'];data=json.dumps(manifest).encode()
        elif action=='patch':
            self.safe_workload(patch)
            args+=['patch',resource,name or '', '--type=merge','-p',json.dumps(patch),'-o','json']
        elif action=='delete':args+=['delete',resource,name or '', '--wait=false']
        elif action=='scale':args+=['scale',resource+'/'+(name or ''),'--replicas='+str(int(replicas))]
        elif action=='restart':args+=['rollout','restart',resource+'/'+(name or '')]
        elif action=='exec':
            if not name or not isinstance(command,list) or not command or any(not isinstance(v,str) for v in command):raise ValueError('Include the pod name and command argument list.')
            args+=['exec',name]+(['-c',container] if container else [])+['--',*command]
        else:raise ValueError('Unknown Kubernetes action')
        result=subprocess.run(args,input=data,capture_output=True,timeout=30,
            env={'PATH':os.environ['PATH'],'LANG':'C.UTF-8'})
        if result.returncode:
            raise ValueError(redact(result.stderr.decode('utf-8','replace'))[:1500])
        text=result.stdout.decode('utf-8','replace')
        if action=='logs' and namespace!='aof-agent':return {'summary':log_summary(text)}
        try:return clean(json.loads(text))
        except ValueError:return {'output':redact(text)[:32000]}

    @staticmethod
    def safe_workload(value):
        if isinstance(value,list):
            for item in value:Kubernetes.safe_workload(item)
        if not isinstance(value,dict):return
        for key,item in value.items():
            if key=='hostPath' or key in {'hostPID','hostIPC','hostNetwork','privileged'} and item:
                raise ValueError('Host-level access is protected; use ordinary aof-agent workloads.')
            if key=='add' and isinstance(item,list) and any(cap not in {'CHOWN','DAC_OVERRIDE','FOWNER','SETUID','SETGID'} for cap in item):
                raise ValueError('Host capabilities require separate operator authorization.')
            Kubernetes.safe_workload(item)
