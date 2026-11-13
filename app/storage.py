from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3

import psycopg


SCHEMA_DIR = Path(__file__).resolve().parent.parent
PROGRESS_COLUMNS = {
    'progress_stage': "TEXT NOT NULL DEFAULT 'queued'",
    'progress_keywords': "TEXT NOT NULL DEFAULT '[]'",
}


class StorageError(RuntimeError):
    """A storage operation failed."""


class PostgresRow(dict):
    """Expose columns by name and by position, as sqlite3.Row does."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def postgres_row(cursor):
    columns = tuple(column.name for column in cursor.description or ())

    def make_row(values):
        return PostgresRow(zip(columns, values))

    return make_row


class PostgresQueries:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement, params=None):
        # Application SQL uses question marks solely for bind parameters.
        if params is not None:
            statement = statement.replace('%', '%%').replace('?', '%s')
        return self.connection.execute(statement, params)


class Database:
    SQLITE_SCHEMA_VERSION = 3
    POSTGRES_SCHEMA_VERSION = 1
    REQUIRED_TABLES = frozenset({
        'sessions', 'messages', 'turns', 'lead_drafts', 'contact_history',
        'lead_previews', 'leads', 'answer_traces', 'facts',
    })
    REQUIRED_INDEXES = frozenset({'one_processing_turn', 'one_current_fact'})

    def __init__(self, target: str | Path):
        self.path = str(target)
        self.backend = 'postgresql' if self.path.startswith(('postgresql://', 'postgres://')) else 'sqlite'
        if self.backend == 'sqlite':
            if self.path == ':memory:':
                raise ValueError('Use a temporary file: each transaction has an independent connection')
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.backend == 'postgresql':
                self._initialize_postgres()
            else:
                self._initialize_sqlite()
        except (sqlite3.Error, psycopg.Error) as exc:
            raise StorageError('Could not initialize database') from exc

    def _initialize_sqlite(self):
        with closing(self.connect()) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('BEGIN IMMEDIATE')
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version > self.SQLITE_SCHEMA_VERSION:
                raise RuntimeError('Database is newer than this application')
            if version < 2:
                # Run the historical v2 schema inside the migration transaction.
                schema = (SCHEMA_DIR / 'schema.sql').read_text(encoding='utf-8')
                statements = schema.split('BEGIN IMMEDIATE;', 1)[1].split('COMMIT;', 1)[0]
                for statement in statements.split(';'):
                    if statement.strip():
                        conn.execute(statement)
            if version < 3:
                columns = {row['name'] for row in conn.execute('PRAGMA table_info(turns)')}
                for name, definition in PROGRESS_COLUMNS.items():
                    if name not in columns:
                        conn.execute(f'ALTER TABLE turns ADD COLUMN {name} {definition}')
                conn.execute('PRAGMA user_version=3')
            conn.commit()

    def _initialize_postgres(self):
        with self.connect() as conn, conn.transaction():
            conn.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
            conn.execute("""SELECT pg_advisory_xact_lock(
                hashtextextended('hackchange.schema.' || current_schema(), 0)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS app_schema (
                singleton BOOLEAN PRIMARY KEY CHECK (singleton), version INTEGER NOT NULL
            )""")
            row = conn.execute('SELECT version FROM app_schema WHERE singleton').fetchone()
            version = row[0] if row else 0
            if version > self.POSTGRES_SCHEMA_VERSION:
                raise RuntimeError('Database is newer than this application')
            if version < 1:
                conn.execute((SCHEMA_DIR / 'schema_postgres.sql').read_text(encoding='utf-8'))
                conn.execute('INSERT INTO app_schema VALUES (true, 1)')

    def ready(self):
        with self.read() as conn:
            if self.backend == 'postgresql':
                return self._postgres_ready(conn)
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='index'")}
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(turns)')}
            return (version == self.SQLITE_SCHEMA_VERSION and self.REQUIRED_TABLES <= tables
                    and self.REQUIRED_INDEXES <= indexes and PROGRESS_COLUMNS.keys() <= columns)

    def _postgres_ready(self, conn):
        tables = {row[0] for row in conn.execute("""SELECT table_name FROM information_schema.tables
            WHERE table_schema=current_schema() AND table_type='BASE TABLE'""")}
        if not self.REQUIRED_TABLES.union({'app_schema'}) <= tables:
            return False
        version = conn.execute('SELECT version FROM app_schema WHERE singleton').fetchone()
        indexes = {row[0] for row in conn.execute("""SELECT idx.relname FROM pg_index i
            JOIN pg_class idx ON idx.oid=i.indexrelid
            JOIN pg_namespace ns ON ns.oid=idx.relnamespace
            WHERE ns.nspname=current_schema() AND i.indisunique AND i.indisvalid""")}
        columns = {row[0] for row in conn.execute("""SELECT column_name FROM information_schema.columns
            WHERE table_schema=current_schema() AND table_name='turns'""")}
        return (version is not None and version[0] == self.POSTGRES_SCHEMA_VERSION
                and self.REQUIRED_INDEXES <= indexes and PROGRESS_COLUMNS.keys() <= columns)

    def connect(self):
        if self.backend == 'postgresql':
            return psycopg.connect(self.path, autocommit=True, connect_timeout=5)
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=5000')
        return conn

    @contextmanager
    def transaction(self, session_id=None):
        try:
            with closing(self.connect()) as conn:
                if self.backend == 'postgresql':
                    conn.row_factory = postgres_row
                    with conn.transaction():
                        conn.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
                        conn.execute("SET LOCAL lock_timeout = '5s'")
                        if session_id is not None:
                            # Every mutation of an existing session starts with this lock.
                            conn.execute('SELECT id FROM sessions WHERE id=%s FOR NO KEY UPDATE', (session_id,))
                        yield PostgresQueries(conn)
                else:
                    conn.execute('BEGIN IMMEDIATE')
                    yield conn
                    conn.commit()
        except (sqlite3.Error, psycopg.Error) as exc:
            raise StorageError('Database transaction failed') from exc

    @contextmanager
    def read(self):
        try:
            with closing(self.connect()) as conn:
                if self.backend == 'postgresql':
                    conn.row_factory = postgres_row
                    with conn.transaction():
                        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
                        yield PostgresQueries(conn)
                else:
                    conn.execute('BEGIN')
                    yield conn
        except (sqlite3.Error, psycopg.Error) as exc:
            raise StorageError('Could not read database') from exc
