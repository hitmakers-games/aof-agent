"""Lossless, transactional import from the stopped gateway's original SQLite file."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

from database.errors import StorageError
from database.postgres import Connection, initialize


TABLES = {"settings": "key", "inbox": "update_id", "outbox": "id"}


def rows(source):
    snapshot = {}
    for table, order in TABLES.items():
        cursor = source.execute(f'SELECT * FROM "{table}" ORDER BY "{order}"')
        columns = [item[0] for item in cursor.description]
        snapshot[table] = (columns, cursor.fetchall())
    return snapshot


def compare(db, snapshot):
    for table, (columns, expected) in snapshot.items():
        names = ",".join('"' + name + '"' for name in columns)
        actual = db.execute(f'SELECT {names} FROM "{table}" ORDER BY "{TABLES[table]}"').fetchall()
        if actual != expected:
            raise StorageError("Migration verification failed; source and destination differ.")


def transfer(source, db):
    initialize(db)
    source.execute("BEGIN")
    try:
        snapshot = rows(source)
        with db:
            db.execute("LOCK TABLE settings,inbox,outbox IN EXCLUSIVE MODE")
            existing = any(db.execute(f'SELECT EXISTS(SELECT 1 FROM "{table}")').fetchone()[0] for table in TABLES)
            if not existing:
                for table, (columns, records) in snapshot.items():
                    # Column names come only from the legacy database schema.
                    expected_columns = {item[0] for item in db.execute(f'SELECT * FROM "{table}" LIMIT 0').description}
                    if not set(columns).issubset(expected_columns):
                        raise StorageError("Unexpected legacy schema; migration stopped without replacing data.")
                    names = ",".join('"' + name + '"' for name in columns)
                    placeholders = ",".join("?" for _ in columns)
                    for record in records:
                        db.execute(f'INSERT INTO "{table}" ({names}) VALUES ({placeholders})', record)
                db.execute("SELECT setval(pg_get_serial_sequence('outbox','id'), COALESCE(max(id),1), count(*)>0) FROM outbox")
            compare(db, snapshot)
        return {table: len(records) for table, (_, records) in snapshot.items()}
    finally:
        source.rollback()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    options = parser.parse_args()
    source = sqlite3.connect(options.source.resolve().as_uri() + "?mode=ro", uri=True)
    db = Connection(Path(os.environ["AOF_DATABASE_DSN_FILE"]).read_text().strip(),
                    os.environ["AOF_DATABASE_CA_FILE"], os.environ.get("AOF_DATABASE_NAME"))
    try:
        identity = db.execute("SELECT current_database(),current_user").fetchone()
        if identity != ("aof_agent", "aof_agent"):
            raise StorageError("Unexpected migration target")
        counts = transfer(source, db)
        print(json.dumps({"exact_row_comparison": "passed", "counts": counts,
                          "paired": db.execute("SELECT EXISTS(SELECT 1 FROM settings WHERE key='owner')").fetchone()[0]}))
    finally:
        db.close()
        source.close()


if __name__ == "__main__":
    try:
        main()
    except (StorageError, OSError, sqlite3.Error, ValueError):
        print("Migration failed; original SQLite state was not modified and credentials were omitted.", file=sys.stderr)
        sys.exit(1)
