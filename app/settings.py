"""Small explicit configuration; no secrets in defaults or representations."""
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    database_path: str | Path = 'data/energytool.db'
    index_dir: str | Path = 'private/index'
    review_token: str = field(default='', repr=False)
    ml_timeout_seconds: float = 10.0
    turn_lease_seconds: float = 30.0
    sync_wait_seconds: float = 0.5
    cors_origins: tuple[str, ...] = ('http://localhost:5173',)
    max_body_bytes: int = 32768
    requests_per_minute: int = 120
    llm_enabled: bool = False
    database_url: str = field(default='', repr=False)

    def __post_init__(self):
        if not 0 < self.ml_timeout_seconds <= 120:
            raise ValueError('ML timeout must be in (0, 120] seconds')
        if not isfinite(self.turn_lease_seconds) or self.turn_lease_seconds <= self.ml_timeout_seconds:
            raise ValueError('Turn lease must exceed ML timeout')
        if not isfinite(self.sync_wait_seconds) or self.sync_wait_seconds < 0 or self.requests_per_minute < 1:
            raise ValueError('Invalid wait/rate configuration')
        if self.max_body_bytes < 1:
            raise ValueError('Request body limit must be positive')
        if '*' in self.cors_origins:
            raise ValueError('Configure explicit CORS origins')
        if self.database_url and not self.database_url.startswith(('postgresql://', 'postgres://')):
            raise ValueError('DATABASE_URL must use PostgreSQL')

    @classmethod
    def from_env(cls):
        return cls(
            database_path=os.getenv('DATABASE_PATH', 'data/energytool.db'),
            database_url=os.getenv('DATABASE_URL', '').strip(),
            index_dir=os.getenv('INDEX_DIR', 'private/index'),
            review_token=os.getenv('REVIEW_TOKEN', ''),
            ml_timeout_seconds=float(os.getenv('ML_TIMEOUT_SECONDS', '10')),
            turn_lease_seconds=float(os.getenv('TURN_LEASE_SECONDS', '30')),
            sync_wait_seconds=float(os.getenv('SYNC_WAIT_SECONDS', '0.5')),
            cors_origins=tuple(x.strip() for x in os.getenv('CORS_ORIGINS', 'http://localhost:5173').split(',') if x.strip()),
            max_body_bytes=int(os.getenv('MAX_BODY_BYTES', '32768')),
            requests_per_minute=int(os.getenv('REQUESTS_PER_MINUTE', '120')),
            llm_enabled=os.getenv('LLM_ENABLED','false').lower() == 'true',
        )
