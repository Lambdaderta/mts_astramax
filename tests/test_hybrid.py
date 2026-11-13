"""Synthetic vectors test mechanics, never claim semantic model quality."""
from __future__ import annotations

import asyncio
import gc
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.ml.corpus import Corpus, ingest
from app.ml.embeddings import (
    EmbeddingConfig, EmbeddingUnavailable, InvalidEmbedding, OpenAIEmbeddingClient,
    PrecomputedEmbeddingProvider, normalized_vector, text_sha256,
)
from app.ml.hybrid import HybridRetriever, document_embedding_text, query_embedding_text


class CountingProvider:
    """Hand-authored test vectors are intentionally named as synthetic."""
    model = "synthetic-test-vectors"
    identity = "synthetic-test-vectors-v1"
    dimensions = 3

    def __init__(self, mapping, *, delay=0):
        self.mapping = mapping
        self.calls = []
        self.delay = delay
        self.failure = None

    async def embed(self, texts, *, timeout_budget_ms):
        self.calls.append(list(texts))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure:
            raise self.failure
        return [list(self.mapping[text]) for text in texts]


class HybridTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / "corpus"
        source.mkdir()
        (source / "solar.md").write_text("# Солнце\n\nПанели вырабатывают энергию днём.", encoding="utf-8")
        (source / "meters.md").write_text("# Учёт\n\nУчёт электричества показывает расход по помещениям.", encoding="utf-8")
        (source / "losses.md").write_text("# Потери\n\nУчёт электричества помогает обнаружить потери.", encoding="utf-8")
        self.corpus = ingest(source)
        self.by_title = {chunk.title: chunk for chunk in self.corpus.chunks}
        self.mapping = {
            document_embedding_text(self.by_title["Солнце"]): [1, 0, 0],
            document_embedding_text(self.by_title["Учёт"]): [0.8, 0.6, 0],
            document_embedding_text(self.by_title["Потери"]): [0, 0, 1],
            "генерация без топлива": [1, 0, 0],
            "учёт электричества": [0.8, 0.6, 0],
            "расход": [0.8, 0.6, 0],
        }
        self.cache = self.root / "private-cache"

    def provider(self, **kwargs):
        return CountingProvider(self.mapping, **kwargs)

    async def test_dense_only_match_has_zero_lexical_evidence(self):
        retriever = HybridRetriever(self.corpus, self.provider(), cache_dir=self.cache, candidate_pool=3)
        result = await retriever.search("генерация без топлива", limit=2)
        self.assertEqual(result.mode, "hybrid")
        self.assertEqual(result.hits[0].chunk.title, "Солнце")
        self.assertEqual(result.hits[0].overlap, 0)
        self.assertEqual(result.hits[0].coverage, 0)
        self.assertEqual(result.rankings[0].dense_rank, 1)
        self.assertIsNone(result.rankings[0].lexical_rank)
        self.assertAlmostEqual(result.rankings[0].fused_score, 1 / 61)
        self.assertTrue(all(value >= 0 for value in result.timings_ms.values()))

    async def test_rrf_uses_ranks_not_incomparable_score_magnitudes(self):
        result = await HybridRetriever(self.corpus, self.provider(), cache_dir=self.cache, candidate_pool=3).search("учёт электричества", limit=3)
        for item in result.rankings:
            expected = (1 / (60 + item.lexical_rank) if item.lexical_rank else 0) + (1 / (60 + item.dense_rank) if item.dense_rank else 0)
            self.assertAlmostEqual(item.fused_score, expected)
        self.assertEqual(result.hits[0].chunk.title, "Учёт")
        self.assertEqual(len({hit.chunk.chunk_id for hit in result.hits}), len(result.hits))

    async def test_context_is_sent_to_both_retrievers(self):
        query, context = "расход", "учёт электричества"
        self.mapping[query_embedding_text(query, context)] = [0.8, 0.6, 0]
        provider = self.provider()
        retriever = HybridRetriever(self.corpus, provider, cache_dir=self.cache)
        result = await retriever.search(query, context=context)
        self.assertIn(["расход\nКонтекст:\nучёт электричества"], provider.calls)
        self.assertEqual(result.lexical_hits, retriever.lexical.search(query, context=context, limit=5))

    async def test_cache_reused_after_process_state_is_cleared(self):
        first = self.provider()
        retriever = HybridRetriever(self.corpus, first, cache_dir=self.cache)
        initial = await retriever.prepare()
        self.assertEqual(len(first.calls), 1)
        # Simulate a fresh interpreter's empty memory cache, without changing disk.
        import app.ml.hybrid as hybrid
        hybrid._READY.clear()
        second = self.provider()
        restarted = HybridRetriever(self.corpus, second, cache_dir=self.cache)
        loaded = await restarted.prepare()
        self.assertEqual(len(second.calls), 0)
        self.assertEqual(initial, loaded)
        result = await restarted.search("расход")
        self.assertTrue(result.cache_hit)
        self.assertEqual(len(second.calls), 1)  # Query only, corpus reused.

    async def test_cache_omits_source_text_and_secrets(self):
        await HybridRetriever(self.corpus, self.provider(), cache_dir=self.cache).prepare()
        paths = list(self.cache.glob("embeddings-*.json"))
        self.assertEqual(len(paths), 1)
        content = paths[0].read_text()
        self.assertNotIn("Панели вырабатывают", content)
        self.assertNotIn("Authorization", content)
        self.assertEqual(paths[0].stat().st_mode & 0o777, 0o600)
        data = json.loads(content)
        self.assertEqual(data["dimensions"], 3)
        self.assertEqual(len(data["cache_key"]), 64)
        self.assertEqual(len(data["descriptor"]["documents"]), 3)

    async def test_tampered_cache_is_rebuilt(self):
        await HybridRetriever(self.corpus, self.provider(), cache_dir=self.cache).prepare()
        path = next(self.cache.glob("embeddings-*.json"))
        data = json.loads(path.read_text())
        data["vectors"][0] = [0, 0, 0]
        path.write_text(json.dumps(data))
        import app.ml.hybrid as hybrid
        hybrid._READY.clear()
        provider = self.provider()
        await HybridRetriever(self.corpus, provider, cache_dir=self.cache).prepare()
        self.assertEqual(len(provider.calls), 1)

    async def test_model_identity_invalidates_cache(self):
        await HybridRetriever(self.corpus, self.provider(), cache_dir=self.cache).prepare()
        other = self.provider()
        other.identity = "synthetic-test-vectors-v2"
        await HybridRetriever(self.corpus, other, cache_dir=self.cache).prepare()
        self.assertEqual(len(other.calls), 1)
        self.assertEqual(len(list(self.cache.glob("embeddings-*.json"))), 2)

    async def test_same_corpus_version_with_changed_content_cannot_reuse_cache(self):
        await HybridRetriever(self.corpus, self.provider(), cache_dir=self.cache).prepare()
        source = self.root / "corpus" / "solar.md"
        source.write_text("# Солнце\n\nИзменённый синтетический документ.", encoding="utf-8")
        changed = ingest(source.parent)
        forged_version = Corpus(self.corpus.version, changed.documents, changed.chunks)
        provider = self.provider()
        for chunk in changed.chunks:
            provider.mapping[document_embedding_text(chunk)] = [1, 0, 0]
        await HybridRetriever(forged_version, provider, cache_dir=self.cache).prepare()
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(list(self.cache.glob("embeddings-*.json"))), 2)

    async def test_concurrent_instances_build_corpus_once(self):
        provider = self.provider(delay=0.02)
        first = HybridRetriever(self.corpus, provider, cache_dir=self.cache)
        second = HybridRetriever(self.corpus, provider, cache_dir=self.cache)
        left, right = await asyncio.gather(first.prepare(), second.prepare())
        self.assertEqual(left, right)
        self.assertEqual(len(provider.calls), 1)

    async def test_in_memory_cache_writes_nothing_and_is_shared(self):
        provider = self.provider()
        first = HybridRetriever(self.corpus, provider, cache_dir=None)
        second = HybridRetriever(self.corpus, provider, cache_dir=None)
        await first.prepare()
        await second.prepare()
        self.assertEqual(len(provider.calls), 1)
        self.assertFalse(self.cache.exists())

    async def test_failed_prepare_is_latched_until_explicit_retry(self):
        provider = self.provider()
        provider.failure = EmbeddingUnavailable("embedding_http_error", "Synthetic failure")
        retriever = HybridRetriever(self.corpus, provider, cache_dir=self.cache)
        for _ in range(2):
            result = await retriever.search("учёт электричества")
            self.assertEqual(result.mode, "lexical_fallback")
            self.assertEqual(result.fallback_reason, "embedding_http_error")
        self.assertEqual(len(provider.calls), 1)
        provider.failure = None
        await retriever.prepare(retry_failed=True)
        self.assertEqual(len(provider.calls), 2)

    async def test_warmup_sanitizes_unexpected_provider_error(self):
        provider = self.provider()
        provider.failure = RuntimeError("private source text in provider exception")
        retriever = HybridRetriever(self.corpus, provider, cache_dir=self.cache)
        with self.assertRaises(EmbeddingUnavailable) as error:
            await retriever.prepare()
        self.assertEqual(error.exception.code, "invalid_embedding_state")
        self.assertNotIn("private source", str(error.exception))
        result = await retriever.search("расход")
        self.assertEqual(result.mode, "lexical_fallback")
        self.assertEqual(len(provider.calls), 1)

    async def test_dimension_mismatch_is_explicit_fallback(self):
        provider = self.provider()
        retriever = HybridRetriever(self.corpus, provider, cache_dir=self.cache)
        await retriever.prepare()
        provider.mapping["расход"] = [1, 0]
        result = await retriever.search("расход")
        self.assertEqual(result.mode, "lexical_fallback")
        self.assertEqual(result.fallback_reason, "invalid_embedding_response")
        self.assertFalse(retriever.dense_ready)

    async def test_timeout_falls_back_without_claiming_hybrid(self):
        provider = self.provider(delay=0.2)
        result = await HybridRetriever(self.corpus, provider, cache_dir=self.cache).search("расход", timeout_budget_ms=10)
        self.assertEqual(result.mode, "lexical_fallback")
        self.assertEqual(result.fallback_reason, "embedding_timeout")
        self.assertTrue(result.hits)

    async def test_cancellation_propagates_and_does_not_poison_later_build(self):
        provider = self.provider(delay=0.2)
        retriever = HybridRetriever(self.corpus, provider, cache_dir=self.cache)
        task = asyncio.create_task(retriever.prepare())
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        provider.delay = 0
        await retriever.prepare()
        self.assertTrue(retriever.dense_ready)

    async def test_cancelling_waiter_does_not_cancel_owner(self):
        provider = self.provider(delay=0.04)
        first = HybridRetriever(self.corpus, provider, cache_dir=self.cache)
        second = HybridRetriever(self.corpus, provider, cache_dir=self.cache)
        owner = asyncio.create_task(first.prepare())
        await asyncio.sleep(0.005)
        waiter = asyncio.create_task(second.prepare())
        await asyncio.sleep(0.005)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        await owner
        self.assertTrue(first.dense_ready)
        self.assertEqual(len(provider.calls), 1)

    async def test_no_provider_is_honest_lexical_mode(self):
        result = await HybridRetriever(self.corpus, None, cache_dir=None).search("расход")
        self.assertEqual(result.mode, "lexical")
        self.assertIsNone(result.fallback_reason)
        self.assertIsNone(result.embedding_model)

    async def test_precomputed_provider_exact_input_and_hash_file(self):
        provider = PrecomputedEmbeddingProvider(self.mapping, model="synthetic", identity="synthetic-1")
        result = await HybridRetriever(self.corpus, provider, cache_dir=self.cache).search("генерация без топлива")
        self.assertEqual(result.hits[0].chunk.title, "Солнце")
        file = self.root / "precomputed.json"
        file.write_text(json.dumps({"format_version": 1, "model": "synthetic", "identity": "synthetic-1", "dimensions": 3, "embeddings": [{"text_sha256": text_sha256("query"), "vector": [1, 0, 0]}]}))
        loaded = PrecomputedEmbeddingProvider.from_file(file)
        self.assertEqual(await loaded.embed(["query"], timeout_budget_ms=100), [[1.0, 0.0, 0.0]])
        with self.assertRaises(EmbeddingUnavailable):
            await loaded.embed(["unknown"], timeout_budget_ms=100)


