"""Authenticated, bounded controller-to-executor requests inside the namespace."""
import json
import os
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

from worker.run import TaskError


def request(path, value=None, timeout=10):
    endpoint = os.environ.get('AOF_EXECUTOR_URL', 'http://aof-agent-coding-executor:8080')
    token = Path('/run/secrets/executor/token').read_text().strip()
    req = Request(endpoint + path, data=None if value is None else json.dumps(value).encode(),
                  headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        with build_opener(ProxyHandler({})).open(req, timeout=timeout) as response:
            data = response.read(256 * 1024 + 1)
        if len(data) > 256 * 1024:
            raise ValueError('Response too large')
        return json.loads(data)
    except (OSError, ValueError):
        raise TaskError('Coding executor is unavailable; no execution was replayed.') from None
