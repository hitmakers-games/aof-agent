"""Fixed repository targets and task-local workspace names."""
from pathlib import Path
import re

from worker.run import TaskError


BASES = {name: ('master' if name in {'aof-agent', 'aof-infra', 'aof-content-manager'} else 'develop')
         for name in ('aof-back', 'aof-front', 'aof-play', 'aof-rustore',
                      'aof-agent', 'aof-content-manager', 'aof-infra', 'aof-apple')}


def repositories(value):
    names = value.split(',') if isinstance(value, str) else value
    if not isinstance(names, list) or not names or len(names) > len(BASES):
        raise ValueError('Choose a repository, for example: /task aof-agent description')
    if any(not isinstance(name, str) or name not in BASES for name in names):
        raise ValueError('Repositories: ' + ', '.join(BASES))
    return sorted(set(names))


def task_directory(task_id, attempt, root='/workspace/tasks'):
    if not re.fullmatch('[0-9a-f]{32}', task_id) or type(attempt) is not int or attempt < 1:
        raise TaskError('Invalid execution identifier')
    return Path(root) / task_id / str(attempt)


def branch(task_id):
    if not re.fullmatch('[0-9a-f]{32}', task_id):
        raise TaskError('Invalid task identifier')
    return 'agent/' + task_id
