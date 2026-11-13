"""BM25 + dense cosine retrieval with reciprocal-rank fusion.

RRF is a ranking score, never factual confidence. Lexical overlap/coverage remain
body-only evidence indicators; a dense hit must not bypass answer grounding.
Corpus vectors are private and cached only in the explicitly supplied directory.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import math
import os
import tempfile
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .corpus import Chunk, Corpus, canonical_json
from .embeddings import EmbeddingProvider, EmbeddingUnavailable, InvalidEmbedding, normalized_vector, text_sha256
from .retriever import BM25Retriever, SearchHit, tokens

CACHE_FORMAT_VERSION = 1
MAX_CACHE_BYTES = 256 * 1024 * 1024
_BUILD_GUARD = threading.Lock()
_INFLIGHT: dict[str, concurrent.futures.Future] = {}
_READY: weakref.WeakValueDictionary = weakref.WeakValueDictionary()


def document_embedding_text(chunk: Chunk) -> str:
    return f"{chunk.title}\n\n{chunk.text}"


def query_embedding_text(query: str, context: str = "") -> str:
    return f"{query}\nКонтекст:\n{context}" if context else query


@dataclass(frozen=True)
class DenseIndex:
    corpus_version: str
    provider_identity: str
    dimensions: int
    chunk_ids: tuple[str, ...]
    vectors: tuple[tuple[float, ...], ...]
    cache_key: str


@dataclass(frozen=True)
class HybridRanking:
    chunk_id: str
    lexical_rank: int | None
    dense_rank: int | None
    lexical_score: float | None
    dense_score: float | None
    fused_score: float


@dataclass(frozen=True)
class HybridSearchResult:
    hits: list[SearchHit]
    mode: Literal["hybrid", "lexical", "dense", "lexical_fallback"]
    fallback_reason: str | None
    timings_ms: dict[str, float]
    rankings: list[HybridRanking] = field(default_factory=list)
    embedding_model: str | None = None
    cache_hit: bool = False
    lexical_hits: list[SearchHit] = field(default_factory=list)
    dense_hits: list[SearchHit] = field(default_factory=list)


def _descriptor(corpus: Corpus, provider: EmbeddingProvider) -> dict:
    # Include actual content hashes even when an upstream corpus version was
    # accidentally reused. Title/preprocessing changes invalidate vectors too.
    return {
        "format_version": CACHE_FORMAT_VERSION,
        "embedding_input_format": "title-double-newline-body-v1",
        "corpus_version": corpus.version,
        "provider_identity": provider.identity,
        "model": provider.model,
        "documents": [[doc.document_id, doc.checksum] for doc in corpus.documents],
        "chunks": [[chunk.chunk_id, chunk.document_id, chunk.checksum, text_sha256(document_embedding_text(chunk))] for chunk in corpus.chunks],
    }


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class _EmbeddingCache:
    def __init__(self, directory: str | Path | None, corpus: Corpus, provider: EmbeddingProvider):
        self.directory = Path(directory).resolve() if directory is not None else None
        self.descriptor = _descriptor(corpus, provider)
        self.request_key = _digest(self.descriptor)
        self.path = self.directory / f"embeddings-{self.request_key}.json" if self.directory is not None else None
        self.corpus = corpus
        self.provider = provider

    def make_index(self, vectors: object) -> DenseIndex:
        if not isinstance(vectors, (list, tuple)) or len(vectors) != len(self.corpus.chunks) or not vectors:
            raise InvalidEmbedding("Corpus embedding count does not match the loaded chunks")
        dimension = self.provider.dimensions
        normalized = []
        for vector in vectors:
            checked = normalized_vector(vector, dimensions=dimension)
            dimension = len(checked) if dimension is None else dimension
            normalized.append(checked)
        metadata = {**self.descriptor, "dimensions": dimension}
        return DenseIndex(
            self.corpus.version, self.provider.identity, dimension,
            tuple(chunk.chunk_id for chunk in self.corpus.chunks), tuple(normalized), _digest(metadata),
        )

    def load(self) -> DenseIndex | None:
        if self.path is None:
            return None
        try:
            if self.path.stat().st_size > MAX_CACHE_BYTES:
                return None
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("descriptor") != self.descriptor:
                return None
            checksum = data.pop("payload_sha256", None)
            if checksum != _digest(data):
                return None
            dimension = data.get("dimensions")
            if type(dimension) is not int or dimension <= 0:
                return None
            if self.provider.dimensions is not None and dimension != self.provider.dimensions:
                return None
            index = self.make_index(data["vectors"])
            if index.dimensions != dimension or index.cache_key != data.get("cache_key") or list(index.chunk_ids) != data.get("chunk_ids"):
                return None
            return index
        except (OSError, ValueError, TypeError, KeyError, EmbeddingUnavailable):
            return None

    def save(self, index: DenseIndex) -> None:
        if self.path is None or self.directory is None:
            return
        payload = {
            "descriptor": self.descriptor,
            "dimensions": index.dimensions,
            "cache_key": index.cache_key,
            "chunk_ids": list(index.chunk_ids),
            "vectors": index.vectors,
        }
        payload["payload_sha256"] = _digest(payload)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A unique temp file + replace avoids torn caches and concurrent writers.
        # mkstemp creates mode 0600; no raw corpus text or API secret is stored.
        descriptor, temporary = tempfile.mkstemp(prefix=".embedding-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class HybridRetriever:
    def __init__(
        self, corpus: Corpus, provider: EmbeddingProvider | None = None, *,
        cache_dir: str | Path | None = None, candidate_pool: int = 20, rrf_k: float = 60.0,
        lexical_weight: float = 1.0, dense_weight: float = 1.0,
    ):
        if not corpus.chunks:
            raise ValueError("Hybrid retrieval requires a nonempty corpus")
        if not 1 <= candidate_pool <= 10000 or not math.isfinite(rrf_k) or rrf_k <= 0:
            raise ValueError("Invalid retrieval candidate pool or RRF constant")
        if not all(math.isfinite(weight) and weight >= 0 for weight in (lexical_weight, dense_weight)) or lexical_weight + dense_weight <= 0:
            raise ValueError("Retrieval weights must be finite, nonnegative and not both zero")
        if provider is not None and (not provider.identity.strip() or not provider.model.strip()):
            raise ValueError("Embedding provider identity and model must be explicit")
        self.corpus = corpus
        self.provider = provider
        self.candidate_pool = candidate_pool
        self.rrf_k = rrf_k
        self.lexical_weight = lexical_weight
        self.dense_weight = dense_weight
        self.lexical = BM25Retriever(corpus)
        self._chunks = corpus.chunks_by_id
        self._body_terms = {chunk.chunk_id: set(tokens(chunk.text)) for chunk in corpus.chunks}
        self._cache = _EmbeddingCache(cache_dir, corpus, provider) if provider is not None else None
        self._dense: DenseIndex | None = None
        self._last_cache_hit = False
        self._prepare_failure_code: str | None = None

    @property
    def dense_ready(self) -> bool:
        return self._dense is not None

    async def prepare(self, *, timeout_budget_ms: int = 120000, retry_failed: bool = False) -> DenseIndex:
        """Warm/load corpus vectors once; concurrent callers share the build.

        A cancelled owner releases the single-flight entry. Waiter cancellation
        cannot cancel another request's build. A later call can retry failures.
        """
        if self.provider is None or self._cache is None:
            raise EmbeddingUnavailable("embeddings_not_configured", "No semantic embedding provider is configured")
        if retry_failed:
            self._prepare_failure_code = None
        if self._prepare_failure_code is not None:
            raise EmbeddingUnavailable(self._prepare_failure_code, "Corpus embedding preparation previously failed; explicitly retry after recovery")
        if self._dense is not None:
            self._last_cache_hit = True
            return self._dense
        if timeout_budget_ms <= 0:
            raise EmbeddingUnavailable("embedding_timeout", "Embedding preparation has no remaining time budget")
        key = str(self._cache.path) if self._cache.path is not None else "memory:" + self._cache.request_key
        with _BUILD_GUARD:
            ready = _READY.get(key)
            if ready is not None and (self.provider.dimensions is None or ready.dimensions == self.provider.dimensions):
                self._dense = ready
                self._last_cache_hit = True
                return ready
            future = _INFLIGHT.get(key)
            owner = future is None
            if owner:
                future = concurrent.futures.Future()
                _INFLIGHT[key] = future
        if not owner:
            try:
                async with asyncio.timeout(timeout_budget_ms / 1000):
                    index = await asyncio.shield(asyncio.wrap_future(future))
            except TimeoutError:
                self._prepare_failure_code = "embedding_timeout"
                raise EmbeddingUnavailable("embedding_timeout", "Shared embedding preparation exceeded this request's budget") from None
            except EmbeddingUnavailable as exc:
                self._prepare_failure_code = exc.code
                raise
            self._dense = index
            self._last_cache_hit = True
            return index
        try:
            async with asyncio.timeout(timeout_budget_ms / 1000):
                cached = self._cache.load()
                if cached is not None:
                    self._dense = cached
                    self._last_cache_hit = True
                else:
                    vectors = await self.provider.embed(
                        [document_embedding_text(chunk) for chunk in self.corpus.chunks],
                        timeout_budget_ms=timeout_budget_ms,
                    )
                    index = self._cache.make_index(vectors)
                    try:
                        self._cache.save(index)
                    except OSError:
                        raise EmbeddingUnavailable("embedding_cache_error", "Private embedding cache could not be persisted") from None
                    self._dense = index
                    self._last_cache_hit = False
            with _BUILD_GUARD:
                _READY[key] = self._dense
            future.set_result(self._dense)
            return self._dense
        except TimeoutError:
            self._prepare_failure_code = "embedding_timeout"
            error = EmbeddingUnavailable("embedding_timeout", "Corpus embedding preparation exceeded its time budget")
            future.set_exception(error)
            raise error from None
        except asyncio.CancelledError:
            future.set_exception(EmbeddingUnavailable("embedding_build_cancelled", "Shared embedding preparation was cancelled"))
            raise
        except Exception as exc:
            error = exc if isinstance(exc, EmbeddingUnavailable) else EmbeddingUnavailable("invalid_embedding_state", "Corpus embedding preparation failed")
            self._prepare_failure_code = error.code
            future.set_exception(error)
            raise error from None
        finally:
            with _BUILD_GUARD:
                if _INFLIGHT.get(key) is future:
                    del _INFLIGHT[key]

    def _hit(self, chunk_id: str, score: float, query_terms: set[str]) -> SearchHit:
        overlap = len(query_terms & self._body_terms[chunk_id])
        return SearchHit(self._chunks[chunk_id], score, overlap, overlap / len(query_terms) if query_terms else 0.0)

    async def search(
        self, query: str, *, context: str = "", limit: int = 5, timeout_budget_ms: int = 10000,
    ) -> HybridSearchResult:
        started = time.perf_counter()
        timings = dict.fromkeys(("lexical", "index", "embedding_query", "dense", "fusion", "total"), 0.0)
        if limit <= 0:
            return HybridSearchResult([], "lexical", None, timings)
        if not isinstance(query, str) or not isinstance(context, str):
            raise ValueError("Retrieval query and context must be strings")
        query_terms = set(tokens(query))
        pool = max(limit, self.candidate_pool)
        lexical_started = time.perf_counter()
        lexical_hits = self.lexical.search(query, context=context, limit=pool)
        timings["lexical"] = (time.perf_counter() - lexical_started) * 1000

        def lexical_result(mode: Literal["lexical", "lexical_fallback"], reason: str | None = None) -> HybridSearchResult:
            timings["total"] = (time.perf_counter() - started) * 1000
            return HybridSearchResult(
                lexical_hits[:limit], mode, reason, timings,
                [HybridRanking(hit.chunk.chunk_id, rank, None, hit.score, None, hit.score) for rank, hit in enumerate(lexical_hits[:limit], 1)],
                self.provider.model if self.provider else None, self._last_cache_hit,
                lexical_hits[:limit], [],
            )

        if self.provider is None or self.dense_weight == 0:
            return lexical_result("lexical")
        if not query.strip():
            return lexical_result("lexical", "empty_query")
        deadline = time.monotonic() + max(0, timeout_budget_ms) / 1000
        remaining = lambda: max(0, int((deadline - time.monotonic()) * 1000))
        try:
            async with asyncio.timeout(max(0, timeout_budget_ms) / 1000):
                stage = time.perf_counter()
                try:
                    index = await self.prepare(timeout_budget_ms=remaining())
                finally:
                    timings["index"] = (time.perf_counter() - stage) * 1000
                stage = time.perf_counter()
                try:
                    vectors = await self.provider.embed([query_embedding_text(query, context)], timeout_budget_ms=remaining())
                    if not isinstance(vectors, (list, tuple)) or len(vectors) != 1:
                        raise InvalidEmbedding("Query embedding response must contain exactly one vector")
                    vector = normalized_vector(vectors[0], dimensions=index.dimensions)
                finally:
                    timings["embedding_query"] = (time.perf_counter() - stage) * 1000
                stage = time.perf_counter()
                dense_scores = [
                    (chunk_id, max(-1.0, min(1.0, math.fsum(left * right for left, right in zip(vector, document)))))
                    for chunk_id, document in zip(index.chunk_ids, index.vectors)
                ]
                dense_scores.sort(key=lambda item: (-item[1], item[0]))
                dense_scores = dense_scores[:pool]
                timings["dense"] = (time.perf_counter() - stage) * 1000
                stage = time.perf_counter()
                lexical_map = {hit.chunk.chunk_id: (rank, hit.score) for rank, hit in enumerate(lexical_hits, 1)}
                dense_map = {chunk_id: (rank, score) for rank, (chunk_id, score) in enumerate(dense_scores, 1)}
                ranking = []
                for chunk_id in lexical_map.keys() | dense_map.keys():
                    lexical_rank, lexical_score = lexical_map.get(chunk_id, (None, None))
                    dense_rank, dense_score = dense_map.get(chunk_id, (None, None))
                    fused = (
                        self.lexical_weight / (self.rrf_k + lexical_rank) if lexical_rank is not None else 0.0
                    ) + (self.dense_weight / (self.rrf_k + dense_rank) if dense_rank is not None else 0.0)
                    if fused > 0:
                        ranking.append(HybridRanking(chunk_id, lexical_rank, dense_rank, lexical_score, dense_score, fused))
                ranking.sort(key=lambda item: (-item.fused_score, item.chunk_id))
                selected = ranking[:limit]
                hits = [self._hit(item.chunk_id, item.fused_score, query_terms) for item in selected]
                dense_hits = [self._hit(chunk_id, score, query_terms) for chunk_id, score in dense_scores[:limit]]
                timings["fusion"] = (time.perf_counter() - stage) * 1000
                timings["total"] = (time.perf_counter() - started) * 1000
                return HybridSearchResult(
                    hits, "dense" if self.lexical_weight == 0 else "hybrid", None, timings,
                    selected, self.provider.model, self._last_cache_hit, lexical_hits[:limit], dense_hits,
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return lexical_result("lexical_fallback", "embedding_timeout")
        except EmbeddingUnavailable as exc:
            if isinstance(exc, InvalidEmbedding):
                # Do not keep using a possibly stale vector space. Cache reload
                # will validate the provider's newly observed dimensions.
                self._dense = None
            return lexical_result("lexical_fallback", exc.code)
        except Exception:
            return lexical_result("lexical_fallback", "invalid_embedding_state")