class EmbeddingClientTests(unittest.IsolatedAsyncioTestCase):
    def config(self, **changes):
        values = dict(base_url="https://example.test/v1", model="synthetic-model", api_key="secret-test-key", allow_private_corpus=True)
        values.update(changes)
        return EmbeddingConfig(**values)

    async def test_batching_and_out_of_order_response_indices(self):
        requests = []
        async def handle(request):
            body = json.loads(request.content)
            requests.append(body)
            self.assertEqual(request.headers["authorization"], "Bearer secret-test-key")
            return httpx.Response(200, json={"data": [{"index": i, "embedding": [float(text), 1]} for i, text in reversed(list(enumerate(body["input"])))]})
        client = OpenAIEmbeddingClient(self.config(batch_size=2), transport=httpx.MockTransport(handle))
        vectors = await client.embed(["1", "2", "3"], timeout_budget_ms=1000)
        self.assertEqual(vectors, [[1, 1], [2, 1], [3, 1]])
        self.assertEqual([request["input"] for request in requests], [["1", "2"], ["3"]])
        self.assertEqual(client.dimensions, 2)

    async def test_malformed_responses_are_not_accepted(self):
        cases = [
            {"data": []},
            {"data": [{"index": 0, "embedding": [1, 0]}, {"index": 0, "embedding": [0, 1]}]},
            {"data": [{"index": True, "embedding": [1, 0]}, {"index": 1, "embedding": [0, 1]}]},
            {"data": [{"index": 0, "embedding": [1, 0]}, {"index": 2, "embedding": [0, 1]}]},
            {"data": [{"index": 0, "embedding": [1, 0]}, {"index": 1, "embedding": [0, 1, 2]}]},
            {"data": [{"index": 0, "embedding": [0, 0]}, {"index": 1, "embedding": [0, 1]}]},
            {"data": [{"index": 0, "embedding": [True, 1]}, {"index": 1, "embedding": [0, 1]}]},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                client = OpenAIEmbeddingClient(self.config(), transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)))
                with self.assertRaises(InvalidEmbedding):
                    await client.embed(["a", "b"], timeout_budget_ms=1000)
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                content = json.dumps({"data": [{"index": 0, "embedding": [value, 1]}]}).encode()
                client = OpenAIEmbeddingClient(self.config(), transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content)))
                with self.assertRaises(InvalidEmbedding):
                    await client.embed(["a"], timeout_budget_ms=1000)

    async def test_dimensions_cannot_change_across_batches(self):
        calls = 0
        def handle(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1] * (calls + 1)}]})
        client = OpenAIEmbeddingClient(self.config(batch_size=1), transport=httpx.MockTransport(handle))
        with self.assertRaises(InvalidEmbedding):
            await client.embed(["a", "b"], timeout_budget_ms=1000)

    async def test_transient_retry_but_no_auth_retry(self):
        calls = 0
        def handle(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(429, json={"private": "must never appear"})
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]})
        client = OpenAIEmbeddingClient(self.config(), transport=httpx.MockTransport(handle))
        self.assertEqual(await client.embed(["a"], timeout_budget_ms=1000), [[1, 0]])
        self.assertEqual(calls, 2)
        calls = 0
        def forbidden(request):
            nonlocal calls
            calls += 1
            return httpx.Response(401, content=b"private failure body")
        client = OpenAIEmbeddingClient(self.config(), transport=httpx.MockTransport(forbidden))
        with self.assertRaises(EmbeddingUnavailable) as error:
            await client.embed(["private input"], timeout_budget_ms=1000)
        self.assertEqual(calls, 1)
        self.assertNotIn("private", str(error.exception))
        self.assertNotIn("secret-test-key", repr(client.config))

    async def test_redirect_is_not_followed(self):
        calls = []
        def handle(request):
            calls.append(str(request.url))
            return httpx.Response(307, headers={"Location": "https://other.example/steal"})
        client = OpenAIEmbeddingClient(self.config(), transport=httpx.MockTransport(handle))
        with self.assertRaises(EmbeddingUnavailable):
            await client.embed(["private input"], timeout_budget_ms=1000)
        self.assertEqual(calls, ["https://example.test/v1/embeddings"])

    async def test_total_deadline_and_cancellation_stop_transport(self):
        stopped = asyncio.Event()
        async def handle(request):
            try:
                await asyncio.sleep(10)
            finally:
                stopped.set()
        client = OpenAIEmbeddingClient(self.config(), transport=httpx.MockTransport(handle))
        with self.assertRaises(EmbeddingUnavailable) as error:
            await client.embed(["a"], timeout_budget_ms=10)
        self.assertEqual(error.exception.code, "embedding_timeout")
        self.assertTrue(stopped.is_set())
        stopped.clear()
        task = asyncio.create_task(client.embed(["a"], timeout_budget_ms=1000))
        await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())

    async def test_official_env_defaults_and_identity(self):
        with patch.dict(os.environ, {"OPENAI_BASE_URL": "http://organizer-proxy/v1", "LLM_API_KEY": "llm-key", "EMBEDDER_API_KEY": "embedding-key"}, clear=True):
            config = EmbeddingConfig.from_env(platform=True)
        self.assertEqual(config.model, "baai/bge-m3")
        self.assertEqual(config.api_key, "embedding-key")
        self.assertTrue(config.allow_internal_http)
        rotated = self.config(api_key="rotated")
        self.assertEqual(self.config().identity, rotated.identity)
        self.assertNotEqual(self.config().identity, self.config(model_revision="next").identity)

    async def test_explicit_llm_config_controls_endpoint_credentials_and_consent(self):
        from app.ml.llm_client import LLMConfig
        llm = LLMConfig(base_url="https://approved.example/v1", model="test-qwen", api_key="approved-key", allow_private_corpus=True)
        with patch.dict(os.environ, {"LLM_BASE_URL": "https://wrong.example/v1", "LLM_API_KEY": "wrong-key", "EMB_MODEL": "configured-embedding"}, clear=True):
            config = EmbeddingConfig.from_env(llm_config=llm)
            self.assertEqual(config.base_url, llm.base_url)
            self.assertEqual(config.api_key, llm.api_key)
            self.assertTrue(config.allow_private_corpus)
            self.assertEqual(config.model, "configured-embedding")
            os.environ["EMBEDDER_API_KEY"] = "explicit-embedding-key"
            overridden = EmbeddingConfig.from_env(llm_config=llm)
            self.assertEqual(overridden.api_key, "explicit-embedding-key")
            self.assertEqual(overridden.base_url, llm.base_url)

    async def test_compressed_response_is_not_accepted(self):
        def handle(request):
            self.assertEqual(request.headers["accept-encoding"], "identity")
            return httpx.Response(200, headers={"Content-Encoding": "br"}, content=b"")
        client = OpenAIEmbeddingClient(self.config(), transport=httpx.MockTransport(handle))
        with self.assertRaises(EmbeddingUnavailable):
            await client.embed(["a"], timeout_budget_ms=1000)

    async def test_permission_and_vector_validation(self):
        with self.assertRaises(ValueError):
            EmbeddingConfig(base_url="https://example.test/v1", model="x", api_key="y")
        with self.assertRaises(ValueError):
            self.config(base_url="http://not-local.test/v1")
        for vector in ([], [0, 0], [False, 1], [float("inf"), 1], ["1", 0]):
            with self.assertRaises(InvalidEmbedding):
                normalized_vector(vector)
        self.assertEqual(normalized_vector([3, 4]), (0.6, 0.8))
