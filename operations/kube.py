"""Internal namespace client for detached operations; credentials stay in files."""
import json
import os
import subprocess
import time

from developer.kubernetes import Kubernetes


class Kube:
    def __init__(self):
        self.config = Kubernetes(os.environ.get('AOF_OPERATION_KUBECONFIG')).kubeconfig

    def run(self, arguments, value=None, timeout=45):
        command = ['kubectl', '--kubeconfig', str(self.config), '--request-timeout=30s', '-n', 'aof-agent', *arguments]
        result = subprocess.run(command, input=None if value is None else json.dumps(value).encode(),
                                capture_output=True, timeout=timeout,
                                env={'PATH': os.environ['PATH'], 'LANG': 'C.UTF-8'})
        if result.returncode:
            from gateway.redaction import redact
            raise ValueError(redact(result.stderr.decode('utf-8', 'replace'))[:1500])
        return result.stdout.decode('utf-8', 'replace')

    def get(self, resource, name=None):
        return json.loads(self.run(['get', resource, *([name] if name else []), '-o', 'json']))

    def create(self, value):
        return json.loads(self.run(['create', '-f', '-', '-o', 'json'], value))

    def patch(self, resource, name, value):
        self.run(['patch', resource, name, '--type=merge', '-p', json.dumps(value)])

    def scale(self, name, replicas):
        self.run(['scale', 'deployment/'+name, '--replicas='+str(replicas)])

    def wait_job(self, name, timeout=900):
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            job = self.get('jobs', name)
            if job.get('status', {}).get('succeeded'):
                return
            if job.get('status', {}).get('failed'):
                raise ValueError('Job failed. Inspect '+name+'.')
            time.sleep(3)
        raise ValueError('Job timed out: '+name)

    def wait_deployment(self, name, timeout=240, stable_seconds=0):
        deadline = time.monotonic()+timeout
        ready_since = None
        while time.monotonic()<deadline:
            value = self.get('deployments', name)
            status, count = value.get('status', {}), value['spec'].get('replicas', 1)
            if (status.get('observedGeneration', 0)>=value['metadata']['generation']
                    and status.get('updatedReplicas', 0)==count and status.get('readyReplicas', 0)==count):
                if ready_since is None:
                    ready_since = time.monotonic()
                if time.monotonic()-ready_since >= stable_seconds:
                    return
            else:
                ready_since = None
            time.sleep(3)
        raise ValueError('Rollout timed out: '+name)
