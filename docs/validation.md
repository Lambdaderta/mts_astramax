# Energytool backend validation

Updated 14 November 2026. This record separates the current development checks from results recorded on 13 November. Historical results describe the code and environment tested at that time; they are not a claim that the latest code was rerun in every earlier environment.

## 14 November: regression and packaging

| Check | Verified result | Scope and limit |
| --- | --- | --- |
| Final full local suite, Python 3.12.14 | **223 passed, 7 skipped**, 5.33 s | Includes the hybrid client, consultation integration, evaluator and Qwen response parser. Seven PostgreSQL tests were skipped because no test database was configured. One existing Starlette/AnyIO deprecation warning. |
| Submission-package checks with loopback HTTP available | **21 passed** | Package and platform-adapter behavior, including corpus warm-up, per-query embeddings, generation, authentication failure and explicit per-query lexical fallback. This does not exercise the organizer's live service. |
| Targeted hybrid-search/client checks | **28 passed** | Local hybrid/client regression checks. These are functional checks, not evidence of improved answer quality. |
| Synthetic evaluator checks | **12 passed** | Independent checks of graded metrics, required evidence groups, corpus version validation and preventing failed embeddings from being reported as hybrid. |
| Clean extracted submission ZIP, official demo inputs | **18/18 valid rows** | Offline format check: exact ID order, nonempty answers, 19-document ingestion. Thirteen root-level files and three dependencies from the allowed list; no corpus, index, vectors or secrets. |
| `compileall`, `git diff --check`, `docker compose config --quiet` | Passed | Current application/scripts/tests compile and Compose configuration parses. Containers were not rebuilt in this pass. |

The parser accepts a bounded leading Qwen reasoning block or separate reasoning fields while validating only the final JSON. Unfinished reasoning, nested blocks, truncated output and unsupported answer evidence are rejected. This compatibility handling does not enable unverified proxy request parameters.

## 14 November: retrieval benchmark and encoder comparison

The current submission chunker produces **248 chunks from 19 documents**, corpus version `409d8ba6ffc4829b55bbe2949288431523a3632be62edc19ec98634f7e49bddd`. A stale 249-chunk index was reconciled before evaluation: the document metadata and normalized document text were identical, but two price-category passages were merged in the current index. The only referenced ID that changed was mapped using identical document identity, text, title and checksum. Evaluation labels and embedding inputs now refer to the current 248-chunk index.

The private development benchmark contains **26 cases**: 18 official demo questions, six authored paraphrases, and two guardrail cases. **24 cases have positive relevance labels**; the unsupported-model and clarification cases have no positive retrieval target and are excluded from positive-query Recall@k. Source-based labels were authored before the new ranking comparison. A separate agent reviewed the six difficult cases (`demo10`, `demo14`–`demo18`), and its corrections were applied before the labels were frozen. Mechanical validation passed for all 26 cases, including chunk existence, source hashes/checksums, evidence-group membership and paraphrase-label consistency.

The frozen private `qrels-v1.json` has SHA-256 `1ebb18f7a3718730ead19987f6abda9f412f67f09c0ecb024b4e8b5b4ba81eb3`. Its 55 referenced chunks include direct evidence and supporting context. Required evidence groups treat duplicate passages as alternatives; optional aspects, such as offering a pilot, do not count as compulsory evidence. Report group coverage alongside ordinary recall so retrieving all duplicate passages is not confused with covering the facts needed for an answer.

An initial local comparison using `cointegrated/rubert-tiny2` found **lower**, not higher, Recall@5 after equal-weight hybrid fusion:

| Retrieval configuration | Mean Recall@5 across 24 positive development cases |
| --- | ---: |
| BM25 | 0.5153 |
| Equal-weight BM25 + local ruBERT hybrid | 0.3845 |

