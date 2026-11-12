"""Measure lexical Recall@k from local JSONL queries, without external APIs.

Each line: {"query":"...", "relevant_chunk_ids":["chunk_..."]}.
Use organizer-provided answers to label real evaluation data after ingestion.
"""
from __future__ import annotations
import argparse
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.ml.corpus import Corpus
from app.ml.retriever import BM25Retriever


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index-dir", required=True, type=Path)
    parser.add_argument("--questions", required=True, type=Path)
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()
    if args.k <= 0:
        parser.error("--k must be positive")
    corpus = Corpus.load(args.index_dir)
    retriever = BM25Retriever(corpus)
    known_ids = set(corpus.chunks_by_id)
    recalls, latencies = [], []
    for line in args.questions.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        relevant = set(item["relevant_chunk_ids"])
        if not relevant or not relevant.issubset(known_ids):
            raise ValueError("Every query must reference existing, nonempty relevant_chunk_ids")
        started = time.perf_counter()
        found = {hit.chunk.chunk_id for hit in retriever.search(item["query"], limit=args.k)}
        latencies.append((time.perf_counter() - started) * 1000)
        recalls.append(len(relevant & found) / len(relevant))
    if not recalls:
        raise ValueError("No evaluation queries supplied")
    ordered = sorted(latencies)
    print(json.dumps({
        "corpus_version": corpus.version, "queries": len(recalls), "k": args.k,
        "mean_recall_at_k": statistics.mean(recalls),
        "retrieval_median_ms": statistics.median(latencies),
        "retrieval_p95_ms": ordered[max(0, int(len(ordered) * 0.95 + 0.999999) - 1)],
        "note": "Retrieval only; this does not measure answer correctness or end-to-end latency.",
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
