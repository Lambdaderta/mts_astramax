# Consultation core

The core is implemented and runnable. It loads only an explicitly supplied
knowledge base, retrieves source chunks, and returns typed consultation results.
With no model configuration it uses a conservative lexical/extractive baseline.
With explicit model configuration it calls the organizer-compatible API for a
structured, source-backed answer. It does not download or train a model.

The technical regulation was recovered from the link in the supplied case. It
confirms `/chat/completions`, the environment aliases below, independent CSV test
cases, and the platform-provided knowledge archive. The original engineering
spec's `ml_contract.py` was not attached; this repository reconstructs its
documented fields. Real model quality remains untested without API access.

## Local ingestion

```bash
python scripts/ingest.py --corpus-dir /private/knowledge --index-dir /private/index
```

Supported inputs are UTF-8 `.txt` / `.md`, JSON objects or arrays of
`{ "title": "...", "text": "..." }`, JSONL with one such object per line, and
text PDFs via `pypdf`. No websites are crawled. Scanned or empty PDF pages cause
ingestion to fail. PDF reading order, tables and footnotes need manual QA.

`index.json` contains source text and is private. It includes document checksums,
source-relative identifiers, page numbers for PDFs, deterministic chunk IDs, and
a corpus checksum. It is not an embedding index. Restart the API after replacing
an index; running processes hold an immutable loaded snapshot.

Markdown section headings become retrieval metadata and preserve topic
boundaries. Paragraphs are packed toward 115 words. A paragraph is never
truncated; obvious following qualifications and footnotes stay attached. This is
a heuristic, so boundaries still need review. Oversized chunks remain searchable
but are excluded from the short extractive response path. No OCR is supplied.

During this implementation, the recovered organizer archive was safely extracted
and indexed outside the deliverable directory: **19 Markdown documents, 249
chunks**. No source document, generated private index, or organizer question text
is copied into this repository. Checked-in fixtures are explicitly synthetic.

## Retrieval and context

`build_core(index_dir, llm_config=None)` implements asynchronous
`ConsultationCore.consult(ConsultationRequest)`. Each request carries its own
history; the core has no mutable user/session memory.

Retrieval uses BM25, lightweight Russian inflection normalization, and a small
set of generic lexical aliases. Its coverage threshold is provisional, not a
confidence probability. Short follow-up questions can use the previous
substantive user message. The configured model receives chronological conversation
history within a 32,000-character budget, up to 4,000 characters per message.
All 62 short messages from a 31-turn synthetic conversation fit in the regression
test. Long histories lose their oldest messages; a full fact/topic store is not
implemented here. `fact_updates` and structured `recommended_products` stay empty
rather than guessing application-specific structured facts.

Backend input construction must remove contact fields. The core also redacts
phone/email patterns and explicit name phrases. Financial values and consumption
quantities are preserved. This is not general-purpose personal-data recognition.

## Answer modes and validation

Without model configuration, an answer is an exact complete chunk inside fixed
framing. `validate_result` checks document/chunk membership, the unmodified quote,
and exact rendering. The offline baseline usually abstains on paraphrastic
questions; it is intended for deterministic plumbing and provenance checks.

Configured `grounded` mode sends retrieved candidates and the redacted dialogue
to the model. The model returns a strict object with `kind`, `answer_text`, and
`chunk_ids`. Factual answers need one to three real retrieved sources. Checks
reject nonexistent sources, unsupported numbers, changed scales/currencies/units,
guaranteed economic outcomes, and instruction-like output. All non-answer model
prose is replaced with vetted local messages to prevent hiding a product claim
inside a clarification. Invalid grounded prose becomes a safe
`insufficient_knowledge` result. Ambiguous retrieved price excerpts cause
abstention before generation because source-priority rules remain unresolved.

These are structural and lexical checks, **not proof of semantic entailment**.
They cannot fully detect omitted conditions, changed negation, unsupported
non-numeric paraphrases, or incorrect product applicability. A real answer-quality
suite and model evaluation are still required. Number/unit checks intentionally
over-reject some equivalent formatting and user-number echoes rather than
silently accepting a calculation. The simple price conflict detector can also
abstain when retrieved prices belong to different conditions or products.

Set `LLM_GENERATION_MODE=extractive` for the optional model-assisted evidence
selector: the model chooses a chunk and the server renders only its exact text.
This is useful when strict extraction is preferable to generated paraphrases.

When the index is missing or invalid, `ready` is false. Clarifying and out-of-scope
responses still work; product questions return `corpus_unavailable`. Timings are
measured; unused stages report zero. No source-backed answer is fabricated.

## Model configuration

`LLMConfig.from_env(platform=True)` reads the documented organizer variables:

- `LLM_BASE_URL` or `OPENAI_BASE_URL`.
- `LLM_API_KEY` or `OPENAI_API_KEY`.
- `LLM_MODEL`, `OPENAI_MODEL`, or `GEN_MODEL`.
- `TEMPERATURE` and `MAX_TOKENS` when supplied.

The platform mode explicitly permits its internal HTTP proxy and private-source
transmission. Web mode defaults to disabled at the application level. Enabling it
also requires `ALLOW_PRIVATE_CORPUS_TO_LLM=true` (the alias
`LLM_ALLOW_PRIVATE_CORPUS=true` is accepted). Non-local HTTP requires explicit
`LLM_ALLOW_INTERNAL_HTTP=true`; HTTPS is otherwise required. Redirects are
disabled so credentials/excerpts cannot follow another host. No endpoint, API key,
or model ID is embedded in code.

`ML_TIMEOUT_SECONDS` bounds calls; `LLM_MAX_RETRIES` defaults to two retries, at
most three. Only network failures, HTTP 429 and 5xx are retried, with bounded
backoff inside the original time budget. Authentication errors, malformed model
JSON and invalid result schemas do not trigger blind retries. Errors expose no
response bodies, request text or API credentials.

The API transport, generated output parsing and guards are tested with mocked
responses. No live call to the organizer's model has been made.

## Evaluation and remaining work

```bash
python scripts/evaluate.py --index-dir /private/index --questions /private/questions.jsonl --k 5
```

Each evaluation line contains `query` and nonempty `relevant_chunk_ids` for the
loaded version. The tool reports mean Recall@k and retrieval latency only; it
does not grade answers or measure end-to-end API latency.

A local inspection of the **18 organizer demo questions** found the expected
broad-topic document among the top five candidates for each question. This is a
topic-level smoke check, not fact-level Recall@k or answer accuracy. The offline
extractive gate accepted only one demo question, reinforcing the need for the
configured model on the actual consultation task. No leaderboard score is claimed.

Remaining work includes grounded answer-quality evaluation against the organizer
examples, full topic/fact correction behavior, embedding/reranking comparison,
model latency and an actual platform run. BAAI/bge-m3 and Qwen3 Reranker 8B are
not integrated: lexical retrieval is the tested starting point and reranking is
optional under the regulation. There are no hidden runtime model downloads.
