"""Shared developer model selection, with immutable per-request choices."""
import json

from gateway import access
from telegram.storage import put, setting


DEFAULT = {'model': 'gpt-6.1-sol', 'effort': 'medium'}
def selected(db, chat_id):
    # Carry forward the owner's preference when upgrading from per-chat settings.
    # A group's old override must never become the shared developer preference.
    legacy = setting(db, f"model:{setting(db, 'owner')}", json.dumps(DEFAULT))
    return json.loads(setting(db, 'developer:model', legacy))


def initialize(db):
    db.execute("INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO NOTHING",
               ('developer:model', json.dumps(selected(db, None))))


def validate(value):
    for key in ('model', 'effort'):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f'{key} must be a nonempty string.')
    return {'model': value['model'].strip(), 'effort': value['effort'].strip()}


def configure(db, actor, chat_id, action, argument):
    access.require(db, actor, 'admin')
    if action not in {'model', 'reasoning'}:
        raise ValueError('Unknown model setting.')
    current = selected(db, chat_id)
    if argument:
        if action == 'model':
            parts = argument.split()
            if len(parts) > 2:
                raise ValueError('Provide model name and optional reasoning level.')
            current = {'model': parts[0], 'effort': parts[1] if len(parts) == 2 else current['effort']}
        else:
            current['effort'] = argument
        current = validate(current)
        put(db, 'developer:model', json.dumps(current))
        access.audit(db, actor, 'model.select', 'developer', current)
    return f"Model: {current['model']}\nReasoning: {current['effort']}"


def snapshot(db, request_id, chat_id, override=None):
    put(db, f'request-model:{request_id}', json.dumps(validate(override or selected(db, chat_id))))


def for_request(db, request_id, chat_id):
    return json.loads(setting(db, f'request-model:{request_id}', json.dumps(selected(db, chat_id))))
