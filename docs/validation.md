# Validation record

Executed on 12 November 2026, Python 3.12.14, using the pinned local environment. These results establish implementation behavior, not competition acceptance or live model quality.

## Executed checks

| Check | Result | Scope |
| --- | --- | --- |
| `python -m pytest -q --tb=short` | **96 passed**, 3.02 seconds; one upstream Starlette/AnyIO deprecation warning | Backend, HTTP contract/security boundaries, ML, privacy regressions and platform packaging |
| Backend integration scenarios | 30 passed within the suite | Session isolation; token hashes; duplicate/retry conflicts; one processing turn across application instances; timeouts; stale leases; idempotent partial/full leads; stale previews; restart persistence; forced SQLite response-commit failure |
| Independent review regressions | 8 passed within the suite | Historical contact redaction through correction, clearing and restart; contact-label parsing; trace model metadata; synopsis correction order; accepted 4000-character text after redaction |
| ML tests | 36 passed within the suite | Corpus/checksum integrity; source validation; scope and missing knowledge; injection examples; generated non-answer bypasses; numeric magnitude/currency/unit changes; economic guarantees; conflicting prices; 31-turn history budget; preservation of business quantities |
| Platform adapter tests | 17 passed within the suite | CSV quoting/IDs/limits; fresh context per row; archive traversal/link/size checks; atomic output; clean-directory packed execution; actual HTTP requests to temporary localhost fake proxies |
| HTTP boundary and contract tests | 5 passed within the suite | Body size, sanitized validation errors, rate limiting, CORS/review defaults, explicit model configuration and generated OpenAPI consistency |
| Real Uvicorn HTTP smoke | Passed | Created session, requested manager, filled synthetic contacts, previewed/confirmed lead, safely retried confirmation, read history and closed session; server stopped afterward |
| `/health/ready` with real local index | 200 | SQLite and the loaded local corpus were available; no external model health or quality claim |
| Python `compileall` | Passed | Application, scripts, entrypoint and ML contract |
| Generated frontend TypeScript syntax | Parsed successfully by Node 24's `--experimental-strip-types` | Syntax check only; no TypeScript compiler/type-checker run |
| `docker compose config --quiet` | Passed | Configuration validity only |
| Container image build/restart | **Not run** | Docker client exists, but no Docker daemon/socket is running on this machine |

The complete suite used the exact runtime dependency versions recorded in `requirements.lock`, with test tools in `requirements-dev.txt`. Local fake-server tests need permission to bind loopback sockets in restrictive execution environments. No real model or external inference endpoint was called.

## Real corpus and demo data

The organizer's issued archive was retrieved through the link in the original case and processed locally. It contains **19 Markdown documents**, producing **249 section-aware chunks**. Corpus version:

```text
1e8e2243767886442ddcd2c6ad01e8da3b32063861ece0abc26bf3d014ab0d34
```

The private corpus, index, demonstration questions and generated answers are outside the source deliverable and both ZIPs.

A local top-five retrieval inspection found the expected **broad-topic document for all 18 demo questions**. The diagnostic was based on expected document topics, not annotated relevant chunks or claim-level answer support. It is not Recall@5, an accuracy percentage, or a judge score. Generic Russian normalization/synonyms were adjusted using these public demonstration examples; a held-out evaluation is still needed.

Running the real demo questions through the final offline core gave:

| Decision | Count |
| --- | ---: |
| Sourced excerpt answer | 1 |
| Clarification | 1 |
| Insufficient knowledge / conservative abstention | 16 |

Local sequential median processing was 0.290 ms and maximum 0.835 ms with the index already loaded. These numbers concern the tiny local lexical baseline, exclude model/API work, and must not be compared to full-consultation latency goals. The result demonstrates that retrieval can find relevant topics while the strict offline evidence gate under-answers the consultation task.

The official CSV adapter also processed the real archive and all 18 demo questions in explicit offline mode, producing 18 unique nonempty output rows. This validates file/format handling only. Local fake proxies validate request structure and package execution; they do not establish compatibility with live Qwen behavior.

## Unverified and intentionally incomplete

- Live organizer Qwen responses, token/rate behavior, numerical and semantic correctness, product recommendation quality and end-to-end latency.
- Real generated answers on the demo set, a held-out corpus evaluation, and organizer-platform execution or score.
- Full topic/fact correction reasoning beyond bounded chronological history. Current manager notes are extractive and structured facts/product lists are not inferred.
- Dense embeddings and reranking. They remain optional optimizations to compare with the measured lexical baseline.
- Full AT-01…AT-20 acceptance, frontend integration, chat/debug UI, deployment and final competition eligibility.

Source checks prove where quotations came from. They cannot prove that generated prose preserves every qualifier, negation and applicability condition. Conflict detection is intentionally conservative and may abstain for different products with different prices until an authoritative source-priority rule and applicability logic are supplied.
