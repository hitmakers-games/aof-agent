"""Versioned shared instructions, cached locally so chat never waits on GitHub."""
import base64
import json
from pathlib import Path
import threading
import time
import os

from repositories.github_app import GitHubApp, AccessError

FILES = ('identity.md', 'requirements.md', 'memory.md')
CACHE = Path('/transport/agent-context.json')
_lock = threading.Lock()


def context():
    try:
        return json.loads(CACHE.read_text())
    except (OSError, ValueError):
        root = Path(__file__).resolve().parent.parent / 'agent'
        return {name: (root/name).read_text() for name in FILES}


def refresh():
    app = GitHubApp()
    token = app.token('aof-agent')
    files = {}
    for name in FILES:
        value = app.request('/repos/hitmakers-games/aof-agent/contents/agent/'+name+'?ref=master', token)
        files[name] = base64.b64decode(value['content']).decode()
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


def remember(text):
    """Read latest master before a conditional update; concurrent edits are preserved."""
    from coding.publish import SECRET
    if not text.strip() or len(text)>4000 or SECRET.search(text.encode()):
        raise ValueError('Supply a short nonsecret project fact.')
    app = GitHubApp()
    token = app.token('aof-agent', {'contents':'write'})
    path = '/repos/hitmakers-games/aof-agent/contents/agent/memory.md'
    latest = app.request(path+'?ref=master', token)
    current = base64.b64decode(latest['content']).decode()
    if text.strip() in current:
        return {'status':'already remembered'}
    content = current.rstrip()+'\n\n- '+text.strip()+'\n'
    result = app.request(path, token, {'message':'Update shared project memory', 'branch':'master',
        'sha':latest['sha'], 'content':base64.b64encode(content.encode()).decode()}, method='PUT')
    refresh()
    return {'commit':result['commit']['html_url']}
