"""OpenTofu runner with remote locking and saved, reviewed plans."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess

from gateway.redaction import redact


def environment(kubeconfig):
    result = {key:value for key,value in os.environ.items() if 'proxy' not in key.lower()}
    result.update(TF_IN_AUTOMATION='1', TF_INPUT='0', TF_VAR_kubeconfig_path=str(kubeconfig),
                  TF_VAR_kube_context='agent',
                  AWS_ACCESS_KEY_ID=Path('/run/secrets/terraform/access-key-id').read_text().strip(),
                  AWS_SECRET_ACCESS_KEY=Path('/run/secrets/terraform/secret-access-key').read_text().strip())
    # The canonical root also declares credentials for other modules. They are
    # unused by the targeted agent module and must never be copied into its jobs.
    for name in ('selectel_domain_name','selectel_username','selectel_password','selectel_project_id',
                 'postgres_s3_access_key','postgres_s3_secret_key','frontend_s3_access_key',
                 'frontend_s3_secret_key','registry_username','registry_password','content_manager_git_token',
                 'public_sites_backup_s3_access_key','public_sites_backup_s3_secret_key'):
        result['TF_VAR_'+name]='unused-by-agent-module'
    cache=Path('/workspace/.terraform-cache')
    cache.mkdir(exist_ok=True,mode=0o700)
    result['TF_PLUGIN_CACHE_DIR']=str(cache)
    return result


def run(root, arguments, env, timeout=600):
    process = subprocess.Popen(['tofu','-chdir='+str(root),*arguments],env=env,
                               stdout=subprocess.PIPE,stderr=subprocess.STDOUT,start_new_session=True)
    try:
        output = process.communicate(timeout=timeout)[0]
    except subprocess.TimeoutExpired:
        os.killpg(process.pid,signal.SIGINT)
        try:process.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid,signal.SIGKILL);process.communicate()
        raise ValueError('Terraform timed out; inspect the operation before retrying.') from None
    if process.returncode:
        raise ValueError(redact(output.decode('utf-8','replace'))[-2500:])
    return output


def initialize(root,env):
    run(root,['init','-input=false','-no-color','-backend-config=bucket=hitmakers-aof-infra-state'],env)


def plan(root,env,image=None,deploy=False):
    arguments = ['plan','-target=module.aof_agent','-input=false','-no-color','-lock-timeout=60s','-out=operation.plan']
    if image:arguments += ['-var=agent_image_digest='+image]
    run(root,arguments,env)
    document = json.loads(run(root,['show','-json','operation.plan'],env))
    for name,change in document.get('output_changes',{}).items():
        if name!='agent_runtime_image_digest' and change['actions'] not in (['no-op'],['read']):
            raise ValueError('Plan also changes a shared infrastructure output: '+name)
    changes = []
    for item in document.get('resource_changes',[]):
        actions = item['change']['actions']
        if actions in (['no-op'],['read']):continue
        address = item['address']
        if not address.startswith('module.aof_agent.'):
            raise ValueError('Plan changes infrastructure outside the requested agent module: '+address)
        if 'delete' in actions:
            raise ValueError('Destructive plan stopped: '+address+'. Existing volumes and data were preserved.')
        if deploy and address not in {'module.aof_agent.helm_release.agent["telegram"]','module.aof_agent.helm_release.agent["coding"]'}:
            raise ValueError('Self-deployment also changes infrastructure: '+address+'. Run and review a separate Terraform plan first.')
        changes.append({'address':address,'actions':actions})
    return {'changes':changes,'plan_sha256':hashlib.sha256((root/'operation.plan').read_bytes()).hexdigest()}


def apply(root,env,expected):
    if hashlib.sha256((root/'operation.plan').read_bytes()).hexdigest()!=expected:
        raise ValueError('Saved plan changed; create a new plan.')
    run(root,['apply','-input=false','-no-color','-lock-timeout=60s','operation.plan'],env)
