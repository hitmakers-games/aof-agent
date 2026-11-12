"""Small stdio client for Codex turns, including live follow-up messages."""
import json
import queue
import subprocess
import threading
import time

from gateway.redaction import redact
from worker.run import TaskError, TaskCancelled, terminate, MAX_EVENT_BYTES, MAX_EVENTS_BYTES


class RequestRejected(TaskError):
    pass


class Client:
    def __init__(self, command, environment):
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=environment, start_new_session=True)
        self.incoming = queue.Queue(maxsize=256)
        self.notifications = []
        self.sequence = 0
        self.bytes_read = 0
        self.closed = threading.Event()
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def offer(self, value):
        while not self.closed.is_set():
            try:
                self.incoming.put(value,timeout=0.2)
                return
            except queue.Full:
                pass

    def read(self):
        try:
            while not self.closed.is_set():
                line = self.process.stdout.readline(MAX_EVENT_BYTES+1)
                if not line:
                    break
                self.bytes_read += len(line)
                if len(line)>MAX_EVENT_BYTES or self.bytes_read>MAX_EVENTS_BYTES:
                    raise TaskError('Codex exceeded the event limit')
                self.offer(json.loads(line))
        except Exception:
            self.offer({'error': {'message': 'Codex event stream failed'}})
        finally:
            self.offer(None)

    def send(self, message):
        self.process.stdin.write((json.dumps(message)+'\n').encode())
        self.process.stdin.flush()

    def receive(self, timeout=0.2):
        try:
            value = self.incoming.get(timeout=timeout)
        except queue.Empty:
            return {}
        if value is None:
            raise TaskError('Codex stopped before completing the turn')
        # Approval is governed by the configured never policy. Do not hang on
        # unexpected interactive requests or grant additional permissions here.
        if 'method' in value and 'id' in value:
            self.send({'id': value['id'], 'error': {'code': -32601,
                'message': 'Interactive requests are unavailable; ask the user in your reply.'}})
            return {}
        return value

    def request(self, method, params, timeout=60):
        self.sequence += 1
        request_id = self.sequence
        self.send({'id': request_id, 'method': method, 'params': params})
        deadline = time.monotonic()+timeout
        while time.monotonic()<deadline:
            value = self.receive()
            if value.get('id') == request_id:
                if 'error' in value:
                    raise RequestRejected(redact(value['error'].get('message','Codex request failed'))[:1000])
                return value.get('result', {})
            if value:
                self.notifications.append(value)
        raise TaskError('Codex '+method+' timed out')

    def close(self):
        self.closed.set()
        terminate(self.process)
        self.reader.join(timeout=1)
        self.process.stdin.close()
        self.process.stdout.close()


def run(command, environment, root, model, effort, prompt, timeout=3600,
        cancelled=None, followups=None, accepted=None, released=None, progress=None, output_schema=None, interim=None):
    client = Client(command, environment)
    try:
        client.request('initialize', {'clientInfo': {'name': 'aof_agent', 'title': 'AOF Agent', 'version': '1'}})
        client.send({'method': 'initialized', 'params': {}})
        thread = client.request('thread/start', {'cwd': str(root), 'model': model,
            'approvalPolicy': 'never', 'ephemeral': True})['thread']['id']
        parameters = {'threadId': thread, 'input': [{'type': 'text', 'text': prompt}], 'effort': effort}
        if output_schema:
            parameters['outputSchema'] = output_schema
        turn = client.request('turn/start', parameters)['turn']['id']
        deadline = time.monotonic()+timeout
        response = ''
        interim_budget = 0
        while time.monotonic()<deadline:
            if cancelled and cancelled():
                client.request('turn/interrupt', {'threadId': thread, 'turnId': turn}, timeout=10)
                stopped_by=time.monotonic()+10
                while time.monotonic()<stopped_by:
                    value=client.notifications.pop(0) if client.notifications else client.receive()
                    if value.get('method')=='turn/completed':break
                raise TaskCancelled('Stopped; existing files were preserved')
            if followups:
                messages = followups()
                if messages:
                    text = '\n\n'.join(message['text'] for message in messages)
                    try:
                        client.request('turn/steer', {'threadId': thread, 'expectedTurnId': turn,
                            'input': [{'type': 'text', 'text': text}]}, timeout=30)
                    except RequestRejected:
                        if released: released(messages)
                    else:
                        if accepted: accepted(messages)
                        interim_budget = 1
            value = client.notifications.pop(0) if client.notifications else client.receive()
            method, params = value.get('method'), value.get('params', {})
            if method == 'item/completed':
                item = params.get('item', {})
                if item.get('type') == 'agentMessage':
                    response = item.get('text', response)
                    if item.get('phase')=='commentary' and interim and interim_budget:
                        interim(response)
                        interim_budget -= 1
                elif progress and item.get('type') in {'commandExecution', 'fileChange', 'mcpToolCall'}:
                    progress(item)
            elif method == 'turn/completed':
                result = params['turn']
                if result['status'] != 'completed':
                    raise TaskError('Codex turn ended with '+result['status'])
                if not response.strip():
                    raise TaskError('Codex returned no final reply')
                return response
            elif method == 'error' and not params.get('willRetry', False):
                error=params.get('error',{})
                raise TaskError(redact(error.get('message',params.get('message','Codex failed')))[:1000])
        raise TaskError('Codex turn timed out; workspace preserved')
    finally:
        client.close()
