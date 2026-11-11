"""Separate read-only diagnostic worker; this container has no model credentials."""
import fcntl
import os
from pathlib import Path
import sqlite3
import time

from gateway import tasks
from gateway.diagnostics import process_one
from gateway.kubernetes import Kubernetes
from telegram.storage import connect, put
from database.errors import StorageError


def main():
    os.umask(0o077)
    with open("/transport/observer.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = connect("/transport/telegram.sqlite3")
        tasks.recover(db, "operations")
        client = Kubernetes()
        print("Read-only stand observer ready", flush=True)
        while True:
            with db:
                put(db, "operations_heartbeat", time.time())
            Path("/tmp/observer-ready").touch()
            if not process_one(db, client):
                time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except (OSError, sqlite3.Error, StorageError):
        raise SystemExit("Observer stopped; check configuration") from None
