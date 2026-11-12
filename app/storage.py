from contextlib import contextmanager
from pathlib import Path
import sqlite3


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path == ':memory:':
            raise ValueError('Use a temporary file: each transaction has an independent connection')
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.read() as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version > 1:
                raise RuntimeError('Database is newer than this application')
            conn.executescript((Path(__file__).parent.parent / 'schema.sql').read_text())

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
            yield conn
        finally:
            conn.close()
