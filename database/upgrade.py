"""Apply the additive gateway schema upgrade to the explicitly configured database."""
import json
import os
from pathlib import Path
import sys

from database.errors import StorageError
from database.postgres import Connection, initialize


def main():
    db=Connection(Path(os.environ['AOF_DATABASE_DSN_FILE']).read_text().strip(),os.environ['AOF_DATABASE_CA_FILE'])
    try:
        if db.execute('SELECT current_database(),current_user').fetchone()!=('aof_agent','aof_agent'):
            raise StorageError('Unexpected upgrade target')
        before={table:db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('settings','inbox','outbox')}
        initialize(db)
        after={table:db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in before}
        if before!=after:
            raise StorageError('Unexpected row-count change')
        print(json.dumps({'schema_version':3,'original_table_counts_preserved':after,
                          'legacy_chats_backfilled':db.execute('SELECT count(*) FROM inbox WHERE chat_id IS NOT NULL').fetchone()[0]}))
    finally:
        db.close()


if __name__=='__main__':
    try:main()
    except (StorageError,OSError,ValueError):
        print('Schema upgrade failed; credentials and records omitted.',file=sys.stderr)
        sys.exit(1)
