# Consultation core

The core loads an explicitly supplied knowledge base, retrieves source chunks,
and returns a typed `ConsultationResult`. The local mode uses lexical retrieval
and short source excerpts. With a configured provider, the model selects
supporting statements or returns a decision to clarify, abstain, or route an
out-of-scope request. The same core serves the web backend and `solution.py`.

## Local ingestion

```bash
python scripts/ingest.py --corpus-dir /private/knowledge --index-dir /private/index
```

Ingestion accepts UTF-8 `.txt`, `.md`, JSON objects or arrays of
`{ "title": "...", "text": "..." }`, JSONL with one such object per line, and
text PDFs. Scanned or empty PDF pages require OCR or manual review before
indexing. PDF reading order, tables, and footnotes still require corpus QA.

`index.json` contains source text, document checksums, source-relative IDs, PDF
page numbers, chunk IDs, and a checksum for the full corpus. The ingest command
stages and atomically replaces the index with mode `0600`. Restart the service
after replacing an index because each process keeps a loaded snapshot. A missing
or invalid index marks the core unready. Clarification and out-of-scope replies
still work without it.

Markdown headings become retrieval metadata. Paragraphs are grouped toward 115
words and retained whole. Recognizable standalone qualifications and footnotes
stay with the preceding material. Chunks too long for a complete short excerpt
remain available to grounded retrieval. The organizer sample indexed locally to
19 documents and 248 chunks. The repository fixtures are synthetic.

## Retrieval and context

With generation enabled, the shared core uses BM25 plus normalized dense cosine
search. Each branch returns 20 candidates by default, merged by weighted
reciprocal-rank fusion with `RRF_K=60` and equal branch weights. The first six
eligible passages reach the generator. Dense input is document title plus body;
query input is the latest turn plus the same relevant user context used by BM25.
Ranking scores are not probabilities and do not bypass answer validation.

The organizer-compatible embedding client calls `/embeddings` with `model` and
`input`, validates indexed vectors, dimensions and finite values, and batches
the corpus once before submission workers start. Web startup also prepares the
index. Concurrent builds share one result. Optional private disk caches bind to
the corpus version, actual chunk text, model/provider identity and dimensions;
they are written atomically with owner-only permissions. Default storage is
memory only. A failed corpus build switches that process to BM25 with a recorded
reason; restart after fixing configuration. A failed query embedding falls back
for that query. Traces include retrieval mode, model and stage timings. The CSV
adapter prints aggregate retrieval modes for completed consultations.

BM25 uses Russian inflection normalization and a small set of lexical aliases.
Heading matches help locate sources, while offline evidence coverage measures
matches in the cited body. For a follow-up with an implicit reference or missing
subject, retrieval adds up to two previous user messages with lower weight than
the latest turn. Assistant prose never becomes a retrieval term. A stand-alone
question searches on its own wording. The configured provider receives the
latest question, retrieved candidates, and up to 32,000 characters of redacted
same-session dialogue, bounded to 4,000 characters per message.

The model payload can include bounded, source-linked customer facts for the
active topic. The backend supplies verified values. The core keeps
`fact_updates` and `recommended_products` empty and carries no session state
across requests. Redaction covers recognized contact patterns and explicit
contact fields. Arbitrary personal information in free text remains a boundary
for the caller to manage.

## Answer validation

In grounded mode, the provider returns `kind`, `answer_text`, and `chunk_ids`.
Each factual sentence must match wording in a supplied source, with limited
normalization of whitespace and ruble notation. The validator checks the full
chunk against the loaded index, groups recognizable standalone qualifications
with their claim, and rejects unsupported wording, reversed roles, unsupported
prices and units, economic guarantees, unrelated citations, and complete
multi-statement copies. The customer receives at most 150 words. Full evidence
is available through the authorized review trace. The provider can select a
specific local clarification question. An unsupported or repeated question
uses a local safe response. Invalid model output yields a short explanation and
an offer to contact a specialist.

This design favors evidence preservation over free paraphrase. Hybrid retrieval
and sentence matching cannot establish factual correctness or all dependencies
between sections or documents. Tables that need selected rows or interpretation
can lead to abstention. Price conflicts are screened for matching query terms
and normalized ruble amounts. Corpus-level precedence and non-price conflicts
still require evaluation. End-to-end organizer model quality and latency require
live access.

The offline baseline renders a complete short chunk with a fixed introduction
and closing. Use it for local testing and provenance checks. It abstained on most
organizer examples. `LLM_GENERATION_MODE=extractive` lets the configured model
select one source while preserving this same excerpt rendering.

## Provider configuration

`LLMConfig.from_env(platform=True)` accepts the organizer proxy settings:

- `LLM_BASE_URL`, `BASE_URL`, or `OPENAI_BASE_URL`.
- `LLM_API_KEY` or `OPENAI_API_KEY`.
- `LLM_MODEL`, `GEN_MODEL`, or `OPENAI_MODEL`, defaulting to `qwen/qwen3.8-27b`.
- `TEMPERATURE`, `MAX_TOKENS`, `ML_TIMEOUT_SECONDS` and `LLM_MAX_RETRIES`.

