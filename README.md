# MTS Energytool: backend and RAG

A working backend and initial RAG implementation for the Energytool sales-consultant case. The product must understand a business customer's energy problem, consult the organizer's knowledge base, ask useful follow-up questions and save a manager enquiry with contacts and a summary. Training a new language model is not required.

The supplied team specification was used for the REST design. During implementation, the organizer's linked technical regulation, 19-document knowledge archive, 18 demo questions, output examples and sample submission were recovered. The platform adapter follows that recovered CSV contract. Original machine-readable team contracts were absent, so the included REST/TypeScript/ML contracts are reconstructed and need comparison before claiming compatibility with an existing frontend.

## Run

Python 3.12:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

Open `http://127.0.0.1:8000/docs` for the interactive API contract. This is API documentation, not the final chat frontend.

Docker:

```bash
docker compose up --build
```

The API binds to localhost port 8000. SQLite lives in the `energytool_data` volume. The private index is mounted read-only from `./private/index` or `INDEX_DIR_HOST`. Without an index, session and contact workflows remain available, `/health/ready` returns 503, and product questions receive an explicit knowledge-unavailable response. `/health/live` checks the running process. See [validation results](docs/validation.md) for the container checks.

To ingest an authorized local corpus:

```bash
python scripts/ingest.py --corpus-dir /path/to/private/corpus --index-dir private/index
```

Restart the API after rebuilding the index. Supported formats are Markdown, text, structured JSON/JSONL and text PDFs. The corpus and derived index contain private material: neither belongs in source control or either distributed ZIP. No real product knowledge is checked into this project.

See [local workspace instructions](docs/local-workspace.md) for private mounts and offline evaluation.

## Modes and configuration

Default mode is local BM25 retrieval with conservative source excerpts. It performs no external model calls. With the real corpus it is a useful retrieval baseline, but its strict answer gate intentionally abstains often; it is not the competition-ready consultation mode.

To enable grounded generation on an authorized proxy, set these environment variables (Compose reads a local `.env`; direct Uvicorn requires exported environment variables):

```text
LLM_ENABLED=true
ALLOW_PRIVATE_CORPUS_TO_LLM=true
LLM_BASE_URL=<authorized OpenAI-compatible base URL>
LLM_API_KEY=<provided at runtime>
LLM_MODEL=<provided at runtime>
```

The organizer supplies proxy credentials automatically on its platform. Environment aliases `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL` and `GEN_MODEL` are supported by the shared client. The web API requires an explicit opt-in to transmit corpus excerpts. TLS is required except for localhost or explicitly enabled internal HTTP (`LLM_ALLOW_INTERNAL_HTTP=true`). Redirects are refused. A single wall-clock budget covers model transport, response reads and transient retries. See `.env.example` for other settings.

Set a private `REVIEW_TOKEN` to enable `/api/v1/review/*`. An empty token disables access. Generate it locally with `python -c "import secrets; print(secrets.token_urlsafe(32))"` and keep it out of frontend builds. `CORS_ORIGINS` allows explicit origins only. `REQUESTS_PER_MINUTE` limits requests per client address within one process. `MAX_BODY_BYTES` bounds incoming HTTP request bodies.

## Implemented

- FastAPI/Pydantic REST endpoints, Russian customer-facing messages, JSON errors and request IDs.
- SQLite/WAL persistence with an additive v1-to-v2 migration, per-session bearer token hashes, isolated append-only histories and protected review access. Readiness verifies the database schema and index availability.
- Durable turn acceptance, idempotent retries, asynchronous polling, one active turn per session enforced in SQLite, expiring leases and attempt fencing after a crashed worker. Failed model calls preserve the user message; failed response commits never report success.
- Contacts entered through a versioned form or explicit chat labels; phone normalization; full and partial enquiries; persisted preview snapshots; stale-preview rejection; explicit idempotent confirmation. Saving is local, not delivery to a CRM or manager.
- Historical explicit contact values remain redacted after correction or clearing. Raw history stays available only to the session owner and authorized review access. This is deterministic redaction, not a general PII detector.
- Structured logs cover API responses, turn completion, model failures, retries, lead confirmations, and session closure. They carry operational IDs and durations. A log collector can aggregate the events and timing fields into counters and latency distributions.
- A transport-independent consultation core, deterministic section-aware ingestion, corpus checksums, BM25 retrieval that uses relevant prior turns, measured stage timings and source traces. Grounded responses require short source-matching claims and relevant qualifications. Complete supporting evidence is available to authorized reviewers.
- Official `solution.py` CSV processing with independent contexts, safe runtime ZIP extraction, bounded concurrency, atomic output and a deterministic root-only submission packager. The API and platform use the same core.

