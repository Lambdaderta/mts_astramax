# Requirements and scope

Source review: 12 November 2026. This document records requirements and design scope, not a claim that every item has been implemented or accepted. Check the README and test results for the delivered implementation status.

## What the case asks for

Build a Russian-language Energytool sales consultant that takes a visitor from an energy-related business problem to a locally saved manager enquiry. The ML task is retrieval and grounded consultation over the supplied knowledge base; training a new language model is not required.

| Requirement | Original case | Backend/ML design scope |
| --- | --- | --- |
| Understand the need, ask useful clarifications, recommend suitable products | pp. 2, 6 | A consultation core with structured decisions and session-specific context |
| Retrieve supporting product information from the issued knowledge base | pp. 6, 10 | Corpus ingestion, retrieval, source identifiers, evidence checks and measured stage timings |
| Refuse unsupported claims; offer specialist help when evidence is absent | p. 10 | Safe insufficient-knowledge and out-of-scope responses; no invented prices, compatibility or savings |
| Exclude technical documentation support and device/system configuration advice | p. 10 | Scope handling before product recommendations |
| Collect full name, telephone and company; prepare a useful manager summary | p. 6 | Contact draft, preview and explicit local confirmation; no CRM or email delivery claim |
| Preserve context across topic changes; reset on a new dialogue | pp. 6, 10 | Isolated sessions, chronological messages and source-linked client facts |
| Keep every dialogue, including those without an enquiry | pp. 6, 10 | Persistent history and authorized review access |
| Show response time and source documents in debug mode | p. 6 | Protected review traces for a later frontend |
| Process independent automated test cases through `solution.py` | p. 7; recovered technical regulation §§2–4 | Reuse the consultation core behind a separate CSV platform adapter |
| Reproducible Docker launch and fixed dependencies | p. 8 | Container configuration, environment template and documented local verification |

The user requested backend and ML work. The chat frontend, its debug interface, presentation, deployment and competition submission are deferred. A complete competition entry still needs a working chat interface, all submission materials and a positive automated-judge result (case pp. 8, 11). API functionality alone does not establish final-entry eligibility.

## Source hierarchy and limits

1. **Original case:** `МТС_кейс (1).pdf`, 17 physical pages. Its task, constraints, models and deliverables are the organizer-facing basis. Pages 6, 7, 9, 10 and 13 were also rendered and visually reviewed.
2. **Team backend design:** `MTS_Energytool_Backend_TechSpec_v1.md` and its 24-page PDF. These specify the internal API and implementation approach; they explicitly are not the organizer's platform contract.
3. **Recovered technical regulation:** the [organizer's linked Google document](https://docs.google.com/document/d/1wZsqH4xXZXwjiAJCtFQGjTaLxEU3D_tMCSw0yXcbQ0U/edit) was retrieved during implementation. Its CSV format, environment variables, package layout and runtime limits supersede the backend spec's earlier unknown-platform placeholders. Organizer demonstration questions, output and a sample submission archive were also recovered locally.
4. **Supplementary meeting summary:** the related local `MTS_HackChange_QA_Summary.pdf` paraphrases a Q&A recording. It supports accepting a phone-only partial enquiry and allowing an immediate manager request. It is neither a transcript nor a replacement for written platform rules.

The backend design refers to a separate 16-page intermediate requirements document that was not supplied. Its internal page references do not identify pages in the 17-page original case. AT-01–AT-20 and CT-01–CT-17 are team test definitions, not organizer-issued test IDs.

The referenced original `openapi.yaml`, `frontend-contract.ts`, `ml_contract.py`, `schema.sql`, example JSON files and mock-server bundle were not supplied or recovered. Any machine-readable contracts in this implementation are reconstructed from the prose and examples. They need comparison with the original bundle before another team's frontend or ML component can be claimed drop-in compatible.

## Team choices rather than organizer mandates

Python 3.12, FastAPI, Pydantic v2, SQLite/WAL, the `/api/v1` routes, bearer session tokens, separate review access, optimistic revisions, idempotency keys and explicit enquiry previews come from the team backend design. The case permits alternative stacks and architectures.

The case names Qwen3.8-27B for platform generation, BAAI/bge-m3 for available embeddings and Qwen3 Reranker 8B as an optional reranker (p. 9). The recovered regulation defines an OpenAI-compatible local proxy and environment-supplied access. The actual model identifier must come from that environment. Lexical retrieval is a permissible baseline; embeddings and reranking need measured benefits on the real corpus.

The team's median ≤7 seconds and p95 ≤10 seconds targets are internal goals. The Q&A's 5–7 second guidance is an oral usability target, not a verified platform timeout. No latency or consultation-quality claim should be made without measurements on the intended environment and issued corpus.

## Recovered platform contract

The platform supplies `questions.csv` and `knowledge-base.zip` inside `DATA_DIR` (default `data`). Questions have `id,question` columns; each row is independent, including rows containing a short dialogue. `solution.py` must write `submission.csv` in the working directory, encoded as UTF-8 with exactly `id,answer`, one nonempty answer per input ID, no duplicate IDs and no answer above 10,000 characters. CSV quoting must preserve commas, quotes and embedded newlines.

The ZIP has root-level `solution.py`, fixed-version `requirements.txt` from the permitted dependency set, and any additional root-level Python modules. Required modules must not live in nested directories; runtime package installation is forbidden. The platform provides the input questions and knowledge archive, so they are not included in the submission ZIP. The published limits are 128 MB ZIP, 8 CPU, 16 GB RAM, 20 minutes per run, and five submissions per day with reset at 00:00 Moscow time.

Generation calls target `{LLM_BASE_URL}/chat/completions` with bearer authorization. The environment supports `LLM_BASE_URL`/`OPENAI_BASE_URL`, `LLM_API_KEY`/`OPENAI_API_KEY`, and `LLM_MODEL`/`OPENAI_MODEL`/`GEN_MODEL`. `TEMPERATURE`, `MAX_TOKENS` and `MAX_WORKERS` are also supplied configuration. Actual values and service availability require the organizer environment; they must not be fabricated or stored in source code.

## Remaining information gaps

Real model calls, product-answer quality and platform execution still require verification against the actual issued corpus and organizer service. Recovering a sample contract does not establish a positive judge score. Product facts cannot be inferred from the case or synthetic fixtures. Corpus availability, local evaluation and measured results are recorded in the README rather than asserted by this requirements document.

See [integration questions](integration-questions.md) for the exact resources and decisions needed next. Private documents, indexes, credentials and user contacts must not be published with the implementation.
