"""One task at a time: claim, prepare, execute, validate, publish, report."""
import fcntl
import json
import os
from pathlib import Path
import threading
import time

from coding import client, git, prompt, publish, queue
from gateway import work
from repositories.github_app import AccessError, GitHubApp
from telegram.storage import connect
from worker.run import TaskError, TaskCancelled
from vpn.proxy import Server


def process(db, item, app):
    links = []
    try:
        root = git.prepare(app, item)
        if queue.cancelled(db, item):
            raise TaskCancelled('Cancelled before execution')
        client.request('/run', {'run_id': item['run_id'], 'task_id': item['id'], 'attempt': item['attempt'],
                               'repositories': json.loads(item['repositories']), 'prompt': prompt.build(item)})
        queue.checkpoint(db, item, 'Implementing and testing')
        deadline = time.monotonic() + 1860
        while True:
            queue.heartbeat(db)
            Path('/tmp/controller-ready').touch()
            if queue.cancelled(db, item) or time.monotonic() > deadline:
                client.request('/cancel', {'run_id': item['run_id']})
                raise TaskCancelled('Cancellation requested; checkout preserved.')
            status = client.request('/status')
            if status['run_id'] != item['run_id']:
                raise TaskError('Executor restarted; explicit retry required.')
            if status['state'] != 'running':
                break
            time.sleep(2)
        if status['state'] != 'completed':
            raise TaskError(status['output'] or 'Execution failed')
        queue.checkpoint(db, item, 'Reviewing diff and publishing task branch')
        def recorded(link):
            links.append(link)
            with db:
                current = json.loads(work.get(db, item['id'])['pull_requests'])
                db.execute('UPDATE work_items SET pull_requests=? WHERE id=?',
                           (json.dumps(list(dict.fromkeys([*current, *links]))), item['id']))
        _, result = publish.publish(app, item, root, status['output'],
                                    cancelled=lambda: queue.cancelled(db, item), recorded=recorded)
        queue.finish(db, item, 'ready_for_review', result, links)
    except TaskCancelled as error:
        queue.finish(db, item, 'cancelled', str(error), links)
    except (TaskError, AccessError) as error:
        queue.finish(db, item, 'failed', str(error), links)
    except (OSError, ValueError):
        queue.finish(db, item, 'failed', 'Controller failed; workspace preserved; private details omitted.', links)


def main():
    os.umask(0o077)
    with open('/workspace/.coding-controller.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = connect('/unused')
        queue.recover(db)
        app = GitHubApp()
        app.verify_installation()
        proxy = Server(('0.0.0.0', 8888))
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        print('Coding controller ready; task branches only; PRs disabled', flush=True)
        while True:
            queue.heartbeat(db)
            Path('/tmp/controller-ready').touch()
            status = client.request('/status')
            if status['state'] == 'running':
                # Cancel an orphan after controller restart; never claim another task yet.
                client.request('/cancel', {'run_id': status['run_id']})
            else:
                item = queue.claim(db)
                if item:
                    process(db, item, app)
            time.sleep(2)


if __name__ == '__main__':
    main()
