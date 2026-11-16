"""Compare source-backed retrieval on versioned private judgments.

This measures retrieval, not answer correctness or the organizer's judge score.
Unjudged chunks score zero; development labels may miss valid evidence.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.ml.core import _query_parts
from app.ml.corpus import Corpus
from app.ml.embeddings import EmbeddingConfig, OpenAIEmbeddingClient, PrecomputedEmbeddingProvider
from app.ml.hybrid import HybridRetriever, document_embedding_text, query_embedding_text
from app.ml.retriever import BM25Retriever
from solution import Question, consultation_request

METRICS = ('recall_at_k', 'mrr_at_k', 'ndcg_at_k', 'group_recall_at_k', 'full_group_coverage_at_k')


def load_benchmark(path: Path, corpus: Corpus, *, legacy: bool = False) -> dict:
    raw = path.read_bytes()
    if legacy:
        entries = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
        benchmark = {'schema_version': 1, 'benchmark_id': 'legacy-jsonl', 'corpus_version': corpus.version,
                     'relevance_threshold': 2, 'label_status': 'legacy_unversioned', 'cases': []}
        for number, entry in enumerate(entries, 1):
            relevant = entry['relevant_chunk_ids']
            if not relevant:
                raise ValueError('Legacy queries require nonempty relevant_chunk_ids')
            benchmark['cases'].append({'id': str(number), 'split': 'legacy', 'question': entry['query'],
                'qrels': dict.fromkeys(relevant, 2), 'relevant_chunk_ids': relevant, 'evidence_groups': []})
    else:
        benchmark = json.loads(raw)
    if benchmark.get('schema_version') != 1 or benchmark.get('corpus_version') != corpus.version:
        raise ValueError('Benchmark schema or corpus_version does not match the loaded index')
    threshold = benchmark.get('relevance_threshold', 2)
    if type(threshold) is not int or not 1 <= threshold <= 2:
        raise ValueError('Relevance threshold must be 1 or 2')
    cases = benchmark.get('cases')
    if not isinstance(cases, list) or not cases:
        raise ValueError('No evaluation cases supplied')
    seen = set()
    known = corpus.chunks_by_id
    for case in cases:
        identifier = case.get('id')
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError('Case IDs must be distinct nonempty strings')
        seen.add(identifier)
        if not isinstance(case.get('question'), str) or not case['question'].strip():
            raise ValueError('Every case needs a nonempty question')
        qrels = case.get('qrels')
        if not isinstance(qrels, dict) or any(cid not in known or type(grade) is not int or not 0 <= grade <= 2 for cid, grade in qrels.items()):
            raise ValueError('Qrels must reference known chunk IDs with grades 0..2')
        relevant = {cid for cid, grade in qrels.items() if grade >= threshold}
        if 'relevant_chunk_ids' in case and set(case['relevant_chunk_ids']) != relevant:
            raise ValueError('relevant_chunk_ids disagree with graded judgments')
        for group in case.get('evidence_groups', []):
            alternatives = group.get('alternatives')
            supported = relevant if group.get('required', True) else {cid for cid, grade in qrels.items() if grade > 0}
            if not isinstance(alternatives, list) or not alternatives or not set(alternatives).issubset(supported):
                raise ValueError('Evidence groups must contain positively judged alternatives')
            if type(group.get('required', True)) is not bool:
                raise ValueError('Evidence group required must be boolean')
        for check in case.get('source_checks', []):
            chunk = known.get(check.get('chunk_id'))
            if chunk is None or chunk.checksum != check.get('chunk_checksum'):
                raise ValueError('A source checksum does not match the current chunk')
    return {**benchmark, 'benchmark_sha256': hashlib.sha256(raw).hexdigest()}


def evaluate_ranking(ranked_ids: list[str], case: dict, k: int, threshold: int = 2) -> dict:
    if k <= 0:
        raise ValueError('k must be positive')
    ids = ranked_ids[:k]
    if len(ids) != len(set(ids)):
        raise ValueError('A ranking must not contain duplicate chunks')
    qrels = case['qrels']
    relevant = {cid for cid, grade in qrels.items() if grade >= threshold}
    if not relevant:
        return dict.fromkeys(METRICS, None)
    found = set(ids)
    dcg = sum((2 ** qrels.get(cid, 0) - 1) / math.log2(rank + 1) for rank, cid in enumerate(ids, 1))
    ideal = sum((2 ** grade - 1) / math.log2(rank + 1) for rank, grade in enumerate(sorted(qrels.values(), reverse=True)[:k], 1))
    groups = [group for group in case.get('evidence_groups', []) if group.get('required', True)]
    covered = sum(bool(found & set(group['alternatives'])) for group in groups)
    return {
        'recall_at_k': len(found & relevant) / len(relevant),
        'mrr_at_k': next((1 / rank for rank, cid in enumerate(ids, 1) if cid in relevant), 0.0),
        'ndcg_at_k': dcg / ideal,
        'group_recall_at_k': covered / len(groups) if groups else None,
        'full_group_coverage_at_k': float(covered == len(groups)) if groups else None,
    }


def summarize(rows: list[dict]) -> dict:
    result = {'total_cases': len(rows), 'positive_cases': sum(row['metrics']['recall_at_k'] is not None for row in rows)}
    result['no_positive_cases'] = len(rows) - result['positive_cases']
    for metric in METRICS:
        values = [row['metrics'][metric] for row in rows if row['metrics'][metric] is not None]
        result['mean_' + metric] = statistics.mean(values) if values else None
        result[metric + '_cases'] = len(values)
    latencies = sorted(row['latency_ms'] for row in rows)
    result['median_ms'] = statistics.median(latencies) if latencies else None
    result['p95_ms'] = latencies[math.ceil(0.95 * len(latencies)) - 1] if latencies else None
    result['mode_counts'] = dict(Counter(row['mode'] for row in rows))
    return result


def query_parts(case: dict, corpus: Corpus) -> tuple[str, str]:
    return _query_parts(consultation_request(Question(case['id'], case['question']), corpus.version, 10000))


def export_inputs(benchmark: dict, corpus: Corpus) -> dict:
    return {'format_version': 1, 'corpus_version': corpus.version, 'benchmark_id': benchmark['benchmark_id'],
            'benchmark_sha256': benchmark['benchmark_sha256'],
            'documents': [{'chunk_id': chunk.chunk_id, 'text': document_embedding_text(chunk)} for chunk in corpus.chunks],
            'queries': [{'id': case['id'], 'split': case.get('split', 'dev'),
                         'text': query_embedding_text(*query_parts(case, corpus))} for case in benchmark['cases']]}


def private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write('\n')
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def evaluate(corpus: Corpus, benchmark: dict, *, k: int = 5, retriever: str = 'bm25', provider=None,
                   cache_dir=None, candidate_pool: int = 20, rrf_k: float = 60,
                   lexical_weight: float = 1, dense_weight: float = 1,
                   allow_lexical_fallback: bool = False) -> dict:
    if retriever not in {'bm25', 'dense', 'hybrid', 'both'} or k <= 0:
        raise ValueError('Unsupported retriever or k')
    modes = ['bm25', 'dense', 'hybrid'] if retriever == 'both' else [retriever]
    if any(mode != 'bm25' for mode in modes) and provider is None:
        raise ValueError('Dense/hybrid evaluation requires real vectors or an explicitly configured embedding API')
    lexical = BM25Retriever(corpus)
    hybrid = HybridRetriever(corpus, provider, cache_dir=cache_dir, candidate_pool=candidate_pool,
                             rrf_k=rrf_k, lexical_weight=lexical_weight, dense_weight=dense_weight) if provider else None
    preparation_ms = 0.0
    if hybrid:
        started = time.perf_counter()
        try:
            await hybrid.prepare(timeout_budget_ms=120000)
        except Exception:
            if not allow_lexical_fallback:
                raise ValueError('Embedding preparation failed; refusing a misleading hybrid comparison') from None
        preparation_ms = (time.perf_counter() - started) * 1000
    rows = []
    for case in benchmark['cases']:
        query, context = query_parts(case, corpus)
        results = {}
        if 'bm25' in modes:
            started = time.perf_counter()
            hits = lexical.search(query, context=context, limit=k)
            results['bm25'] = (hits, (time.perf_counter() - started) * 1000, 'bm25', None, {})
        if hybrid and any(mode != 'bm25' for mode in modes):
            searched = await hybrid.search(query, context=context, limit=k, timeout_budget_ms=10000)
            fallback = searched.mode == 'lexical_fallback'
            if fallback and not allow_lexical_fallback:
                raise ValueError('Query embedding failed; refusing a misleading hybrid comparison')
            if 'hybrid' in modes:
                results['hybrid'] = (searched.hits, searched.timings_ms['total'], searched.mode, searched.fallback_reason, searched.timings_ms)
            if 'dense' in modes:
                # A failed dense branch has no semantic hits; never substitute BM25.
                results['dense'] = (searched.dense_hits, sum(searched.timings_ms[name] for name in ('index', 'embedding_query', 'dense')),
                                    'unavailable' if fallback else 'dense', searched.fallback_reason, searched.timings_ms)
        for name, (hits, latency, actual_mode, reason, timings) in results.items():
            ids = [hit.chunk.chunk_id for hit in hits]
            rows.append({'id': case['id'], 'split': case.get('split', 'dev'), 'retriever': name,
                         'mode': actual_mode, 'fallback_reason': reason, 'ranked_chunk_ids': ids,
                         'scores': [hit.score for hit in hits], 'latency_ms': latency, 'timings_ms': timings,
                         'metrics': evaluate_ranking(ids, case, k, benchmark.get('relevance_threshold', 2))})
    return {
        'corpus_version': corpus.version, 'benchmark_id': benchmark['benchmark_id'],
        'benchmark_sha256': benchmark.get('benchmark_sha256'), 'label_status': benchmark.get('label_status', 'unspecified'),
        'judgment_policy': benchmark.get('judgment_policy', {}), 'k': k,
        'embedding_model': provider.model if provider else None, 'embedding_identity': provider.identity if provider else None,
        'configuration': {'candidate_pool': candidate_pool, 'rrf_k': rrf_k, 'lexical_weight': lexical_weight, 'dense_weight': dense_weight},
        'preparation_ms': preparation_ms,
        'summary': {name: summarize([row for row in rows if row['retriever'] == name]) for name in modes},
        'by_split': {split: {name: summarize([row for row in rows if row['retriever'] == name and row['split'] == split]) for name in modes}
                     for split in sorted({row['split'] for row in rows})},
        'cases': rows,
        'limitations': ['Development retrieval only; not answer quality or organizer score.',
                        'Unjudged chunks score zero; alternative valid evidence may be missing from labels.',
                        'Precomputed-vector query timings exclude local encoder inference; dense/hybrid share one query embedding.'],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index-dir', required=True, type=Path)
    labels = parser.add_mutually_exclusive_group(required=True)
    labels.add_argument('--qrels', type=Path)
    labels.add_argument('--questions', type=Path, help='Legacy JSONL with query/relevant_chunk_ids')
    parser.add_argument('--retriever', choices=['bm25', 'dense', 'hybrid', 'both'], default='bm25')
    parser.add_argument('--k', type=int, default=5)
    embeddings = parser.add_mutually_exclusive_group()
    embeddings.add_argument('--vectors', type=Path)
    embeddings.add_argument('--embedding-api', action='store_true')
    parser.add_argument('--cache-dir', type=Path)
    parser.add_argument('--candidate-pool', type=int, default=20)
    parser.add_argument('--rrf-k', type=float, default=60)
    parser.add_argument('--lexical-weight', type=float, default=1)
    parser.add_argument('--dense-weight', type=float, default=1)
    parser.add_argument('--allow-lexical-fallback', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--export-embedding-inputs', type=Path)
    args = parser.parse_args()
    corpus = Corpus.load(args.index_dir)
    benchmark = load_benchmark(args.qrels or args.questions, corpus, legacy=args.questions is not None)
    if args.export_embedding_inputs:
        private_json(args.export_embedding_inputs, export_inputs(benchmark, corpus))
    provider = PrecomputedEmbeddingProvider.from_file(args.vectors) if args.vectors else OpenAIEmbeddingClient(EmbeddingConfig.from_env()) if args.embedding_api else None
    report = asyncio.run(evaluate(corpus, benchmark, k=args.k, retriever=args.retriever, provider=provider,
                                  cache_dir=args.cache_dir, candidate_pool=args.candidate_pool, rrf_k=args.rrf_k,
                                  lexical_weight=args.lexical_weight, dense_weight=args.dense_weight,
                                  allow_lexical_fallback=args.allow_lexical_fallback))
    if args.output:
        private_json(args.output, report)
    print(json.dumps({'k': report['k'], 'embedding_model': report['embedding_model'], 'summary': report['summary']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
