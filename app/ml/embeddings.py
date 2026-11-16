"""Explicit embedding providers for semantic retrieval.

The organizer sample defines POST /embeddings with {model, input} and indexed
float-vector responses. This module performs no implicit model download and no
network call until a configured provider's embed method is awaited.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from numbers import Real
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

import httpx

from .environment import env_value
from .protocol import ConsultationUnavailable

if TYPE_CHECKING:
    from .llm_client import LLMConfig

MAX_DIMENSIONS = 65536
MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class EmbeddingUnavailable(ConsultationUnavailable):
    """A sanitized, machine-readable embedding failure."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class InvalidEmbedding(EmbeddingUnavailable):
    def __init__(self, message: str = "Embedding vectors or response metadata are invalid"):
        super().__init__("invalid_embedding_response", message)


class EmbeddingProvider(Protocol):
    """Local and remote providers use the same document/query vector space.

    identity must change when model weights, revision, pooling, preprocessing or
    vector meaning changes. dimensions may be None until the first response.
    """

    identity: str
    model: str
    dimensions: int | None

    async def embed(self, texts: Sequence[str], *, timeout_budget_ms: int) -> list[list[float]]: ...


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validated_vector(vector: object, *, dimensions: int | None = None) -> tuple[float, ...]:
    """Reject booleans, strings, NaN/Inf, empty/zero and inconsistent vectors."""
    if not isinstance(vector, (list, tuple)) or not 1 <= len(vector) <= MAX_DIMENSIONS:
        raise InvalidEmbedding()
    if dimensions is not None and len(vector) != dimensions:
        raise InvalidEmbedding("Embedding dimensions do not match the configured vector space")
    if any(isinstance(value, bool) or not isinstance(value, Real) for value in vector):
        raise InvalidEmbedding()
    result = tuple(float(value) for value in vector)
    if not all(math.isfinite(value) for value in result):
        raise InvalidEmbedding()
    norm = math.hypot(*result)
    if not math.isfinite(norm) or norm <= 0:
        raise InvalidEmbedding("Embedding vector must have a finite nonzero norm")
    return result


def normalized_vector(vector: object, *, dimensions: int | None = None) -> tuple[float, ...]:
    result = validated_vector(vector, dimensions=dimensions)
    norm = math.hypot(*result)
    return tuple(value / norm for value in result)


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    allow_private_corpus: bool = False
    allow_internal_http: bool = False
    batch_size: int = 32
    timeout_seconds: float = 30.0
    max_retries: int = 2
    expected_dimensions: int | None = None
    model_revision: str = ""

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Embedding endpoint must be an HTTP(S) base URL without embedded credentials or query parameters")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"} and not self.allow_internal_http:
            raise ValueError("Non-local embedding endpoints require HTTPS or explicit organizer-proxy mode")
        if not self.model.strip() or not self.api_key.strip():
            raise ValueError("Embedding model and API key must be configured")
        if not self.allow_private_corpus:
            raise ValueError("Explicit permission to transmit private corpus text is required")
        if not 1 <= self.batch_size <= 256 or not 0 < self.timeout_seconds <= 1200 or not 0 <= self.max_retries <= 3:
            raise ValueError("Invalid embedding batching, timeout or retry configuration")
        if self.expected_dimensions is not None and (isinstance(self.expected_dimensions, bool) or not 1 <= self.expected_dimensions <= MAX_DIMENSIONS):
            raise ValueError("Invalid expected embedding dimension")

    @property
    def identity(self) -> str:
        # A stable hash identifies provider/model/revision without exposing URLs
        # or credentials in traces/cache filenames. API key rotation is harmless.
        descriptor = json.dumps({
            "protocol": "openai-float-embeddings-v1", "base_url": self.base_url.rstrip("/"),
            "model": self.model, "revision": self.model_revision,
            "expected_dimensions": self.expected_dimensions,
        }, sort_keys=True, separators=(",", ":"))
        return "openai:" + text_sha256(descriptor)

    @classmethod
    def from_env(cls, *, platform: bool = False, llm_config: LLMConfig | None = None) -> "EmbeddingConfig":
        get = env_value
        expected = get("EMBEDDING_DIMENSIONS")
        return cls(
            base_url=llm_config.base_url if llm_config is not None else get("LLM_BASE_URL", "BASE_URL", "OPENAI_BASE_URL"),
            api_key=get("EMBEDDER_API_KEY", default=llm_config.api_key) if llm_config is not None else get("EMBEDDER_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY"),
            model=get("EMB_MODEL", default="baai/bge-m3"),
            allow_private_corpus=platform or (llm_config.allow_private_corpus if llm_config is not None else get("ALLOW_PRIVATE_CORPUS_TO_LLM", "LLM_ALLOW_PRIVATE_CORPUS").lower() in {"1", "true", "yes"}),
            allow_internal_http=platform or (llm_config.allow_internal_http if llm_config is not None else get("LLM_ALLOW_INTERNAL_HTTP").lower() in {"1", "true", "yes"}),
            batch_size=int(get("EMBEDDING_BATCH_SIZE", default="32")),
            timeout_seconds=float(get("EMBEDDING_TIMEOUT_SECONDS", default="30")),
            max_retries=int(get("EMBEDDING_MAX_RETRIES", default="2")),
            expected_dimensions=int(expected) if expected else None,
            model_revision=get("EMBEDDING_MODEL_REVISION"),
        )


