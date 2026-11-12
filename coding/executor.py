"""Credential-separated coding process; no GitHub, Telegram, DB, or Kube access."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import threading

from coding.config import task_directory, repositories
from coding.sandbox import options
from worker.run import TaskError, child_environment, command, run_codex


class Execution:
    def __init__(self):
        self.lock = threading.Lock()
        self.cancel = threading.Event()
        self.run_id = None
        self.state = 'idle'
        self.output = None

    def start(self, value):
        if set(value) != {'run_id', 'task_id', 'attempt', 'repositories', 'prompt'}:
            raise ValueError('Invalid request')
        root = task_directory(value['task_id'], value['attempt'])
        names = repositories(value['repositories'])
        if len(value['run_id']) != 32 or not isinstance(value['prompt'], str) or len(value['prompt'].encode()) > 128*1024:
            raise ValueError('Invalid request')
        for name in names:
            path = root / name
            if path.is_symlink() or not path.is_dir() or root.resolve() not in path.resolve().parents:
                raise ValueError('Invalid checkout')
        with self.lock:
            if self.run_id == value['run_id']:
                return
            if self.state == 'running':
                raise ValueError('Executor busy')
            self.run_id, self.state, self.output = value['run_id'], 'running', None
            self.cancel.clear()
            threading.Thread(target=self.run, args=(root, value), daemon=True).start()

    def run(self, root, value):
        try:
            args = command(root, 'workspace-write')
            index=args.index('--sandbox')
            del args[index:index+2]
            args[-1:-1] = ['--skip-git-repo-check', '--output-schema', '/opt/aof-agent/coding/result.schema.json']
            shell_env = {'HTTPS_PROXY': 'http://aof-agent-coding-controller:8888',
                         'HTTP_PROXY': 'http://aof-agent-coding-controller:8888',
                         'NO_PROXY': 'localhost,127.0.0.1', 'NODE_OPTIONS': '--max-old-space-size=1536',
                         'MAVEN_OPTS': '-Xmx1024m -Dmaven.repo.local='+str(root/'.m2'), 'MAKEFLAGS': '-j1',
                         'npm_config_cache':str(root/'.npm')}
            shell_toml = '{' + ','.join(key + '=' + json.dumps(value) for key,value in shell_env.items()) + '}'
            args[-1:-1] = [*options(root,value['repositories'],network=True),
                           '-c', 'shell_environment_policy.set=' + shell_toml,
                           '-c', 'web_search="disabled"', '-c', 'features.view_image=false']
            environment = child_environment(os.environ)
            output = run_codex(args, value['prompt'], 1800, environment, cancelled=self.cancel.is_set, include_commands=True)
            from coding.publish import result
            if result(output['response'])['outcome'] == 'completed':
                from coding.verify import check
                check(root, value['repositories'], output['commands'])
            self.output, self.state = output['response'], 'completed'
        except TaskError as error:
            self.output, self.state = str(error), 'cancelled' if self.cancel.is_set() else 'failed'
        except (OSError, ValueError):
            self.output, self.state = 'Executor failed; private details omitted.', 'failed'


execution = Execution()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def handle_request(self):
        expected = Path('/run/secrets/executor/token').read_text().strip()
        if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + expected):
            self.send_error(403)
            return
        try:
            if self.command == 'POST':
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 160 * 1024:
                    raise ValueError('Invalid request size')
                value = json.loads(self.rfile.read(size))
                if self.path == '/run':
                    execution.start(value)
                elif self.path == '/cancel' and value.get('run_id') == execution.run_id:
                    execution.cancel.set()
                else:
                    raise ValueError('Invalid action')
            elif self.path != '/status':
                raise ValueError('Invalid action')
            body = json.dumps({'run_id': execution.run_id, 'state': execution.state, 'output': execution.output}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ValueError, KeyError, TypeError):
            self.send_error(400)

    do_GET = do_POST = handle_request


def main():
    os.umask(0o077)
    # This preflight checks actual filesystem enforcement before accepting tasks.
    from worker.check_sandbox import check
    check()
    from coding.sandbox import preflight
    preflight()
    Path('/tmp/executor-ready').touch()
    print('Coding executor ready; sandbox verified', flush=True)
    ThreadingHTTPServer(('0.0.0.0', 8080), Handler).serve_forever()


if __name__ == '__main__':
    main()
