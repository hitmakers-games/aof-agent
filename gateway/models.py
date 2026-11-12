"""Telegram model selection; choices are saved separately from untrusted prompts."""
import json
import os
from pathlib import Path

from gateway import access
from telegram.storage import put, setting


PRESETS = {
    'fast': {'model': 'gpt-6-luna', 'effort': 'low'},
    'balanced': {'model': 'gpt-6.1-sol', 'effort': 'low'},
    'smart': {'model': 'gpt-6-astra', 'effort': 'high'},
}


def publish_catalog():
    source = Path(os.environ.get('CODEX_HOME', '/state/auth')) / 'models_cache.json'
    if not source.exists():
        return
    path = Path('/transport/models.json')
    if path.exists() and path.stat().st_mtime >= source.stat().st_mtime:
        return
    models = []
    for item in json.loads(source.read_text()).get('models', []):
        if item.get('visibility') == 'list':
            models.append({'model': item['slug'], 'default': item['default_reasoning_level'],
                           'efforts': [level['effort'] for level in item['supported_reasoning_levels']]})
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(models))
    temporary.replace(path)


def catalog():
    path = Path('/transport/models.json')
    if path.exists():
        return json.loads(path.read_text())
    source = Path(os.environ.get('CODEX_HOME', '/state/auth')) / 'models_cache.json'
    if source.exists():
        return [{'model': item['slug'], 'default': item['default_reasoning_level'],
                 'efforts': [level['effort'] for level in item['supported_reasoning_levels']]}
                for item in json.loads(source.read_text()).get('models', []) if item.get('visibility')=='list']
    return [{'model': value['model'], 'default': value['effort'],
             'efforts': ['low', 'medium', 'high', 'xhigh', 'max'] + ([] if 'luna' in value['model'] else ['ultra'])}
            for value in PRESETS.values()]


def selected(db, chat_id):
    return json.loads(setting(db, f'model:{chat_id}', json.dumps(PRESETS['balanced'])))


def validate(value):
    model = next((item for item in catalog() if item['model'] == value.get('model')), None)
    if not model or value.get('effort') not in model['efforts']:
        raise ValueError('Unsupported model/reasoning combination. Use /models.')
    return {'model': value['model'], 'effort': value['effort']}


def command(db, actor, chat_id, head, argument):
    current = selected(db, chat_id)
    if head == '/models':
        return '\n'.join(item['model'] + ': ' + ', '.join(item['efforts']) for item in catalog())
    if argument:
        access.require(db, actor, 'operator')
        if head == '/mode':
            if argument not in PRESETS:
                raise ValueError('Use /mode fast|balanced|smart.')
            current = dict(PRESETS[argument])
        elif head == '/model':
            parts = argument.split()
            if len(parts) > 2:
                raise ValueError('Use /model model-name [reasoning].')
            model = next((item for item in catalog() if item['model'] == parts[0]), None)
            if not model:
                raise ValueError('Choose a model from /models.')
            current = {'model': parts[0], 'effort': parts[1] if len(parts) == 2 else model['default']}
        else:
            current['effort'] = argument
        current = validate(current)
        put(db, f'model:{chat_id}', json.dumps(current))
        access.audit(db, actor, 'model.select', chat_id, current)
    return f"Model: {current['model']}\nReasoning: {current['effort']}\n/mode fast|balanced|smart; /model name [reasoning]; /reasoning level\n/fast message or /smart message changes only that request."


def snapshot(db, request_id, chat_id, override=None):
    put(db, f'request-model:{request_id}', json.dumps(validate(override or selected(db, chat_id))))


def for_request(db, request_id, chat_id):
    return json.loads(setting(db, f'request-model:{request_id}', json.dumps(selected(db, chat_id))))
