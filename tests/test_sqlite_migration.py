from contextlib import closing
from pathlib import Path
import sqlite3

import psycopg
import pytest
from psycopg import sql

from app.storage import Database
from scripts.migrate_sqlite_to_postgres import MigrationError, TABLE_COLUMNS, migrate


SQLITE_SCHEMA = Path(__file__).resolve().parents[1] / 'schema.sql'
STAMP = '2025-01-01T00:00:00+00:00'
SNAPSHOT = '{"full_name": "Мария", "source": "user"}'
ROWS = {
    'sessions': [('session', 'token-hash', 'active', 'new', 4, STAMP, None)],
    'messages': [
        ('question', 'session', 1, 'user', 'Подскажите тариф', STAMP),
        ('answer', 'session', 2, 'assistant', 'Уточните адрес', STAMP),
        ('retry', 'session', 3, 'user', 'В Москве', STAMP),
        ('pending', 'session', 4, 'user', 'В другом городе', STAMP),
    ],
    'turns': [
        ('done', 'session', 'client-1', 'req-1', 'completed', 'question', 'answer', 1, None, None, 0, SNAPSHOT, STAMP),
        ('failed', 'session', 'client-2', 'req-2', 'failed', 'retry', None, 2, None, 'ML_TIMEOUT', 1, None, STAMP),
        ('open', 'session', 'client-3', 'req-3', 'processing', 'pending', None, 3, 1_800_000_000.25, None, 0, None, STAMP),
    ],
    'lead_drafts': [('session', 2, 'Мария', '+79000000000', None, STAMP)],
    'contact_history': [('session', 'full_name', 'Мария')],
    'lead_previews': [('preview', 'session', 2, 2, SNAPSHOT, STAMP)],
    'leads': [('lead', 'session', 'preview', 'key', 'partial', '+79000000000', SNAPSHOT, STAMP)],
    'answer_traces': [('answer', 'session', SNAPSHOT)],
    'facts': [
        (2, 'session', 'topic', 'city', '"Москва"', 'question', STAMP),
        (42, 'session', 'topic', 'city', '"Тула"', 'retry', None),
    ],
}


def make_source(path, version):
    schema = SQLITE_SCHEMA.read_text()
    if version == 1:
        schema = schema.split('CREATE TABLE IF NOT EXISTS facts (', 1)[0] + 'PRAGMA user_version=1; COMMIT;'
    with closing(sqlite3.connect(path)) as source, source:
        source.executescript(schema)
        for table, rows in ROWS.items():
            if table == 'facts' and version == 1:
                continue
            columns = TABLE_COLUMNS[table].split()
            placeholders = ', '.join('?' for _ in columns)
            source.executemany(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})", rows)
    if version == 3:
        database = Database(path)
        with database.transaction() as source:
            source.execute(
                'UPDATE turns SET progress_stage = ?, progress_keywords = ? WHERE id = ?',
                ('preparing_answer', '["тариф"]', 'open'),
            )


@pytest.mark.parametrize('version', [1, 2, 3])
def test_import_preserves_rows_and_restarts_fact_identity(tmp_path, postgres_url, version):
    path = tmp_path / 'source #1.sqlite3'
    make_source(path, version)
    before = path.read_bytes()
    expected = {table: rows if table != 'facts' or version >= 2 else [] for table, rows in ROWS.items()}

    assert migrate(path, postgres_url) == {table: len(rows) for table, rows in expected.items()}
    assert path.read_bytes() == before
    with pytest.raises(MigrationError, match='target is not empty'):
        migrate(path, postgres_url)

    with psycopg.connect(postgres_url) as target:
        for table, rows in expected.items():
            columns = sql.SQL(', ').join(map(sql.Identifier, TABLE_COLUMNS[table].split()))
            actual = target.execute(sql.SQL('SELECT {} FROM {}').format(columns, sql.Identifier(table)))
            assert set(actual) == set(rows)
        progress = target.execute("SELECT progress_stage, progress_keywords FROM turns WHERE id = 'open'").fetchone()
        assert progress == (('preparing_answer', '["тариф"]') if version == 3 else ('queued', '[]'))
        generated = target.execute(
            "INSERT INTO facts (session_id, topic_key, fact_key, value_json, source_message_id) "
            "VALUES ('session', 'topic', 'name', '\"Мария\"', 'question') RETURNING id"
        ).fetchone()[0]
        assert generated == (1 if version == 1 else 43)


def test_import_rolls_back_earlier_tables_on_late_foreign_key_failure(tmp_path, postgres_url):
    path = tmp_path / 'source.sqlite3'
    make_source(path, 2)
    with closing(sqlite3.connect(path)) as source, source:
        source.execute("UPDATE facts SET source_message_id = 'missing' WHERE id = 42")

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        migrate(path, postgres_url)

    with psycopg.connect(postgres_url) as target:
        for table in TABLE_COLUMNS:
            assert target.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(table))).fetchone() == (0,)

    with closing(sqlite3.connect(path)) as source, source:
        source.execute("UPDATE facts SET source_message_id = 'retry' WHERE id = 42")
    assert migrate(path, postgres_url)['facts'] == 2


def test_import_rejects_missing_or_unsupported_source_without_connecting(tmp_path):
    path = tmp_path / 'source.sqlite3'
    unused_url = 'postgresql://invalid'

    with pytest.raises(sqlite3.OperationalError):
        migrate(path, unused_url)
    assert not path.exists()

    with closing(sqlite3.connect(path)) as source:
        source.execute('PRAGMA user_version=99')
    with pytest.raises(MigrationError, match='Unsupported SQLite schema version 99'):
        migrate(path, unused_url)