## API integration

`openapi.yaml` is generated from the running implementation; `frontend-contract.ts` is generated from the same schemas. `ml_contract.py` re-exports the shared Python interface. Regenerate after schema changes:

```bash
python -m pip install -r requirements-dev.txt
python scripts/export_contracts.py
```

Frontend flow:

1. `POST /api/v1/sessions` with `{}`. Keep the returned session token in memory.
2. Send `Authorization: Bearer <session_token>` for that session's endpoints.
3. `POST /sessions/{id}/turns` with a UUID `client_message_id` and text. Retry with the **same ID and text** after an uncertain network result. Poll the returned turn on 202. A terminal failure has `retryable=false`; send a newly phrased message rather than replaying it indefinitely.
4. Read/edit `/lead-draft` using `expected_revision`; omitted fields remain unchanged and explicit null clears a field.
5. Create `/lead-preview`, display it, then confirm via `/leads` with `preview_id`, a stable `idempotency_key`, and `confirm:true`. Any new turn or contact change invalidates the preview.
6. A new dialogue is a new session. Closing preserves history. Successful retries remain readable after closing; new changes are rejected.

A failed earlier turn cannot be retried after a newer user turn; the API returns `RETRY_SUPERSEDED`. Confirming the same preview with a different idempotency key returns `PREVIEW_ALREADY_CONFIRMED`, preventing duplicate leads. These additional rules are documented implementation choices.

## Platform adapter

Official input is `DATA_DIR/questions.csv` with `id,question` plus `DATA_DIR/knowledge-base.zip`; default `DATA_DIR` is `data`. Output is `./submission.csv`, exactly `id,answer`, one nonempty answer per unique input ID, at most 10,000 characters per answer. Published role-labelled dialogues are parsed with fresh context for every row.

```bash
# Requires the organizer proxy environment variables.
python solution.py

# Explicit local baseline, not a scored model run:
python solution.py --offline --data-dir /path/to/inputs --output /path/to/submission.csv

# Produces a ZIP with only root-level Python modules and allowed dependencies:
python scripts/build_submission.py --output dist/submission.zip
```

The packager rewrites shared module imports through Python's AST. It excludes web application code, corpus, index, questions, environment files and credentials. Dependencies in the platform ZIP are from the recovered allowed list. Input files are supplied by the platform, so no precomputed-index exception is needed. No submission was uploaded.

`MAX_WORKERS` defaults to 3 and is capped at 8; `CONSULTATION_TIMEOUT_SECONDS` defaults to 60, and `SUBMISSION_TIMEOUT_SECONDS` to 1080, leaving room below the organizer's 20-minute limit. The model transport also honors `ML_TIMEOUT_SECONDS`, `TEMPERATURE`, `MAX_TOKENS` and official access aliases. Check live service limits before tuning.

## Verify

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python scripts/smoke.py --base-url http://127.0.0.1:8000
```

The smoke script creates a synthetic enquiry on the running server. Tests use temporary databases and synthetic knowledge; local fake-proxy tests open a loopback socket. No real model credentials or product corpus are required for the test suite.

See [validation results](docs/validation.md) for the exact executed checks and their limits, and [requirements](docs/requirements-and-status.md) for the source distinction. For retrieval evaluation, `scripts/evaluate.py` accepts labelled JSONL queries and reports Recall@k and retrieval latency. It does not grade generated answers.

## Remaining work

Live Qwen response quality and latency remain unverified. Claim validation is deliberately conservative and can reject valid paraphrases. Product applicability, conditions spread across documents and source precedence still need evaluation on the issued corpus.

Dialogue reasoning uses bounded history and stores explicit meter counts by resource with their source message. A later correction supersedes the previous value for that resource. General semantic facts, structured product recommendations, dense embedding search and model-based reranking remain open. Manager summaries use the latest substantive customer messages, retained facts and the latest outstanding consultation question when available.

The chat frontend, presentation, deployment, CRM integration and competition upload were outside this backend/ML pass. [Integration questions](docs/integration-questions.md) lists the remaining external inputs; the most useful next step is a run against the organizer proxy, followed by reviewing the 18 demo answers and the real frontend contract.
