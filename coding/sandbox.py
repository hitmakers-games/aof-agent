"""One permission profile for model commands and independent verification."""
import json
from pathlib import Path
import subprocess
import tempfile


def options(root, names, network=False):
    rules = {str(root): 'write', '/run/secrets': 'deny', '/state/auth/auth.json': 'deny'}
    tasks = Path('/workspace/tasks')
    if tasks.exists():
        for path in tasks.iterdir():
            if path != root and path not in root.parents:
                rules[str(path)] = 'deny'
        if root.parent != tasks:
            for path in root.parent.iterdir():
                if path != root:
                    rules[str(path)] = 'deny'
    for name in names:
        for protected in ('.git', '.codex', '.agents', '.aws', 'AGENTS.md'):
            rules[str(root / name / protected)] = 'read'
    filesystem = '{' + ','.join(json.dumps(path) + '=' + json.dumps(access) for path,access in rules.items()) + '}'
    return ['-c','default_permissions="coding"','-c','permissions.coding.extends=":workspace"',
            '-c','permissions.coding.filesystem='+filesystem,
            '-c','permissions.coding.network.enabled='+str(network).lower()]


def preflight():
    with tempfile.TemporaryDirectory(prefix='.metadata-check-',dir='/workspace/tasks') as directory:
        root=Path(directory)
        (root/'nested/.git').mkdir(parents=True)
        (root/'nested/.git/config').write_text('public canary')
        code='''import errno
from pathlib import Path
p=Path('nested/.git/config')
assert p.read_text()=='public canary'
try:p.write_text('changed')
except OSError as error:
    assert error.errno in (errno.EROFS,errno.EACCES,errno.EPERM)
else:raise SystemExit('Git metadata was writable')
Path('allowed').write_text('approved workspace write')
'''
        subprocess.run(['codex','sandbox','--include-managed-config','-P','coding','-C',str(root),
                        *options(root,['nested']),'--','python3','-c',code],check=True,timeout=30,
                       stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL)
