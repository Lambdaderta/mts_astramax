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

This design favors evidence preservation over free paraphrase. Lexical retrieval
and sentence matching cannot establish semantic relevance or all dependencies
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

- `LLM_BASE_URL` or `OPENAI_BASE_URL`.
- `LLM_API_KEY` or `OPENAI_API_KEY`.
- `LLM_MODEL`, `OPENAI_MODEL`, or `GEN_MODEL`, defaulting to `qwen/qwen3.8-27b`.
- `TEMPERATURE`, `MAX_TOKENS`, `ML_TIMEOUT_SECONDS` and `LLM_MAX_RETRIES`.

The first nonempty alias wins. The platform mode permits the supplied internal
proxy. Web mode enables provider calls through `LLM_ENABLED=true` with
`ALLOW_PRIVATE_CORPUS_TO_LLM=true`. Non-local HTTP needs an explicit
`LLM_ALLOW_INTERNAL_HTTP=true`. The async HTTP client refuses redirects and
honors a single time budget across response streaming and transient retries.
Only transport errors, HTTP 429 and HTTP 5xx are retried. Typed unavailability
allows the web API to record a retryable turn failure and the platform adapter
to return a safe fallback. Provider errors do not expose credentials or bodies.

```bash
python scripts/evaluate.py --index-dir /private/index --questions /private/questions.jsonl --k 5
```

The evaluation script accepts JSONL entries with `query` and nonempty
`relevant_chunk_ids` from the current corpus version. It measures retrieval
Recall@k and latency. Use answer-level expectations and a live provider to assess
factual accuracy and conversation quality.
