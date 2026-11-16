"""Small shared retrieval configuration for the web and competition adapters."""
from __future__ import annotations

from dataclasses import dataclass
import math
from .environment import env_value


@dataclass(frozen=True)
class RetrievalConfig:
    mode: str = "bm25"
    top_k: int = 6
    candidate_pool: int = 20
    rrf_k: float = 60.0
    lexical_weight: float = 1.0
    dense_weight: float = 1.0
    timeout_seconds: float = 8.0
    cache_dir: str | None = None

    def __post_init__(self):
        if self.mode not in {"bm25", "hybrid"}:
            raise ValueError("RETRIEVAL_MODE must be bm25 or hybrid")
        if not 1 <= self.top_k <= 30 or not self.top_k <= self.candidate_pool <= 200:
            raise ValueError("TOP_K must be 1..30 and RETRIEVAL_CANDIDATES must be TOP_K..200")
        values = (self.rrf_k, self.lexical_weight, self.dense_weight, self.timeout_seconds)
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError("Retrieval weights, RRF constant and timeout must be finite and positive")
        if self.timeout_seconds > 120:
            raise ValueError("RETRIEVAL_TIMEOUT_SECONDS must not exceed 120")

    @classmethod
    def from_env(cls, *, enabled: bool) -> "RetrievalConfig":
        top_k = int(env_value("TOP_K", default="6"))
        return cls(
            mode=env_value("RETRIEVAL_MODE", default="hybrid") if enabled else "bm25",
            top_k=top_k,
            candidate_pool=int(env_value("RETRIEVAL_CANDIDATES", default=str(max(20, top_k)))),
            rrf_k=float(env_value("RRF_K", default="60")),
            lexical_weight=float(env_value("LEXICAL_WEIGHT", default="1")),
            dense_weight=float(env_value("DENSE_WEIGHT", default="1")),
            timeout_seconds=float(env_value("RETRIEVAL_TIMEOUT_SECONDS", default="8")),
            cache_dir=env_value("EMBEDDING_CACHE_DIR") or None,
        )