This ruled out the small cached encoder for our local development baseline. A second encoder, `intfloat/multilingual-e5-small`, was downloaded from its public model repository; all private-corpus encoding ran locally with network access disabled. The [official E5 preprocessing](https://huggingface.co/intfloat/multilingual-e5-small) was used: query/passage prefixes, attention-mask mean pooling and L2 normalization. Revision: `614241f622f53c4eeff9890bdc4f31cfecc418b3`. It encoded 274 inputs to 384-dimensional vectors in 5.49 seconds on CPU. One of 248 passages exceeded the 512-token limit and was truncated; no query was truncated.

The E5 comparison used the same frozen labels, candidate pool of 20, `RRF_K=60` and equal weights. Fusion weights were not tuned for E5.

| Metric, 24 positive development cases | BM25 | E5 dense only | BM25 + E5 hybrid |
| --- | ---: | ---: | ---: |
| Recall@5 | 0.5153 | 0.4720 | **0.5748** |
| MRR@5 | 0.6278 | 0.5792 | **0.7125** |
| nDCG@5 | 0.5106 | 0.4579 | **0.5342** |
| Required evidence-group coverage@5 | 0.7917 | 0.7292 | **0.8333** |

The hybrid Recall@5 gain is 5.95 percentage points. On the 18 official examples alone, recall moves from 0.5296 to 0.5534, while group coverage stays at 0.8333; on six related paraphrases, recall moves from 0.4722 to 0.6389. This split matters because paraphrases are not independent holdout evidence. At the configured generation depth of six, BM25/hybrid Recall@6 is 0.5222/0.5935 and group coverage is 0.7917/0.8333. All 26 dense/hybrid runs used real local vectors, with zero lexical fallback.

Full private results are `e5-small-evaluation-v1.json` and `e5-small-evaluation-k6-v1.json` under the local private retrieval-evaluation directory. Reports retain per-case rankings, split metrics, model identity, corpus version and label checksum. Query timings with precomputed vectors exclude encoder inference and must not be presented as live API latency. The encoder script and evaluation commands are documented in `app/ml/README.md`.

The submission uses the organizer's runtime `EMB_MODEL` through its local proxy, with the sample default `baai/bge-m3`; E5 weights and precomputed corpus vectors are not packaged. The local E5 gain therefore validates that local configuration, not BGE quality or the final submitted answers. Keep the equal-weight fusion as a starting point and rerun the same benchmark with authorized organizer embeddings when available.

These are assistant-authored development labels, with independent agent review of six cases, not organizer hidden gold or expert acceptance. The paraphrases share their source cases' answer semantics and are not a held-out test set. Unlisted positive passages may remain, and retrieval metrics measure evidence access rather than generated-answer correctness, hallucination rate or competition score. No live Qwen or organizer-provider quality result has been established.

## 13 November: PostgreSQL and response progress

After the web backend moved to PostgreSQL 17 in Docker Compose, the complete **175-test suite** passed on Python 3.12 with PostgreSQL 17. A complete local run on Python 3.13 with PostgreSQL 15 also passed. The checks used temporary schemas and synthetic data.

- Concurrent confirmation of one enquiry, replay of one turn, session isolation and recovery from a save failure were checked. Saved history remained available after restarting PostgreSQL and the API.
- Migration from SQLite versions 1–3 preserved session tokens, history, enquiries, answers and replay state. A nonempty target database was rejected, and an import failure rolled back the copy.
- HTTP checks in Docker covered Swagger, database/index readiness, a `202` response with topic labels while the test proxy was running, the final answer and enquiry persistence. They used a synthetic corpus and a test proxy.
- The complete suite included `solution.py` packaging and CSV checks. Real Qwen answer quality and latency require a separate run through the organizer's proxy.

## 13 November: checks before PostgreSQL migration

Local changes on top of `537697d` were reviewed against the original MTS case, the Q&A summary and the team's Backend Engineering Contract v1. The automated-evaluation format was checked against the [organizer's written regulation](https://docs.google.com/document/d/1wZsqH4xXZXwjiAJCtFQGjTaLxEU3D_tMCSw0yXcbQ0U/edit), demonstration files and sample submission.

| Check | Recorded result | Scope |
| --- | --- | --- |
| `python -m pytest -q --tb=short`, local Python 3.13.5 | **164 passed**, 3.66 s | API, SQLite, contacts/privacy, sources, timeouts, submission packaging and the test HTTP proxy |
| Same suite in Docker, Python 3.12.13 | **164 passed**, 4.33 s | Target Python version and dependencies from `requirements.lock` |
| Original `537697d` tests on Python 3.13.5 | 95 passed, 1 failed | HTTP 413/422 descriptions depended on Python version. The application retained the previous descriptions explicitly; the saved OpenAPI matched generated output. |
| `docker compose config`, build and startup | Passed | Local container and private index; `/health/live` and `/health/ready` returned 200 |
| `scripts/smoke.py` and container recreation | Passed | Manager request, full enquiry, repeated confirmation, history and closure. After recreation, the previous enquiry, 17 messages and protected trace were found. A new chat began with a greeting. |
| Two concurrent sessions over HTTP | Passed | One session's token could not read the other session. Each history contained only its own messages. |
| `solution.py` from the extracted ZIP | **18/18 valid CSV rows** | Actual demo questions in offline mode; original IDs/order and nonempty answers. That historical ZIP contained ten root files, and its three dependencies were checked against the permitted list. |
| `compileall`, `ruff check --select F`, `git diff --check` | Passed | Application code, scripts, tests, entry point and ML contract |

Both 164-test pytest runs emitted Starlette's warning about the future removal of a deprecated AnyIO alias.

## 13 November: sessions, enquiries and storage

Regression coverage included SQLite v1→v2 migration with history preserved, recovery after an expired lease, preventing an expired handler from saving an answer, and polling during a concurrent write. Separate checks covered provider failure and retry with the same key, session isolation, safe traces when evidence is rejected, readiness responses for a damaged schema and `400` for invalid JSON.

Enquiry checks covered a direct specialist request, complete and partial contact details, a draft without a phone number that cannot be confirmed, stale previews and repeated confirmation. The preview retained the task from a mixed-intent turn, current explicitly stated meter counts and the latest open question. Count corrections were linked to messages and stored by resource. Other business facts remained available through bounded history and summary text.

## 13 November: corpus diagnostics and response timing

The issued archive was processed locally; the historical record reported **19 documents and 248 chunks**. Search in the five multi-turn demo cases included previous user turns. The original version included no previous user turn in any of those five cases. The composition of the top five documents changed in 15 of 18 queries. This was a comparison of search outputs without relevance judgments, not an accuracy result.

Offline mode returned one sourced answer, one clarification and 16 insufficient-information responses across the 18 demo cases. This matched the original version. The run verified CSV formatting and retrieval execution; it did not establish full consultation quality through a model.

On a warm container, **36 sequential HTTP turns** using local BM25 and SQLite had median latency **10.26 ms** and nearest-rank p95 **23.44 ms**. Requests included user turns from demo questions and synthetic clarification, topic-change, specialist-handoff and out-of-scope scenarios. The measurement covered local HTTP handling and storage. It excluded cold-container startup and Qwen calls and must not be reused as a PostgreSQL or end-to-end model-latency result.

## Remaining validation

- Validate retrieval on the organizer's embedding model; the measured improvement above uses local E5 development data. Extend and independently review labels before treating it as a generalization result.
- Measure Qwen answers and latency through the organizer's proxy, verify its service limits and obtain an actual platform score. The local test proxy covers HTTP behavior, network failures and the overall response-time budget only.
- Review product selection, conflicting sources and conditions carried between documents. Text grounding requires close source wording and may reject valid paraphrases.
- Check compatibility with an existing frontend if the team has a separate original contract bundle. Current API schemas are checked against the code; full AT-01–AT-20 acceptance requires the interface and model access.
- Collect counters and distributions in the deployment environment. The backend emits structured stage/timing events; a separate metrics collector is not connected.
