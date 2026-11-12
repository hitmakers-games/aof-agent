"""Trusted Git operations; repository hooks and credential storage are disabled."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

from coding.config import BASES, branch, task_directory
from worker.run import TaskError


def run(args, cwd=None, token=None, timeout=180, data=None):
    env = {key: value for key, value in os.environ.items()
           if key in {'PATH', 'LANG', 'SSL_CERT_FILE'}}
    env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
               GIT_ALLOW_PROTOCOL='https:file', GIT_LFS_SKIP_SMUDGE='1', GIT_OPTIONAL_LOCKS='0')
    if token:
        env['AOF_GIT_TOKEN'] = token
        env['GIT_ASKPASS'] = '/opt/aof-agent/coding/askpass.sh'
    command = ['git', '-c', 'credential.helper=', '-c', 'core.hooksPath=/dev/null',
               '-c', 'core.fsmonitor=false', '-c', 'http.followRedirects=false',
               '-c', 'commit.gpgsign=false', '-c', 'tag.gpgsign=false',
               '-c', 'pack.threads=1', '-c', 'index.threads=1', *args]
    try:
        result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, timeout=timeout, input=data)
    except (OSError,subprocess.SubprocessError):
        raise TaskError('Git operation did not finish; existing files preserved.') from None
    if result.returncode:
        raise TaskError('Git operation failed; checkout preserved and credentials omitted.')
    return result.stdout.decode('utf-8', 'replace').strip()


def manifest(root):
    # This path is outside the executor's tasks-only persistent volume mount.
    return Path('/workspace/.coding-control') / root.parent.name / (root.name + '.json')


def verify_metadata(root, name):
    directory = root / name
    expected = json.loads(manifest(root).read_text())[name]
    if directory.is_symlink() or (directory/'.git').is_symlink() or (directory/'.git/config').is_symlink():
        raise TaskError('Checkout metadata replaced; manual review required.')
    actual = hashlib.sha256((directory/'.git/config').read_bytes()).hexdigest()
    if actual != expected['config']:
        raise TaskError('Git configuration changed during execution; publication stopped.')
    if 'head' in expected and run(['rev-parse','HEAD'],directory) != expected['head']:
        raise TaskError('Git history differs from the trusted checkpoint; manual review required.')


def prepare(app, item):
    if shutil.disk_usage('/workspace').free < 1024 * 1024 * 1024:
        raise TaskError('Workspace has less than 1 GiB free. Expand storage or review old task workspaces before retrying.')
    root = task_directory(item['id'], item['attempt'])
    if root.exists():
        raise TaskError('Execution workspace already exists; refusing to overwrite it.')
    root.mkdir(parents=True, mode=0o700)
    baseline = {}
    for name in json.loads(item['repositories']):
        base = BASES[name]
        remote = f'https://github.com/hitmakers-games/{name}.git'
        target = root / name
        old_root = task_directory(item['id'], item['attempt'] - 1) if item['attempt'] > 1 else None
        if old_root and (old_root/name).exists() and manifest(old_root).exists():
            verify_metadata(old_root, name)
            base_head = json.loads(manifest(old_root).read_text())[name]['base_head']
            old = old_root / name
            names = changes(old)
            run(['clone', '--no-hardlinks', '--single-branch', '--branch', branch(item['id']), '--', str(old), str(target)])
            run(['remote', 'set-url', 'origin', remote], target)
            # Preserve unfinished edits and untracked files after explicit retry.
            patch = run(['diff', 'HEAD', '--binary'], old)
            if patch:
                run(['apply', '--binary', '-'], target, data=(patch+'\n').encode())
            for path in names:
                source = old / path
                if source.exists() and not (target/path).exists():
                    (target/path).parent.mkdir(parents=True,exist_ok=True)
                    shutil.copy2(source,target/path)
        else:
            token = app.token(name)
            run(['clone', '--no-recurse-submodules', '--single-branch', '--branch', base, '--', remote, str(target)], token=token)
            base_head = run(['rev-parse', 'origin/' + base], target)
            previous = run(['ls-remote', '--heads', remote, 'refs/heads/' + branch(item['id'])], token=token)
            if previous:
                run(['fetch', 'origin', branch(item['id'])], target, token)
                run(['checkout', '-b', branch(item['id']), 'FETCH_HEAD'], target)
            else:
                run(['checkout', '-b', branch(item['id']), 'origin/' + base], target)
        run(['config', 'user.name', 'aof-agent[bot]'], target)
        run(['config', 'user.email', 'aof-agent[bot]@users.noreply.github.com'], target)
        baseline[name] = {'base':base,'base_head':base_head,'head':run(['rev-parse','HEAD'],target),
                          'config':hashlib.sha256((target/'.git/config').read_bytes()).hexdigest()}
    record = manifest(root)
    record.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    record.write_text(json.dumps(baseline))
    return root


def changes(directory):
    modified=run(['diff','--name-only','--no-renames','HEAD','-z'],directory).split('\0')
    untracked=run(['ls-files','--others','--exclude-standard','-z'],directory).split('\0')
    names=sorted({name for name in modified+untracked if name})
    tracked=set(run(['ls-files','-z'],directory).split('\0'))
    # Local working instructions are not product changes. Never publish newly
    # generated instructions; edits to tracked instructions still stop the run.
    names=[name for name in names if name in tracked or Path(name).name not in {'AGENTS.md','SKILL.md'}]
    if len(names) > 200:
        raise TaskError('More than 200 changed files; manual review required.')
    forbidden = {'.env', '.npmrc', '.netrc', 'auth.json', 'credentials.json', 'AGENTS.md', 'SKILL.md'}
    for name in names:
        parts = Path(name).parts
        if any(p in {'.git', '.github', '.codex', '.agents'} for p in parts) or Path(name).name in forbidden:
            raise TaskError('Diff changes protected configuration or instructions; publication stopped.')
        path = directory / name
        if not path.resolve().is_relative_to(directory.resolve()):
            raise TaskError('Changed path escapes its checkout; publication stopped.')
        if any(parent.is_symlink() for parent in path.parents if parent != directory and directory in parent.parents):
            raise TaskError('Changed path has a symlink parent; publication stopped.')
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise TaskError('Diff contains a symlink or unsupported file; manual review required.')
        if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
            raise TaskError('Changed file exceeds 2 MiB; manual review required.')
    run(['diff', '--check'], directory)
    return names
