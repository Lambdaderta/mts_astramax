"""Retrieval configuration and consultation integration with synthetic vectors."""
import asyncio

import pytest

from app.ml.core import LocalConsultationCore, build_core
from app.ml.corpus import ingest
from app.ml.embeddings import EmbeddingUnavailable, PrecomputedEmbeddingProvider
from app.ml.hybrid import document_embedding_text
from app.ml.protocol import ConsultationRequest, ConsultationResult
from app.ml.llm_client import LLMConfig
from app.ml.retrieval_config import RetrievalConfig


def test_config_rejects_nonfinite_or_inconsistent_limits(monkeypatch):
    for key, value in [("TOP_K", "0"), ("TOP_K", "31"), ("RRF_K", "nan"),
                       ("DENSE_WEIGHT", "inf"), ("RETRIEVAL_MODE", "typo")]:
        with monkeypatch.context() as isolated:
            isolated.setenv(key, value)
            with pytest.raises(ValueError):
                RetrievalConfig.from_env(enabled=True)
    with pytest.raises(ValueError):
        RetrievalConfig(top_k=6, candidate_pool=4)


def test_offline_mode_does_not_enable_remote_retrieval(monkeypatch):
    monkeypatch.setenv("RETRIEVAL_MODE", "hybrid")
    assert RetrievalConfig.from_env(enabled=False).mode == "bm25"


def test_explicit_llm_config_is_shared_with_embedding_client(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://wrong-environment.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "wrong-environment-key")
    monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
    llm = LLMConfig("http://localhost/v1", "synthetic-generation", "synthetic-shared-key", allow_private_corpus=True)
    core = build_core(tmp_path, llm_config=llm)
    # Missing corpus is fine; construction must use the supplied transport config.
    assert not core.ready
    from app.ml.embeddings import EmbeddingConfig
    config = EmbeddingConfig.from_env(llm_config=llm)
    assert config.base_url == llm.base_url and config.api_key == llm.api_key
    assert config.allow_private_corpus


def test_dense_only_relevant_passage_reaches_generator_and_trace(tmp_path):
    source = tmp_path / "corpus"
    source.mkdir()
    (source / "test.md").write_text("# Учебный прибор\n\nДатчик замечает протечку и подает сигнал оператору.", encoding="utf-8")
    corpus = ingest(source)
    index = tmp_path / "index"
    corpus.save(index)
    query = "Как обнаружить затопление помещения?"
    provider = PrecomputedEmbeddingProvider(
        {**{document_embedding_text(chunk): [1.0, 0.0] for chunk in corpus.chunks}, query: [1.0, 0.0]},
        model="synthetic", identity="synthetic-retrieval-integration-v1",
    )
    core = LocalConsultationCore(index, retrieval_config=RetrievalConfig(mode="hybrid"), embedding_provider=provider)
    captured = []

    class Selector:
        class config:
            generation_mode = "grounded"

        async def generate(self, query, request, candidates, remaining):
            captured.extend(candidates)
            return ConsultationResult(kind="insufficient_knowledge", answer_text="Нет сведений.", reason_code="missing_evidence")

    core.selector = Selector()
    request = ConsultationRequest(request_id="r", session_id="s", user_message_id="u", latest_user_text=query,
                                  corpus_version=corpus.version, timeout_budget_ms=1000)
    result = asyncio.run(core.consult(request))
    assert captured and captured[0].chunk.chunk_id == corpus.chunks[0].chunk_id
    assert captured[0].overlap == 0  # Semantic recall does not fabricate lexical confidence.
    assert result.model_metadata["retrieval_mode"] == "hybrid"
    assert result.model_metadata["embedding_model"] == "synthetic"
    assert "retrieval_embedding_query" in result.timings_ms


def test_failed_corpus_build_is_not_repeated_for_each_turn(tmp_path):
    source = tmp_path / "corpus"
    source.mkdir()
    (source / "test.md").write_text("# Учебный прибор\n\nДатчик замечает протечку и подает сигнал оператору.", encoding="utf-8")
    corpus = ingest(source)
    index = tmp_path / "index"
    corpus.save(index)

    class BrokenProvider:
        identity = "synthetic-failed-prepare-v1"
        model = "synthetic"
        dimensions = None
        calls = 0

        async def embed(self, texts, *, timeout_budget_ms):
            self.calls += 1
            raise EmbeddingUnavailable("embedding_http_error", "Synthetic unavailable provider")

    provider = BrokenProvider()
    core = LocalConsultationCore(index, retrieval_config=RetrievalConfig(mode="hybrid"), embedding_provider=provider)

    async def exercise():
        await core.prepare_retrieval()
        request = ConsultationRequest(request_id="r", session_id="s", user_message_id="u", latest_user_text="Что замечает датчик?",
                                      corpus_version=corpus.version, timeout_budget_ms=1000)
        return await core.consult(request), await core.consult(request)

    results = asyncio.run(exercise())
    assert provider.calls == 1
    assert all(result.model_metadata["retrieval_mode"] == "lexical_fallback" for result in results)
    assert all(result.model_metadata["retrieval_fallback"] == "embedding_http_error" for result in results)
