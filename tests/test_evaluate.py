"""Retrieval benchmark checks use synthetic passages, never private source data."""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import subprocess
import sys

import pytest

from app.ml.corpus import Corpus, ingest
from app.ml.embeddings import EmbeddingUnavailable
from app.ml.hybrid import document_embedding_text
from scripts.evaluate import evaluate, evaluate_ranking, load_benchmark, summarize


@pytest.fixture
def benchmark_corpus(tmp_path: Path) -> Corpus:
    source = tmp_path / "source"
    source.mkdir()
    (source / "synthetic.md").write_text(
        "# Alpha\n\nAlpha is the main evidence for the synthetic question.\n\n"
        "# Beta\n\nBeta is an alternative source of the same synthetic evidence.\n\n"
        "# Gamma\n\nGamma records a second independently required condition.\n\n"
        "# Delta\n\nDelta supplies related background with no direct answer.\n\n"
        "# Epsilon\n\nEpsilon is unrelated to the synthetic question.\n",
        encoding="utf-8",
    )
    return ingest(source)


def _chunk_ids(corpus: Corpus) -> dict[str, str]:
    return {chunk.title: chunk.chunk_id for chunk in corpus.chunks}


def _case(corpus: Corpus, *, case_id: str = "positive") -> dict:
    ids = _chunk_ids(corpus)
    return {
        "id": case_id,
        "split": "synthetic_dev",
        "question": "What evidence does Alpha provide?",
        "answerability": "answerable",
        "qrels": {ids["Alpha"]: 2, ids["Beta"]: 2, ids["Gamma"]: 2, ids["Delta"]: 1},
        "relevant_chunk_ids": [ids["Alpha"], ids["Beta"], ids["Gamma"]],
        "evidence_groups": [
            {"id": "main", "alternatives": [ids["Alpha"], ids["Beta"]], "description": "Main evidence", "required": True},
            {"id": "condition", "alternatives": [ids["Gamma"]], "description": "Required condition", "required": True},
            {"id": "background", "alternatives": [ids["Delta"]], "description": "Optional background", "required": False},
        ],
        "rationale": "Synthetic metric fixture.",
        "source_checks": [],
    }


def _benchmark(corpus: Corpus, cases: list[dict] | None = None) -> dict:
    return {
        "schema_version": 1,
        "benchmark_id": "synthetic-retrieval-v1",
        "corpus_version": corpus.version,
        "label_status": "frozen",
        "audit_status": "synthetic",
        "relevance_threshold": 2,
        "cases": cases if cases is not None else [_case(corpus)],
    }


