"""Copy a version 1–3 SQLite database into an empty PostgreSQL database."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.storage import Database


# Insert parents before children. Explicit column lists preserve data across
# additive schema changes without relying on column order in either database.
TABLE_COLUMNS = {
    'sessions': 'id token_hash status phase revision opened_at closed_at',
    'messages': 'id session_id seq role text created_at',
    'turns': (
        'id session_id client_message_id request_hash status user_message_id '
        'assistant_message_id attempt lease_until error_code retryable response_json created_at'
    ),
    'lead_drafts': 'session_id revision full_name phone company updated_at',
    'contact_history': 'session_id field_name value',
    'lead_previews': 'id session_id snapshot_last_seq draft_revision snapshot_json created_at',
    'leads': 'id session_id preview_id idempotency_key status phone payload_json created_at',
    'answer_traces': 'assistant_message_id session_id payload_json',
    'facts': 'id session_id topic_key fact_key value_json source_message_id superseded_at',
}
BATCH_SIZE = 1000


class MigrationError(Exception):
    """An import error whose message is safe to display."""


def migrate(sqlite_path: Path, database_url: str) -> dict[str, int]:
    if not database_url.startswith(('postgresql://', 'postgres://')):
        raise MigrationError('A PostgreSQL database URL is required.')
    source_uri = f'{sqlite_path.resolve().as_uri()}?mode=ro'
    with closing(sqlite3.connect(source_uri, uri=True)) as source:
        source.execute('BEGIN')
        version = source.execute('PRAGMA user_version').fetchone()[0]
        if version not in (1, 2, 3):
            raise MigrationError(f'Unsupported SQLite schema version {version}.')

        database = Database(database_url)
        with database.connect() as target, target.transaction():
            target.execute(
                sql.SQL('LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE').format(
                    sql.SQL(', ').join(map(sql.Identifier, TABLE_COLUMNS))
                )
            )
            for table in TABLE_COLUMNS:
                has_rows = target.execute(
                    sql.SQL('SELECT EXISTS (SELECT 1 FROM {})').format(sql.Identifier(table))
                ).fetchone()[0]
                if has_rows:
                    raise MigrationError(f'PostgreSQL target is not empty (table {table}).')

            counts = dict.fromkeys(TABLE_COLUMNS, 0)
            with target.cursor() as writer:
                for table, column_names in TABLE_COLUMNS.items():
                    if table == 'facts' and version == 1:
                        continue
                    if table == 'turns' and version >= 3:
                        column_names += ' progress_stage progress_keywords'
                    columns = column_names.split()
                    # All table and column names come from the constants above.
                    reader = source.execute(f"SELECT {', '.join(columns)} FROM {table}")
                    insert = sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(
                        sql.Identifier(table),
                        sql.SQL(', ').join(map(sql.Identifier, columns)),
                        sql.SQL(', ').join(sql.Placeholder() for _ in columns),
                    )
                    while batch := reader.fetchmany(BATCH_SIZE):
                        writer.executemany(insert, batch)
                        counts[table] += len(batch)

            next_fact_id = target.execute('SELECT GREATEST(COALESCE(MAX(id), 0), 0) FROM facts').fetchone()[0] + 1
            target.execute(
                sql.SQL('ALTER TABLE facts ALTER COLUMN id RESTART WITH {}').format(sql.Literal(next_fact_id))
            )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sqlite-path', required=True, type=Path, help='Path to the source SQLite database')
    parser.add_argument('--database-url', default=os.getenv('DATABASE_URL', ''), help='Target URL (defaults to DATABASE_URL)')
    args = parser.parse_args()
    try:
        counts = migrate(args.sqlite_path, args.database_url)
    except MigrationError as exc:
        parser.exit(1, f'{exc}\n')
    except sqlite3.Error:
        parser.exit(1, 'Could not read the SQLite source database.\n')
    except (psycopg.Error, RuntimeError):
        # Driver errors can contain row values or connection details.
        parser.exit(1, 'Could not import into PostgreSQL. Check the connection, schema and data constraints.\n')
    print(json.dumps(counts))


if __name__ == '__main__':
    main()
