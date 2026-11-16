"""Exact environment-template compatibility cases from the organizer sample."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.ml.embeddings import EmbeddingConfig
from app.ml.environment import env_value
from app.ml.llm_client import DEFAULT_MODEL, LLMConfig
from app.ml.retrieval_config import RetrievalConfig


class EnvironmentTests(unittest.TestCase):
    def test_empty_whitespace_and_literal_name_allow_aliases(self):
        with patch.dict(os.environ, {"PRIMARY": " PRIMARY ", "SECONDARY": "   ", "THIRD": "  useful-value \n"}, clear=True):
            self.assertEqual(env_value("PRIMARY", "SECONDARY", "THIRD", default="fallback"), "useful-value")
            self.assertEqual(env_value("PRIMARY", "SECONDARY", "MISSING", default="fallback"), "fallback")
        with patch.dict(os.environ, {"PRIMARY": "actual-model/PRIMARY"}, clear=True):
            self.assertEqual(env_value("PRIMARY"), "actual-model/PRIMARY")

    def test_organizer_template_values_no_longer_break_startup_configs(self):
        names = [
            "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "GEN_MODEL", "OPENAI_MODEL",
            "TEMPERATURE", "MAX_TOKENS", "TOP_K", "EMB_MODEL", "EMBEDDER_API_KEY",
            "ML_TIMEOUT_SECONDS", "LLM_MAX_RETRIES", "LLM_GENERATION_MODE",
            "RETRIEVAL_MODE", "RETRIEVAL_CANDIDATES", "RRF_K", "LEXICAL_WEIGHT",
            "DENSE_WEIGHT", "RETRIEVAL_TIMEOUT_SECONDS", "EMBEDDING_CACHE_DIR",
            "EMBEDDING_DIMENSIONS", "EMBEDDING_BATCH_SIZE", "EMBEDDING_TIMEOUT_SECONDS",
            "EMBEDDING_MAX_RETRIES", "EMBEDDING_MODEL_REVISION",
        ]
        values = {name: name for name in names}
        values.update(BASE_URL="  http://organizer-proxy/v1  ", OPENAI_API_KEY="  dummy  ")
        with patch.dict(os.environ, values, clear=True):
            llm = LLMConfig.from_env(platform=True)
            embedding = EmbeddingConfig.from_env(platform=True, llm_config=llm)
            retrieval = RetrievalConfig.from_env(enabled=True)
        self.assertEqual(llm.base_url, "http://organizer-proxy/v1")
        self.assertEqual(llm.api_key, "dummy")
        self.assertEqual(llm.model, DEFAULT_MODEL)
        self.assertEqual(llm.temperature, 0)
        self.assertEqual(llm.max_tokens, 2048)
        self.assertEqual(embedding.api_key, "dummy")
        self.assertEqual(embedding.model, "baai/bge-m3")
        self.assertIsNone(embedding.expected_dimensions)
        self.assertEqual(retrieval.top_k, 6)
        self.assertEqual(retrieval.candidate_pool, 20)
        self.assertEqual(retrieval.mode, "hybrid")
        self.assertIsNone(retrieval.cache_dir)

    def test_model_alias_priority_matches_organizer_sample(self):
        values = {"LLM_BASE_URL": "https://example.test/v1", "LLM_API_KEY": "dummy", "LLM_MODEL": " LLM_MODEL ", "GEN_MODEL": "generation-model", "OPENAI_MODEL": "other-model"}
        with patch.dict(os.environ, values, clear=True):
            self.assertEqual(LLMConfig.from_env(platform=True).model, "generation-model")
            os.environ["GEN_MODEL"] = " GEN_MODEL "
            self.assertEqual(LLMConfig.from_env(platform=True).model, "other-model")
            os.environ["LLM_MODEL"] = "primary-model"
            self.assertEqual(LLMConfig.from_env(platform=True).model, "primary-model")

    def test_base_url_alias_priority_matches_sample(self):
        values = {"LLM_BASE_URL": "LLM_BASE_URL", "BASE_URL": "https://middle.test/v1", "OPENAI_BASE_URL": "https://last.test/v1", "LLM_API_KEY": "dummy"}
        with patch.dict(os.environ, values, clear=True):
            self.assertEqual(LLMConfig.from_env(platform=True).base_url, "https://middle.test/v1")
            self.assertEqual(EmbeddingConfig.from_env(platform=True).base_url, "https://middle.test/v1")
            os.environ["LLM_BASE_URL"] = " https://first.test/v1 "
            self.assertEqual(LLMConfig.from_env(platform=True).base_url, "https://first.test/v1")

    def test_embedding_placeholder_cannot_override_explicit_llm_credentials(self):
        llm = LLMConfig(base_url="https://approved.test/v1", model="qwen-test", api_key="approved-key", allow_private_corpus=True)
        with patch.dict(os.environ, {"LLM_BASE_URL": "https://other.test/v1", "EMBEDDER_API_KEY": " EMBEDDER_API_KEY "}, clear=True):
            config = EmbeddingConfig.from_env(llm_config=llm)
            self.assertEqual(config.base_url, llm.base_url)
            self.assertEqual(config.api_key, llm.api_key)
            os.environ["EMBEDDER_API_KEY"] = " embedding-key "
            self.assertEqual(EmbeddingConfig.from_env(llm_config=llm).api_key, "embedding-key")

    def test_sample_top_k_range_and_default_pool(self):
        for top_k, expected_pool in [(1, 20), (13, 20), (20, 20), (30, 30)]:
            with self.subTest(top_k=top_k), patch.dict(os.environ, {"TOP_K": str(top_k)}, clear=True):
                config = RetrievalConfig.from_env(enabled=True)
                self.assertEqual(config.top_k, top_k)
                self.assertEqual(config.candidate_pool, expected_pool)
        with patch.dict(os.environ, {"TOP_K": "31"}, clear=True):
            with self.assertRaises(ValueError):
                RetrievalConfig.from_env(enabled=True)
        with patch.dict(os.environ, {"TOP_K": "30", "RETRIEVAL_CANDIDATES": "20"}, clear=True):
            with self.assertRaises(ValueError):
                RetrievalConfig.from_env(enabled=True)

    def test_real_malformed_numbers_are_still_configuration_errors(self):
        with patch.dict(os.environ, {"LLM_BASE_URL": "https://example.test/v1", "LLM_API_KEY": "dummy", "TEMPERATURE": "not-a-number"}, clear=True):
            with self.assertRaises(ValueError):
                LLMConfig.from_env(platform=True)
        with patch.dict(os.environ, {"TOP_K": "not-a-number"}, clear=True):
            with self.assertRaises(ValueError):
                RetrievalConfig.from_env(enabled=True)

    def test_platform_and_web_timeout_defaults_and_explicit_override(self):
        values = {"LLM_BASE_URL": "https://example.test/v1", "LLM_API_KEY": "dummy", "ALLOW_PRIVATE_CORPUS_TO_LLM": "true"}
        with patch.dict(os.environ, values, clear=True):
            self.assertEqual(LLMConfig.from_env(platform=True).timeout_seconds, 90)
            self.assertEqual(LLMConfig.from_env(platform=False).timeout_seconds, 10)
            os.environ["ML_TIMEOUT_SECONDS"] = " ML_TIMEOUT_SECONDS "
            self.assertEqual(LLMConfig.from_env(platform=True).timeout_seconds, 90)
            os.environ["ML_TIMEOUT_SECONDS"] = " 17 "
            self.assertEqual(LLMConfig.from_env(platform=True).timeout_seconds, 17)
            self.assertEqual(LLMConfig.from_env(platform=False).timeout_seconds, 17)

    def test_whitespace_private_data_flags_do_not_bypass_permission(self):
        with patch.dict(os.environ, {"LLM_BASE_URL": "https://example.test/v1", "LLM_API_KEY": "dummy", "ALLOW_PRIVATE_CORPUS_TO_LLM": "ALLOW_PRIVATE_CORPUS_TO_LLM"}, clear=True):
            with self.assertRaises(ValueError):
                LLMConfig.from_env()
            os.environ["ALLOW_PRIVATE_CORPUS_TO_LLM"] = " true "
            self.assertTrue(LLMConfig.from_env().allow_private_corpus)


class ProviderCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_200_envelopes_become_invalid_evidence(self):
        import httpx
        from app.ml.llm_client import _read_model_response
        from app.ml.protocol import InvalidEvidence
        payloads = [
            {"choices": [None]}, {"choices": [[]]}, {"choices": []},
            {"choices": "wrong-type"}, [], {"choices": [{"message": None}]},
            {"choices": [{"message": {"content": None}}]},
            {"choices": [{"finish_reason": [], "message": {"content": "{}"}}]},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(InvalidEvidence):
                    await _read_model_response(httpx.Response(200, json=payload))

    async def test_valid_gzip_and_deflate_chat_responses_are_decoded(self):
        import gzip
        import json
        import zlib
        import httpx
        from app.ml.llm_client import _read_model_response
        expected = {"kind": "clarify", "answer_text": "generated_clarify", "chunk_ids": []}
        body = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(expected)}}]}).encode()
        for encoding, compress in [("gzip", gzip.compress), ("deflate", zlib.compress)]:
            with self.subTest(encoding=encoding):
                response = httpx.Response(200, headers={"Content-Encoding": encoding}, content=compress(body))
                self.assertEqual(await _read_model_response(response), expected)

    async def test_compressed_chat_still_checks_decoded_size_and_json(self):
        import gzip
        import json
        import httpx
        from app.ml.llm_client import MAX_RESPONSE_BYTES, _read_model_response
        from app.ml.protocol import InvalidEvidence
        # Highly compressible data must not bypass the decoded-byte limit.
        response = httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=gzip.compress(b" " * (MAX_RESPONSE_BYTES + 1)))
        with self.assertRaises(InvalidEvidence):
            await _read_model_response(response)
        malformed = json.dumps({"choices": [{"message": {"content": "prefix {} suffix"}}]}).encode()
        response = httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=gzip.compress(malformed))
        with self.assertRaises(InvalidEvidence):
            await _read_model_response(response)

    async def test_valid_compressed_embedding_responses_are_decoded(self):
        import gzip
        import json
        import zlib
        import httpx
        from app.ml.embeddings import OpenAIEmbeddingClient
        config = EmbeddingConfig(base_url="https://example.test/v1", model="synthetic", api_key="dummy", allow_private_corpus=True)
        body = json.dumps({"data": [{"index": 0, "embedding": [1, 0]}]}).encode()
        for encoding, compress in [("gzip", gzip.compress), ("deflate", zlib.compress)]:
            with self.subTest(encoding=encoding):
                transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={"Content-Encoding": encoding}, content=compress(body)))
                client = OpenAIEmbeddingClient(config, transport=transport)
                self.assertEqual(await client.embed(["synthetic input"], timeout_budget_ms=1000), [[1.0, 0.0]])

    async def test_compressed_embeddings_still_check_decoded_size(self):
        import gzip
        import httpx
        from app.ml.embeddings import InvalidEmbedding, OpenAIEmbeddingClient
        config = EmbeddingConfig(base_url="https://example.test/v1", model="synthetic", api_key="dummy", allow_private_corpus=True)
        transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=gzip.compress(b" " * 129)))
        client = OpenAIEmbeddingClient(config, transport=transport)
        with patch("app.ml.embeddings.MAX_RESPONSE_BYTES", 128):
            with self.assertRaises(InvalidEmbedding):
                await client.embed(["synthetic input"], timeout_budget_ms=1000)
