from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3


class Database:
    SCHEMA_VERSION = 2
    REQUIRED_TABLES = frozenset({
        'sessions', 'messages', 'turns', 'lead_drafts', 'contact_history',
        'lead_previews', 'leads', 'answer_traces', 'facts',
    })
    REQUIRED_INDEXES = frozenset({'one_processing_turn', 'one_current_fact'})

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path == ':memory:':
            raise ValueError('Use a temporary file: each transaction has an independent connection')
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version > self.SCHEMA_VERSION:
                raise RuntimeError('Database is newer than this application')
            if version < self.SCHEMA_VERSION:
                schema = (Path(__file__).parent.parent / 'schema.sql').read_text()
                conn.executescript(schema)

    def ready(self):
        with self.read() as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='index'")}
            return (version == self.SCHEMA_VERSION and self.REQUIRED_TABLES <= tables
                    and self.REQUIRED_INDEXES <= indexes)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA busy_timeout=5000')
        return conn

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self):
        conn = self.connect()
        try:
            conn.execute('BEGIN')
            yield conn
        finally:
            conn.close()