class OpenAIEmbeddingClient:
    """Cancellable async client with bounded retries inside one total budget."""

    def __init__(self, config: EmbeddingConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.model = config.model
        self.identity = config.identity
        self.dimensions = config.expected_dimensions
        self._transport = transport

    async def embed(self, texts: Sequence[str], *, timeout_budget_ms: int) -> list[list[float]]:
        if isinstance(texts, (str, bytes)) or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("Embedding input must be a sequence of nonempty strings")
        if not texts:
            return []
        seconds = min(self.config.timeout_seconds, timeout_budget_ms / 1000)
        if seconds <= 0:
            raise EmbeddingUnavailable("embedding_timeout", "Embedding time budget is exhausted")
        deadline = time.monotonic() + seconds
        try:
            async with asyncio.timeout(seconds):
                async with httpx.AsyncClient(transport=self._transport, follow_redirects=False, trust_env=False) as client:
                    vectors: list[list[float]] = []
                    for start in range(0, len(texts), self.config.batch_size):
                        batch = list(texts[start:start + self.config.batch_size])
                        payload = await self._post(client, batch, deadline)
                        vectors.extend(self._parse(payload, len(batch)))
                    return vectors
        except TimeoutError:
            raise EmbeddingUnavailable("embedding_timeout", "Embedding request exceeded its time budget") from None
        except asyncio.CancelledError:
            # Never convert cancellation to fallback here; callers decide whether
            # an explicit deadline should use lexical retrieval.
            raise

    async def _post(self, client: httpx.AsyncClient, texts: list[str], deadline: float) -> object:
        for attempt in range(self.config.max_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EmbeddingUnavailable("embedding_timeout", "Embedding request exceeded its time budget")
            try:
                async with client.stream(
                    "POST", self.config.base_url.rstrip("/") + "/embeddings",
                    headers={"Authorization": "Bearer " + self.config.api_key, "Accept-Encoding": "identity"},
                    json={"model": self.config.model, "input": texts},
                    timeout=httpx.Timeout(remaining),
                ) as response:
                    if response.status_code == 429 or 500 <= response.status_code < 600:
                        retryable = True
                    elif not 200 <= response.status_code < 300:
                        raise EmbeddingUnavailable("embedding_http_error", "Embedding API rejected the request")
                    else:
                        retryable = False
                        encodings = {part.strip().lower() for part in response.headers.get("content-encoding", "identity").split(",")}
                        if not encodings.issubset({"identity", "gzip", "deflate"}):
                            raise InvalidEmbedding("Embedding response uses unsupported compression")
                        content = bytearray()
                        # Bound decoded bytes; compressed lengths are not a safe
                        # measure of the JSON/vector payload held in memory.
                        async for part in response.aiter_bytes(chunk_size=65536):
                            content.extend(part)
                            if len(content) > MAX_RESPONSE_BYTES:
                                raise InvalidEmbedding("Embedding response exceeds the supported size")
                        try:
                            return json.loads(content)
                        except (ValueError, UnicodeDecodeError):
                            raise InvalidEmbedding("Embedding API did not return valid JSON") from None
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
                retryable = True
            except httpx.HTTPError:
                raise EmbeddingUnavailable("embedding_http_error", "Embedding transport failed") from None
            if retryable and attempt < self.config.max_retries:
                pause = min(0.15 * 2 ** attempt + random.uniform(0, 0.05), max(0.0, deadline - time.monotonic()))
                await asyncio.sleep(pause)
        raise EmbeddingUnavailable("embedding_unavailable", "Embedding API was unavailable within the retry budget")

    def _parse(self, payload: object, count: int) -> list[list[float]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list) or len(payload["data"]) != count:
            raise InvalidEmbedding("Embedding API returned the wrong number of vectors")
        ordered: list[tuple[float, ...] | None] = [None] * count
        dimension = self.dimensions
        for entry in payload["data"]:
            if not isinstance(entry, dict) or type(entry.get("index")) is not int:
                raise InvalidEmbedding("Embedding response indices are invalid")
            index = entry["index"]
            if not 0 <= index < count or ordered[index] is not None:
                raise InvalidEmbedding("Embedding response indices are invalid")
            vector = validated_vector(entry.get("embedding"), dimensions=dimension)
            dimension = len(vector) if dimension is None else dimension
            ordered[index] = vector
        # All positions exist because unique in-range indices and count agree.
        self.dimensions = dimension
        return [list(vector) for vector in ordered if vector is not None]


class PrecomputedEmbeddingProvider:
    """Offline vectors from a real local model, or synthetic test fixtures.

    This provider does not turn lexical features into 'semantic' embeddings. Its
    vectors must come from the named model; callers report that provenance.
    JSON format: {format_version: 1, model, identity, dimensions, embeddings:
    [{text_sha256: '<hash of exact input>', vector: [...]}, ...]}. An entry may use
    'text' instead of 'text_sha256'. Keep all vector files in private storage.
    """

    def __init__(self, vectors: Mapping[str, Sequence[float]], *, model: str, identity: str, dimensions: int | None = None, keys_are_hashes: bool = False):
        if not model.strip() or not identity.strip():
            raise ValueError("A precomputed provider needs an explicit model and revision identity")
        self.model = model
        self.identity = identity
        self.dimensions = dimensions
        self._vectors: dict[str, tuple[float, ...]] = {}
        for key, vector in vectors.items():
            if not isinstance(key, str):
                raise ValueError("Embedding lookup keys must be strings")
            lookup = key if keys_are_hashes else text_sha256(key)
            if len(lookup) != 64 or any(char not in "0123456789abcdef" for char in lookup):
                raise ValueError("Precomputed embedding keys must be SHA-256 hashes")
            checked = validated_vector(list(vector), dimensions=self.dimensions)
            self.dimensions = len(checked) if self.dimensions is None else self.dimensions
            self._vectors[lookup] = checked

    @classmethod
    def from_file(cls, path: str | Path) -> "PrecomputedEmbeddingProvider":
        path = Path(path)
        if path.stat().st_size > 256 * 1024 * 1024:
            raise ValueError("Precomputed embedding file is too large")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("format_version") != 1 or not isinstance(data.get("embeddings"), list):
            raise ValueError("Unsupported precomputed embedding file")
        vectors = {}
        for entry in data["embeddings"]:
            if not isinstance(entry, dict) or ("text" in entry) == ("text_sha256" in entry):
                raise ValueError("Each precomputed row needs exactly one text or text_sha256 key")
            key = text_sha256(entry["text"]) if "text" in entry else entry["text_sha256"]
            if key in vectors:
                raise ValueError("Duplicate precomputed embedding input")
            vectors[key] = entry["vector"]
        return cls(vectors, model=data["model"], identity=data["identity"], dimensions=data.get("dimensions"), keys_are_hashes=True)

    async def embed(self, texts: Sequence[str], *, timeout_budget_ms: int) -> list[list[float]]:
        if timeout_budget_ms <= 0:
            raise EmbeddingUnavailable("embedding_timeout", "Embedding time budget is exhausted")
        if isinstance(texts, (str, bytes)) or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("Embedding input must be a sequence of nonempty strings")
        try:
            return [list(self._vectors[text_sha256(text)]) for text in texts]
        except KeyError:
            raise EmbeddingUnavailable("precomputed_embedding_missing", "No precomputed embedding exists for this exact input") from None
