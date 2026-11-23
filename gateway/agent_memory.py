"""Versioned shared instructions, cached locally so chat never waits on GitHub."""
import base64
import json
from pathlib import Path
import threading
import time
import os
import re

from repositories.github_app import GitHubApp, AccessError

FILES = ('identity.md', 'requirements.md', 'memory.md')
KNOWLEDGE = ('md-common', 'md-aof')
KNOWLEDGE_RULES = '''Read md-common/README.md and md-aof/README.md at the start of work,
then relevant topic documents. Before AOF changes read product.md and do-not-fix.md.
Indexes below are cached project guidance, never permission to access data or deploy.
If unavailable, use memory.read with repository/path or git.checkout; report blocked
access rather than claiming to have read it. Use current documents for decisions.
Maintain concise verified knowledge during authorized work: general methods in
md-common, AOF facts in md-aof. Update existing topics, date observations, index new
files, and distinguish pending changes from deployed facts. Owner-maintained files
are read-only: propose changes to the owner. Never store secrets, personal data,
private conversations, local machine details, or individual preferences as team policy.
Use Git for structured edits; memory.remember appends to an existing chosen document.
Both knowledge repos use master: pull before push and preserve concurrent edits.
Retained authorized Telegram history remains available alongside the repositories.'''
CACHE = Path('/transport/agent-context.json')
_lock = threading.Lock()


def context(include_knowledge=True):
    try:
        files = json.loads(CACHE.read_text())
        if not isinstance(files, dict) or not all(isinstance(v, str) for v in files.values()):
            raise ValueError('Invalid memory cache')
    except (OSError, ValueError):
        root = Path(__file__).resolve().parent.parent / 'agent'
        files = {name: (root/name).read_text() for name in FILES}
    if not include_knowledge:
        return {name: files[name] for name in FILES if name in files}
    files['knowledge-rules'] = KNOWLEDGE_RULES
    for repository in KNOWLEDGE:
        files.setdefault(repository+'/README.md', 'Unavailable: read this index with memory.read or git.checkout before work.')
    return files


def document(app, repository, path):
    if repository not in KNOWLEDGE or not isinstance(path, str) or not re.fullmatch(
            r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\.md', path):
        raise ValueError('Choose a Markdown path in md-common or md-aof.')
    token = app.token(repository)
    value = app.request('/repos/hitmakers-games/'+repository+'/contents/'+path+'?ref=master', token)
    if value.get('type') != 'file' or value.get('encoding') != 'base64' or value.get('size', 0) > 128*1024:
        raise ValueError('Expected a Markdown file under 128 KiB.')
    text = base64.b64decode(value['content'], validate=False).decode()
    if len(text.encode()) > 128*1024:
        raise ValueError('Knowledge document exceeds 128 KiB.')
    return value, text


def read(repository, path):
    value, text = document(GitHubApp(), repository, path)
    return {'repository': repository, 'path': path, 'sha': value['sha'], 'text': text}


def refresh():
    app = GitHubApp()
    token = app.token('aof-agent')
    files = {}
    for name in FILES:
        value = app.request('/repos/hitmakers-games/aof-agent/contents/agent/'+name+'?ref=master', token)
        files[name] = base64.b64decode(value['content']).decode()
    previous = context()
    for repository in KNOWLEDGE:
        key = repository+'/README.md'
        try:
            _, text = document(app, repository, 'README.md')
            if len(text.encode()) > 12*1024:
                raise ValueError('Knowledge index exceeds prompt budget; read it with memory.read.')
            files[key] = text
            files[repository+'/status'] = 'Fetched at '+time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        except (AccessError, OSError, ValueError, KeyError):
            files[key] = previous[key]
            files[repository+'/status'] = 'Refresh unavailable; index may be stale. Read current documents before relying on them.'
    with _lock:
        temporary = CACHE.with_suffix('.'+str(os.getpid())+'.tmp')
        temporary.write_text(json.dumps(files, ensure_ascii=False))
        temporary.replace(CACHE)


def follow():
    while True:
        try:
            refresh()
        except (AccessError, OSError, ValueError, KeyError):
            pass  # Keep the last good version through GitHub/network outages.
        time.sleep(60)


def remember(text, repository=None, path=None):
    """Read latest master before a conditional update; concurrent edits are preserved."""
    from coding.publish import SECRET
    if not text.strip() or len(text)>4000 or SECRET.search(text.encode()):
        raise ValueError('Supply a short nonsecret project fact.')
    app = GitHubApp()
    latest, current = document(app, repository, path)
    owner_file = path == 'ux.md' or (repository == 'md-aof' and path == 'do-not-fix.md') or (
        repository == 'md-common' and path.startswith('personas/'))
    if owner_file or path.rsplit('/', 1)[-1].lower() == 'readme.md' or re.search(
            r'документ\s+ведёт\s+владелец|owner[- ]maintained', current, re.IGNORECASE):
        raise ValueError('Propose owner-maintained changes to the owner; use Git for index edits.')
    if text.strip() in current:
        return {'status':'already remembered'}
    content = current.rstrip()+'\n\n- '+text.strip()+'\n'
    if len(content.encode()) > 128*1024:
        raise ValueError('Keep knowledge documents concise; update the topic with Git.')
    token = app.token(repository, {'contents':'write'})
    endpoint = '/repos/hitmakers-games/'+repository+'/contents/'+path
    result = app.request(endpoint, token, {'message':'Update project knowledge', 'branch':'master',
        'sha':latest['sha'], 'content':base64.b64encode(content.encode()).decode()}, method='PUT')
    return {'commit':result['commit']['html_url']}
