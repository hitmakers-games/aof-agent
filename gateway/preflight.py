"""Check a new image against the current database without changing any data."""
import time
from gateway import work, tasks
from telegram.storage import connect, setting


def check(db):
    # Schema-dependent reads must pass before replacing the working bot.
    # Do not print task contents, credentials, or run schema migrations here.
    work.listing(db, int(setting(db, 'owner')))
    for worker in ('model','operations'):
        db.execute(tasks.selection(worker),(time.time(),)).fetchone()
    for table in ('inbox', 'outbox', 'coding_runs', 'telegram_messages'):
        db.execute('SELECT 1 FROM '+table+' LIMIT 0')


def main():
    # Import the actual service entrypoints too, catching missing packaged modules.
    import telegram.bot, worker.chat, worker.background, developer.server, gateway.coordinator
    db = connect('/unused')
    try:
        db.execute('BEGIN READ ONLY')
        check(db)
        print('Gateway database compatibility check passed.', flush=True)
    finally:
        db.rollback()
        db.close()


if __name__=='__main__':
    try:
        main()
    except Exception as error:
        raise SystemExit('Gateway preflight failed ('+type(error).__name__+'); deployment stopped.') from None
