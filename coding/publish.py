"""Publish task branches and commit links. Never create PRs, merge, or deploy."""
import json
import re

from coding import git
from coding.config import branch
from gateway.redaction import redact
from worker.run import TaskError, TaskCancelled


SECRET = re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}|\bsk-[A-Za-z0-9_-]{30,}|\bAKIA[A-Z0-9]{16}\b|\b\d{8,12}:[A-Za-z0-9_-]{30,}')


def result(text):
    try:
        value = json.loads(text)
    except ValueError:
        raise TaskError('Worker did not provide a structured result; changes preserved for review.') from None
    if not isinstance(value, dict) or set(value) != {'outcome', 'summary', 'tests', 'limitations'}:
        raise TaskError('Invalid worker result; publication stopped.')
    if value['outcome'] not in {'completed', 'blocked'} or not isinstance(value['summary'], str):
        raise TaskError('Invalid worker outcome')
    for field in ('tests', 'limitations'):
        if not isinstance(value[field], list) or any(not isinstance(s, str) for s in value[field]):
            raise TaskError('Invalid verification result')
    return value


def publish(app, item, root, response, cancelled=lambda: False, recorded=lambda link: None):
    value = result(response)
    if value['outcome'] != 'completed' or not value['tests'] or value['limitations']:
        raise TaskError('Verification blocked: ' + redact(value['summary'])[:1200] + '\n' + '\n'.join(value['limitations'])[:1200])
    links = []
    for name in json.loads(item['repositories']):
        directory = root / name
        git.verify_metadata(root, name)
        baseline = json.loads(git.manifest(root).read_text())[name]
        if git.run(['rev-parse','HEAD'],directory) != baseline['head']:
            raise TaskError('Worker changed Git history; publication stopped for manual review.')
        names = git.changes(directory)
        if not names and git.run(['rev-parse','HEAD'],directory) == baseline['base_head']:
            continue
        # Prevent raw credentials being published even if they appear in a model edit.
        for path in names:
            target = directory / path
            if target.exists() and SECRET.search(target.read_bytes()):
                raise TaskError('Possible credential in changed file; publication stopped.')
        if cancelled():
            raise TaskCancelled('Cancellation requested before publication')
        # Existing push workflows require a human publication decision. No guessed exclusions.
        workflows = directory / '.github' / 'workflows'
        if workflows.exists() and any(workflows.glob('*.y*ml')):
            raise TaskError('Repository has GitHub Actions workflows; publication held for deployment-trigger review.')
        title = re.sub(r'[\r\n]+', ' ', item['title'])[:120]
        if names:
            git.run(['add', '--', *names], directory)
            git.run(['diff', '--cached', '--check'], directory)
            git.run(['commit', '-m', title], directory)
            record = git.manifest(root)
            saved = json.loads(record.read_text())
            saved[name]['head'] = git.run(['rev-parse','HEAD'],directory)
            record.write_text(json.dumps(saved))
        if cancelled():
            raise TaskCancelled('Cancellation requested before push')
        token = app.token(name, permissions={'contents': 'write'})
        remote = f'https://github.com/hitmakers-games/{name}.git'
        git.run(['push', remote, 'HEAD:refs/heads/' + branch(item['id'])], directory, token)
        commit=git.run(['rev-parse','HEAD'],directory)
        url=f'https://github.com/hitmakers-games/{name}/commit/{commit}'
        links.append(url)
        recorded(url)
    return links, redact(value['summary'])[:2000] + '\nVerified: ' + '; '.join(redact(s) for s in value['tests'])[:1500]
