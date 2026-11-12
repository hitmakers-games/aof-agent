"""PostgreSQL connection with the gateway's existing transaction interface."""
import datetime
from pathlib import Path

import psycopg
from psycopg.pq import TransactionStatus

from database.errors import StorageError


def legacy_row(row):
    if row is None:
        return None
    return tuple(value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                 if isinstance(value, datetime.datetime) else value for value in row)


class Cursor:
    def __init__(self, cursor):
        self.cursor = cursor
        self.description = cursor.description
        self.rowcount = cursor.rowcount

    def fetchone(self):
        return legacy_row(self.cursor.fetchone())

    def fetchall(self):
        return [legacy_row(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        return (legacy_row(row) for row in self.cursor)


class Connection:
    claim_lock = " FOR UPDATE SKIP LOCKED"
    row_lock = " FOR UPDATE"

    def __init__(self, dsn, ca_file, database=None):
        options = {"dbname": database} if database else {}
        try:
            self.raw = psycopg.connect(dsn, autocommit=True, connect_timeout=5,
                sslmode="verify-full", sslrootcert=str(ca_file),
                options="-c timezone=UTC -c statement_timeout=10000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=15000",
                **options)
        except psycopg.Error:
            raise StorageError("PostgreSQL connection failed") from None

    def execute(self, query, parameters=None):
        # Existing statements use DB-API qmark parameters. Values remain bound;
        # this conversion never touches user data or interpolates SQL values.
        if query == "BEGIN IMMEDIATE":
            if self.raw.info.transaction_status != TransactionStatus.IDLE:
                return None
            query = "BEGIN"
        try:
            return Cursor(self.raw.execute(query.replace("?", "%s"), parameters))
        except psycopg.Error:
            raise StorageError("PostgreSQL operation failed") from None

    def __enter__(self):
        if self.raw.info.transaction_status == TransactionStatus.IDLE:
            self.execute("BEGIN")
        return self

    def __exit__(self, kind, value, traceback):
        if kind is None:
            self.commit()
        else:
            self.rollback()

    def commit(self):
        try:
            self.raw.commit()
        except psycopg.Error:
            raise StorageError("PostgreSQL commit failed") from None

    def rollback(self):
        try:
            self.raw.rollback()
        except psycopg.Error:
            raise StorageError("PostgreSQL rollback failed") from None

    def close(self):
        self.raw.close()


def initialize(db):
    with db:
        db.execute("SELECT pg_advisory_xact_lock(3350001)")
        db.execute(Path(__file__).with_name("schema.sql").read_text())
        if db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] < 2:
            db.execute(Path(__file__).with_name("schema_v2.sql").read_text())
        db.execute(Path(__file__).with_name("gateway.sql").read_text())
        db.execute("ALTER TABLE work_items ADD COLUMN IF NOT EXISTS deleted_at DOUBLE PRECISION")
        if db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] < 3:
            db.execute(Path(__file__).with_name("coding.sql").read_text())
        if db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] < 4:
            db.execute(Path(__file__).with_name('developer.sql').read_text())
            db.execute('INSERT INTO schema_migrations(version) VALUES (4)')