def _write_benchmark(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "qrels.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_chunk_metrics_use_threshold_two_and_graded_discount(benchmark_corpus):
    ids = _chunk_ids(benchmark_corpus)
    result = evaluate_ranking(
        [ids["Delta"], ids["Alpha"], ids["Gamma"], ids["Beta"]],
        _case(benchmark_corpus), k=3,
    )
    # Grade-one background affects nDCG but cannot count as direct evidence.
    assert result["recall_at_k"] == pytest.approx(2 / 3)
    assert result["mrr_at_k"] == pytest.approx(1 / 2)
    dcg = 1 + 3 / math.log2(3) + 3 / math.log2(4)
    ideal = 3 + 3 / math.log2(3) + 3 / math.log2(4)
    assert result["ndcg_at_k"] == pytest.approx(dcg / ideal)


def test_evidence_groups_accept_alternatives_but_require_each_group(benchmark_corpus):
    ids = _chunk_ids(benchmark_corpus)
    case = _case(benchmark_corpus)
    duplicated_evidence = evaluate_ranking([ids["Beta"], ids["Alpha"]], case, k=2)
    assert duplicated_evidence["recall_at_k"] == pytest.approx(2 / 3)
    assert duplicated_evidence["group_recall_at_k"] == pytest.approx(1 / 2)
    assert duplicated_evidence["full_group_coverage_at_k"] == 0

    complete_evidence = evaluate_ranking([ids["Alpha"], ids["Gamma"]], case, k=2)
    assert complete_evidence["recall_at_k"] == pytest.approx(2 / 3)
    assert complete_evidence["group_recall_at_k"] == 1
    assert complete_evidence["full_group_coverage_at_k"] == 1


def test_empty_retrieval_scores_zero_for_positive_case(benchmark_corpus):
    result = evaluate_ranking([], _case(benchmark_corpus), k=5)
    assert all(result[key] == 0 for key in (
        "recall_at_k", "mrr_at_k", "ndcg_at_k", "group_recall_at_k", "full_group_coverage_at_k",
    ))


def test_guardrail_cases_are_excluded_from_positive_denominators(benchmark_corpus):
    ids = _chunk_ids(benchmark_corpus)
    positive = evaluate_ranking([ids["Alpha"], ids["Gamma"]], _case(benchmark_corpus), k=2)
    guardrail = _case(benchmark_corpus, case_id="guardrail")
    guardrail.update(answerability="unanswerable", qrels={ids["Delta"]: 1}, relevant_chunk_ids=[], evidence_groups=[])
    no_positive = evaluate_ranking([ids["Delta"]], guardrail, k=2)
    metric_keys = ("recall_at_k", "mrr_at_k", "ndcg_at_k", "group_recall_at_k", "full_group_coverage_at_k")
    assert all(no_positive[key] is None for key in metric_keys)

    summary = summarize([
        {"metrics": positive, "latency_ms": 1.0, "mode": "bm25"},
        {"metrics": no_positive, "latency_ms": 2.0, "mode": "bm25"},
    ])
    assert summary["total_cases"] == 2
    assert summary["positive_cases"] == 1
    assert summary["no_positive_cases"] == 1
    for key in metric_keys:
        assert summary["mean_" + key] == pytest.approx(positive[key])


def test_load_benchmark_accepts_matching_corpus(tmp_path, benchmark_corpus):
    path = _write_benchmark(tmp_path, _benchmark(benchmark_corpus))
    result = load_benchmark(path, benchmark_corpus)
    assert result["corpus_version"] == benchmark_corpus.version
    assert result["cases"][0]["id"] == "positive"


def test_load_benchmark_rejects_wrong_corpus(tmp_path, benchmark_corpus):
    payload = _benchmark(benchmark_corpus)
    payload["corpus_version"] = "different-frozen-corpus"
    path = _write_benchmark(tmp_path, payload)
    with pytest.raises(ValueError, match="(?i)corpus"):
        load_benchmark(path, benchmark_corpus)


@pytest.mark.parametrize("reference", ["qrels", "relevant_chunk_ids", "evidence_groups"])
def test_load_benchmark_rejects_unknown_chunk_references(tmp_path, benchmark_corpus, reference):
    payload = _benchmark(benchmark_corpus)
    case = payload["cases"][0]
    if reference == "qrels":
        case["qrels"]["chunk_missing"] = 2
    elif reference == "relevant_chunk_ids":
        case["relevant_chunk_ids"].append("chunk_missing")
    else:
        case["evidence_groups"][0]["alternatives"].append("chunk_missing")
    path = _write_benchmark(tmp_path, payload)
    with pytest.raises(ValueError):
        load_benchmark(path, benchmark_corpus)


class _UnavailableProvider:
    identity = "synthetic-evaluation-failure-v1"
    model = "synthetic-unavailable"
    dimensions = 2

    async def embed(self, texts, *, timeout_budget_ms):
        raise EmbeddingUnavailable("embedding_unavailable", "Synthetic unavailable provider")


def test_failed_embeddings_cannot_be_reported_as_hybrid(benchmark_corpus):
    with pytest.raises(ValueError, match="(?i)(fallback|embedding)"):
        asyncio.run(evaluate(
            benchmark_corpus, _benchmark(benchmark_corpus), retriever="hybrid", provider=_UnavailableProvider(),
        ))


@pytest.mark.parametrize("allow_fallback", [False, True])
def test_cli_missing_query_vectors_never_count_as_hybrid(tmp_path, benchmark_corpus, allow_fallback):
    index_dir = tmp_path / "index"
    benchmark_corpus.save(index_dir)
    qrels = _write_benchmark(tmp_path, _benchmark(benchmark_corpus))
    vectors = tmp_path / "vectors.json"
    vectors.write_text(json.dumps({
        "format_version": 1,
        "model": "synthetic-missing-query",
        "identity": "synthetic-missing-query-v1",
        "dimensions": 2,
        "embeddings": [{"text": document_embedding_text(chunk), "vector": [1.0, 0.0]} for chunk in benchmark_corpus.chunks],
    }), encoding="utf-8")
    output = tmp_path / "report.json"
    command = [
        sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "evaluate.py"),
        "--index-dir", str(index_dir), "--qrels", str(qrels), "--vectors", str(vectors),
        "--retriever", "both", "--output", str(output),
    ]
    if allow_fallback:
        command.append("--allow-lexical-fallback")
    process = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    if not allow_fallback:
        assert process.returncode != 0
        assert not output.exists()
        assert "refusing a misleading hybrid comparison" in process.stderr
        return

    assert process.returncode == 0, process.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["summary"]["hybrid"]["mode_counts"] == {"lexical_fallback": 1}
    assert report["summary"]["dense"]["mode_counts"] == {"unavailable": 1}
    assert report["summary"]["bm25"]["mode_counts"] == {"bm25": 1}
    dense_row = next(row for row in report["cases"] if row["retriever"] == "dense")
    assert dense_row["ranked_chunk_ids"] == []
    assert dense_row["fallback_reason"] == "precomputed_embedding_missing"