The first nonempty, non-placeholder alias wins after stripping whitespace. A
value equal to its own variable name is ignored, as in the organizer sample.
The platform mode permits the supplied internal
proxy. Web mode enables provider calls through `LLM_ENABLED=true` with
`ALLOW_PRIVATE_CORPUS_TO_LLM=true`. Non-local HTTP needs an explicit
`LLM_ALLOW_INTERNAL_HTTP=true`. The async HTTP client refuses redirects and
honors a single time budget across response streaming and transient retries.
Only transport errors, HTTP 429 and HTTP 5xx are retried. Typed unavailability
allows the web API to record a retryable turn failure and the platform adapter
to return a safe fallback. Provider errors do not expose credentials or bodies.
Valid gzip/deflate responses are decoded by httpx and checked against the same
decoded-byte limits. Malformed completion envelopes become controlled invalid
evidence responses. Platform generation defaults to 90 seconds, versus 10
seconds for the web service, and remains bounded by the remaining request budget.

| Setting | Starting value | Meaning |
| --- | --- | --- |
| `TEMPERATURE` | `0` | Organizer sample baseline; not a measured optimum |
| `MAX_TOKENS` | `2048` | Generation budget; the current answer contract is still 150 words |
| `MAX_WORKERS` | `3` | Parallel questions in the platform adapter |
| `RETRIEVAL_MODE` | `hybrid` with generation, otherwise `bm25` | Retrieval ablation switch |
| `TOP_K` | `6` | Maximum passages sent to grounded generation; supported range 1..30 |
| `RETRIEVAL_CANDIDATES` | `max(20, TOP_K)` | Candidates from each branch |
| `RRF_K`, `LEXICAL_WEIGHT`, `DENSE_WEIGHT` | `60`, `1`, `1` | Rank fusion parameters |
| `EMB_MODEL` | `baai/bge-m3` | Use the runtime model ID; default matches the organizer sample |
| `EMBEDDING_BATCH_SIZE` | `32` | Documents per embedding request |
| `EMBEDDING_TIMEOUT_SECONDS` | `30` | Total budget across an embedding call's batches and retries |
| `RETRIEVAL_TIMEOUT_SECONDS` | `8` | Per-query dense budget, also capped at 40% of remaining turn budget |
| `EMBEDDING_CACHE_DIR` | unset | Optional writable private vector-cache directory |

`EMBEDDER_API_KEY` overrides the shared key. `EMBEDDING_DIMENSIONS` and
`EMBEDDING_MODEL_REVISION` can enforce a known vector space and invalidate caches
after a provider model update. `CHUNKER`, `RERANKER_MODEL` and `RERANKER_TOP_N`
are documented organizer options but this implementation uses its own fixed
section-aware chunker and does not call a reranker. `TOP_K` is retrieval depth,
not Qwen's sampling `top_k`.

The organizer only explicitly documents temperature and token-budget controls
for generation. Qwen's upstream card also describes `top_p`, sampling `top_k`,
thinking controls and `reasoning_effort`, but proxy acceptance is unverified.
These are not sent automatically. Start with the sample's documented generation
settings, then compare answer quality, truncation, failures and latency on the
actual proxy. Increasing `MAX_TOKENS` alone does not increase the 150-word answer
limit; a later answer-length experiment must change prompt and validator together.

## Retrieval evaluation

Use private qrels tied to the current corpus version. Labels identify supporting
passages and required evidence groups; alternative passages in one group are
not all required. Reports distinguish chunk Recall@k, MRR@k, graded nDCG@k,
group coverage and full required-group coverage. No-evidence guardrails are
excluded from retrieval-recall denominators. They are not scored as perfect
recall merely because there is no relevant chunk. Legacy JSONL is supported.

```bash
python scripts/evaluate.py --index-dir /private/index --qrels /private/qrels.json \
  --retriever bm25 --k 5 --output /private/bm25.json \
  --export-embedding-inputs /private/embedding-inputs.json

# Optional local development measurement; uses an already downloaded E5 encoder.
# Install torch, transformers and numpy only in the local development environment.
python scripts/embed_e5_local.py --inputs /private/embedding-inputs.json \
  --model-path /path/to/cached/multilingual-e5-small \
  --output /private/vectors.json

python scripts/evaluate.py --index-dir /private/index --qrels /private/qrels.json \
  --retriever both --vectors /private/vectors.json --k 5 --output /private/comparison.json
```

The evaluator refuses to call lexical fallback a hybrid result unless explicitly
allowed. Local cached vectors test the named local model, not the organizer's
embedding service. Retrieval latency with precomputed vectors excludes encoder
inference. The small official-demo/paraphrase benchmark is development data,
not an unseen test or a substitute for the organizer's answer score. Keep
corpus text, labels, vectors and full reports private. See
[the current validation record](../../docs/validation.md) for measured results.

`scripts/embed_local.py` also reproduces the normalized CLS ruBERT-tiny2
diagnostic. That encoder made equal-weight fusion worse on the current labels;
the E5 development comparison improved Recall@5 from 0.5153 to 0.5748. Neither
encoder is bundled in the submission. `--rrf-k`, `--lexical-weight` and
`--dense-weight` allow explicit development ablations; record selection on this
set as tuning, not as independent test performance.
