"""Persist task notifications independently of the model; Jira/coding are deferred."""
import fcntl
import os
from pathlib import Path
import sqlite3
import time

from database.errors import StorageError
from gateway import access, notices
from telegram.storage import connect, put


def main():
    os.umask(0o077)
    Path('/tmp/coordinator-ready').unlink(missing_ok=True)
    with open('/transport/coordinator.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        db=connect('/transport/telegram.sqlite3')
        with db:
            access.ensure_owner(db)
            put(db,'jira_state','not configured (deferred)')
        print('Task coordinator ready; coding execution disabled',flush=True)
        while True:
            now=time.time()
            notices.flush(db,now)
            with db:
                put(db,'coordinator_heartbeat',now)
            Path('/tmp/coordinator-ready').touch()
            time.sleep(2)


if __name__=='__main__':
    try:main()
    except (StorageError,OSError,sqlite3.Error,ValueError):
        raise SystemExit('Coordinator stopped; check configuration; private details omitted.') from None
