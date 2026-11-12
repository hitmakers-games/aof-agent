"""Small detached controller and bounded Kaniko build jobs in aof-agent."""

BUILDER = 'ghcr.io/osscontainertools/kaniko@sha256:e7cba1f7e70c01c8ee1cbe622d07990c04dea550430c4c81319d26679226cd37'
REPOSITORY = 'cr.selcloud.ru/aof-registry/aof-agent-worker'


def base(name, controller=True):
    labels = {'aof-agent/operation': 'true', 'aof-agent/role': 'controller' if controller else 'build'}
    if controller:
        labels['app.kubernetes.io/part-of'] = 'aof-agent-db-client'
        labels['aof-agent/workspace'] = 'true'
    return {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {'name': name, 'namespace': 'aof-agent', 'labels': labels},
            'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 5400 if controller else 900,
                     'ttlSecondsAfterFinished': 7200, 'template': {'metadata': {'labels': labels}, 'spec': {
                         'restartPolicy': 'Never', 'automountServiceAccountToken': False,
                         'imagePullSecrets': [{'name': 'selectel-registry'}],
                         'securityContext': {'fsGroup': 10001, 'seccompProfile': {'type': 'RuntimeDefault'}}}}}}


def controller(request, image, source):
    job = base(request['id'])
    pod = job['spec']['template']['spec']
    pod['serviceAccountName'] = 'aof-agent-telegram-developer'
    pod['affinity'] = {'podAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': [{
        'labelSelector': {'matchLabels': {'app.kubernetes.io/instance': 'aof-agent-telegram'}},
        'topologyKey': 'kubernetes.io/hostname'}]}}
    pod['containers'] = [{'name': 'operation', 'image': image,
        'command': ['python3', '-m', 'operations.runner', '/source/request.json'],
        'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'runAsGroup': 10001,
                            'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}},
        'resources': {'requests': {'cpu': '50m', 'memory': '128Mi'}, 'limits': {'cpu': '500m', 'memory': '768Mi'}},
        'env': [{'name': 'AOF_OPERATION_KUBECONFIG', 'value': '/tmp/kubeconfig.json'},
                {'name': 'AOF_DATABASE_DSN_FILE', 'value': '/run/secrets/database/uri'},
                {'name': 'AOF_DATABASE_CA_FILE', 'value': '/run/secrets/database/ca.crt'}],
        'volumeMounts': [{'name': name, 'mountPath': path, **({'readOnly': True} if name not in {'workspace', 'tmp'} else {})}
                         for name, path in [('source', '/source'), ('workspace', '/workspace'), ('tmp', '/tmp'),
                                            ('database', '/run/secrets/database'), ('backend', '/run/secrets/terraform'),
                                            ('kubernetes', '/run/secrets/kubernetes')]]}]
    pod['volumes'] = [{'name': 'source', 'configMap': {'name': source}},
        {'name': 'workspace', 'persistentVolumeClaim': {'claimName': 'aof-agent-workspace'}},
        {'name': 'tmp', 'emptyDir': {'sizeLimit': '512Mi'}},
        {'name': 'backend', 'secret': {'secretName': 'aof-agent-terraform-backend', 'defaultMode': 0o440}},
        {'name': 'database', 'projected': {'defaultMode': 0o440, 'sources': [
            {'secret': {'name': 'aof-agent-db-app', 'items': [{'key': 'uri', 'path': 'uri'}]}},
            {'secret': {'name': 'aof-agent-db-ca', 'items': [{'key': 'ca.crt', 'path': 'ca.crt'}]}}]}},
        {'name': 'kubernetes', 'projected': {'defaultMode': 0o440, 'sources': [
            {'serviceAccountToken': {'path': 'token', 'expirationSeconds': 3600}},
            {'configMap': {'name': 'kube-root-ca.crt', 'items': [{'key': 'ca.crt', 'path': 'ca.crt'}]}}]}}]
    return job


def build(request, image):
    job = base(request['id']+'-build', controller=False)
    pod = job['spec']['template']['spec']
    pod['initContainers'] = [{'name': 'source', 'image': image,
        'command': ['python3', '-m', 'operations.archive', '/source/app.tar.gz', '/context'],
        'securityContext': {'runAsUser': 10001, 'runAsGroup': 10001, 'allowPrivilegeEscalation': False,
                            'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}},
        'resources': {'requests': {'cpu': '25m', 'memory': '32Mi'}, 'limits': {'cpu': '100m', 'memory': '128Mi'}},
        'volumeMounts': [{'name': 'source', 'mountPath': '/source', 'readOnly': True},
                         {'name': 'context', 'mountPath': '/context'}]}]
    pod['containers'] = [{'name': 'build', 'image': BUILDER,
        'args': ['--context=dir:///context', '--dockerfile=/context/'+request['dockerfile'],
                 '--destination='+REPOSITORY+':'+request['id'], '--digest-file=/dev/termination-log',
                 '--cache=false', '--push-retry=1'],
        'securityContext': {'runAsUser': 0, 'allowPrivilegeEscalation': False,
            'capabilities': {'drop': ['ALL'], 'add': ['CHOWN', 'DAC_OVERRIDE', 'FOWNER', 'SETUID', 'SETGID']}},
        'resources': {'requests': {'cpu': '500m', 'memory': '512Mi'},
                      'limits': {'cpu': '1', 'memory': '2Gi', 'ephemeral-storage': '3Gi'}},
        'volumeMounts': [{'name': 'context', 'mountPath': '/context'},
                         {'name': 'registry', 'mountPath': '/kaniko/.docker', 'readOnly': True}]}]
    pod['volumes'] = [{'name': 'source', 'configMap': {'name': request['source']}},
                      {'name': 'context', 'emptyDir': {'sizeLimit': '64Mi'}},
                      {'name': 'registry', 'secret': {'secretName': 'selectel-registry',
                          'items': [{'key': '.dockerconfigjson', 'path': 'config.json'}]}}]
    return job
