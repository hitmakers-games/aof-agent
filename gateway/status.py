"""Fast status from observed heartbeats and Jira requests; no network calls."""
import time

from gateway import access
from telegram.storage import setting


def elapsed(value, now):
    try:
        return max(0, int(now - float(value))) if value else None
    except (TypeError, ValueError):
        return None


def heartbeat(db, name, limit, now):
    age = elapsed(setting(db, name + '_heartbeat'), now)
    if age is None:
        return 'unknown (no heartbeat)'
    return f"{'online' if age < limit else 'offline'} (heartbeat {age}s ago)"


def counts(db, table, column, states):
    rows = dict(db.execute(f'SELECT {column},count(*) FROM {table} GROUP BY {column}').fetchall())
    return '; '.join(f'{state}: {rows.get(state, 0)}' for state in states)


def describe(db, actor):
    now = time.time()
    configured = setting(db, 'jira_configured')
    jira = {'1': 'configured for developer tools (read/write)',
            '0': 'credentials not mounted'}.get(configured, 'configuration not yet observed')
    age = elapsed(setting(db, 'jira_last_request'), now)
    observation = ('no API request recorded' if age is None else
                   f"last API request {setting(db, 'jira_last_outcome', 'unknown')}, {age}s ago")
    coding = heartbeat(db, 'coding', 180, now)
    if setting(db, 'coding_enabled', '0') != '1':
        coding = 'disabled for new assignments; ' + coding
    lines = [
        'Developer: ' + heartbeat(db, 'engine', 180, now),
        'Stand diagnostics: ' + heartbeat(db, 'operations', 30, now),
        'Task coordinator: ' + heartbeat(db, 'coordinator', 30, now),
        f'Jira: {jira}; {observation}.',
        'Jira assignment polling: off; assignment alone does not start work.',
        'Coding worker: ' + coding,
        f"Auto-start assignments: {setting(db, 'autostart', 'off')}; new coding starts paused: {setting(db, 'paused', '0') == '1'}",
        'Chat/diagnostic jobs: ' + counts(db, 'inbox', 'status', ('queued', 'running', 'cancelling')),
        'Coding assignments: ' + counts(db, 'work_items', 'state', ('awaiting_approval', 'queued', 'running', 'cancelling')),
    ]
    if access.allowed(db, actor, 'operator'):
        own = db.execute("""SELECT update_id,status FROM inbox
                            WHERE (chat_id=? OR (chat_id IS NULL AND ?=?))
                            AND status IN ('queued','running','cancelling')
                            ORDER BY created_at LIMIT 5""",
                         (actor, str(actor), setting(db, 'owner'))).fetchall()
        lines.extend(f'#{uid}: {state}' for uid, state in own)
    lines.append('Use /work for assignments; /status <id> for task details.')
    return '\n'.join(lines)
