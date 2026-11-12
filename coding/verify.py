"""Require observed verification commands; independently check the gateway suite."""
import os
from pathlib import Path
import re
import subprocess

from worker.run import TaskError, child_environment
from coding.sandbox import options


def check(root, names, commands):
    observed = [entry for entry in commands if entry.get('exit_code') == 0 and
                re.search(r'\b(test|pytest|unittest|mvn|gradle|vitest|jest|tsc|build)\b', entry.get('command',''))]
    if not observed:
        raise TaskError('No successful verification command was observed; publication held.')
    if 'aof-agent' not in names:
        return
    directory = root / 'aof-agent'
    modules = ['telegram.test_bot','worker.test_chat','worker.test_run','gateway.test_gateway',
               'gateway.test_access','gateway.test_workflow','gateway.test_creation','repositories.test_repositories']
    if (directory/'coding/test_coding.py').exists():
        modules.append('coding.test_coding')
    env = child_environment(os.environ)
    env['PYTHONPATH'] = '.:repositories'
    args = ['codex','sandbox','--include-managed-config','-P','coding','-C',str(directory),*options(root,names),
            '--','python3','-m','unittest',*modules]
    # The independently repeated check remains inside Codex's enforced sandbox.
    try:
        result = subprocess.run(args,env=env,capture_output=True,timeout=120)
    except (OSError,subprocess.TimeoutExpired):
        raise TaskError('Independent gateway verification did not finish; publication held.') from None
    if result.returncode:
        raise TaskError('Independent gateway regression suite failed; publication held.')
