"""Durable gateway state. PostgreSQL in the cluster; SQLite for legacy import/tests."""
import json
import os
from pathlib import Path
import sqlite3

from database.errors import StorageError


def connect(path):
    if "AOF_DATABASE_DSN_FILE" in os.environ:
        from database.postgres import Connection, StorageError as PostgresError
        try:
            db = Connection(Path(os.environ["AOF_DATABASE_DSN_FILE"]).read_text().strip(),
                            os.environ["AOF_DATABASE_CA_FILE"], os.environ.get("AOF_DATABASE_NAME"))
            if db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] not in (2, 3, 4):
                db.close()
                raise StorageError("Unsupported database schema")
            return db
        except PostgresError:
            raise StorageError("PostgreSQL is unavailable or not initialized") from None
    db = sqlite3.connect(path, timeout=5)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS inbox (
            update_id INTEGER PRIMARY KEY, text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'awaiting_model',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL,
            delivered INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # Both containers can start together; serialize the additive migration.
    db.execute("BEGIN IMMEDIATE")
    try:
        columns = {row[1] for row in db.execute("PRAGMA table_info(inbox)")}
        additions = {
            "response": "TEXT", "kind": "TEXT NOT NULL DEFAULT 'message'",
            "target": "TEXT", "component": "TEXT", "window": "INTEGER DEFAULT 15",
            "context": "TEXT", "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
            "started_at": "TEXT", "finished_at": "TEXT", "checkpoint": "TEXT",
            "chat_id": "BIGINT", "requested_by": "BIGINT",
        }
        for name, definition in additions.items():
            if name not in columns:
                db.execute(f"ALTER TABLE inbox ADD COLUMN {name} {definition}")
        db.execute("CREATE INDEX IF NOT EXISTS inbox_pending ON inbox(status, kind, created_at)")
        columns = {row[1] for row in db.execute("PRAGMA table_info(outbox)")}
        for name, definition in {"chat_id": "BIGINT", "required_role": "TEXT NOT NULL DEFAULT 'operator'",
                "event_key": "TEXT", "reply_markup": "TEXT", "message_key": "TEXT", "delivered_message_id": "BIGINT", "authorized_by": "BIGINT"}.items():
            if name not in columns:
                db.execute(f"ALTER TABLE outbox ADD COLUMN {name} {definition}")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS outbox_event ON outbox(event_key)")
        for statement in (Path(__file__).parent.parent / "database/gateway.sql").read_text().split(";"):
            if statement.strip():
                db.execute(statement)
        columns = {row[1] for row in db.execute('PRAGMA table_info(work_items)')}
        for name, definition in {'repositories': "TEXT NOT NULL DEFAULT '[]'", 'attempt': 'INTEGER NOT NULL DEFAULT 0',
                                 'result': 'TEXT', 'pull_requests': "TEXT NOT NULL DEFAULT '[]'", 'checkpoint': 'TEXT',
                                 'deleted_at': 'DOUBLE PRECISION'}.items():
            if name not in columns:
                db.execute(f'ALTER TABLE work_items ADD COLUMN {name} {definition}')
        db.execute('''CREATE TABLE IF NOT EXISTS coding_runs (id TEXT PRIMARY KEY,
            work_id TEXT NOT NULL REFERENCES work_items(id),attempt INTEGER NOT NULL,revision TEXT NOT NULL,
            phase TEXT NOT NULL,result TEXT,created_at DOUBLE PRECISION NOT NULL,updated_at DOUBLE PRECISION NOT NULL,
            UNIQUE(work_id,attempt))''')
        for statement in (Path(__file__).parent.parent/'database/developer.sql').read_text().split(';'):
            if statement.strip():db.execute(statement)
        db.commit()
    except BaseException:
        db.rollback()
        db.close()
        raise
    return db


def setting(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def put(db, key, value):
    db.execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, str(value)))


def queue_report(db, text, chat_id=None, required_role="operator", event_key=None, reply_markup=None, message_key=None, authorized_by=None):
    if not text.strip():
        raise ValueError("Report is empty")
    # Telegram's text limit counts UTF-16 units. Keep room below 4096.
    chat_id = chat_id if chat_id is not None else setting(db, "owner")
    chunks, chunk, units = [], "", 0
    for character in text:
        size = 2 if ord(character) > 0xFFFF else 1
        if units + size > 3500:
            chunks.append(chunk)
            chunk, units = "", 0
        chunk += character
        units += size
    if chunk:
        chunks.append(chunk)
    for index, chunk in enumerate(chunks):
        key = f"{event_key}:{index}" if event_key else None
        db.execute("""INSERT INTO outbox(text,chat_id,required_role,event_key,reply_markup,message_key,authorized_by)
                      VALUES (?,?,?,?,?,?,?) ON CONFLICT(event_key) DO NOTHING""",
                   (chunk, chat_id, required_role, key,
                    json.dumps(reply_markup) if reply_markup and index == len(chunks)-1 else None,
                    message_key if len(chunks) == 1 else None, authorized_by))
