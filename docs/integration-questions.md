# Integration questions

These questions concern the remaining external inputs. They do not block implementation of session storage, contact workflows, a typed consultation boundary or synthetic local tests.

## Resources linked by the original case

- [Technical regulation](https://docs.google.com/document/d/1wZsqH4xXZXwjiAJCtFQGjTaLxEU3D_tMCSw0yXcbQ0U/edit), embedded in original-case page 7.
- [Data and examples folder](https://drive.google.com/drive/folders/1EKFXt51aswP5IEAAcIXAcNl_geX1sdW5?usp=drive_link), embedded in original-case page 13. The case describes a knowledge base, test questions, minimum submission script, Python requirements and expected-output example.

The links were recovered from PDF annotations. Initial access failed, but an authorized network fetch subsequently recovered the technical regulation, demonstration questions and answers, and the organizer's sample submission archive. These resources resolve the earlier unknown input/output format and generation transport. Local implementation should use the recovered written rules rather than the backend specification's older placeholders.

## Questions resolved by the written regulation

- Input is `DATA_DIR/questions.csv`, with `id,question`; `DATA_DIR` defaults to `data`. Each row is an independent case, even when its question contains a dialogue history.
- Output is `submission.csv` in the working directory, UTF-8, exactly `id,answer`, one nonempty answer for every input ID, unique IDs, at most 10,000 characters per answer.
- The platform provides `knowledge-base.zip` alongside questions. Do not include either input file in the submission ZIP.
- Use root-level `solution.py`, fixed allowed dependencies in `requirements.txt`, and root-level Python support modules. Required modules in nested folders and runtime `pip install` are disallowed.
- Generation uses `{LLM_BASE_URL}/chat/completions`, bearer `LLM_API_KEY`, and the model identifier from `LLM_MODEL`. Documented alternatives are `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `OPENAI_MODEL`/`GEN_MODEL`.
- The platform provides service access automatically; a personal API key is not required. Configuration includes `TEMPERATURE`, `MAX_TOKENS`, `MAX_WORKERS` and optional retrieval settings.
- Limits are a 128 MB ZIP, 8 CPU, 16 GB RAM and 20-minute runtime. Submission quota is five per day, resetting at 00:00 Moscow time. This implementation does not submit automatically.

## Inputs needed for the next integration pass

| Priority | Question or requested artifact | What it resolves |
| --- | --- | --- |
| 1 | Confirm the current issued corpus version and any later updates to the recovered materials. | Stable real-corpus ingestion, retrieval evaluation and product regression cases |
| 1 | Provide access to the organizer runtime or authorized local proxy configuration when available. What service-specific token/rate/concurrency limits apply? | Live Qwen compatibility and latency measurements; transport and environment variable names are already documented, while actual access values are supplied at runtime |
| 2 | Can the original `openapi.yaml`, `frontend-contract.ts`, `ml_contract.py`, `schema.sql` and fixtures referenced by the team spec be supplied? | Comparison with the reconstructed implementation contracts before parallel-team integration |
| 2 | If product documents disagree, which source/date/version takes priority? | Deterministic conflict handling without inventing a resolution |
| 2 | Are additional precomputed index files permitted at the ZIP root under the current rules? | Python module layout is resolved, but the Q&A's oral index exception still needs confirmation if precomputed indexes are included; runtime ingestion avoids relying on that exception |
| 2 | Which private endpoints and deployment locations are authorized to receive corpus excerpts or derived indexes? | Real model calls and deployment inside the allowed data boundary |
| 3 | Are phone-only partial enquiries accepted by final scoring, and are all three fields mandatory in any particular test? | The team design allows partial enquiries; the Q&A supports this, while the original case names all three fields |

No external CRM is needed for this prototype. The original case explicitly permits storing the enquiry inside the solution. A successful local save must not be presented as delivery to an actual manager.

The frontend is now integrated against the refreshed API contract and included in the common Compose. Live-model acceptance, deployment and final submission materials remain; see the [current checklist](case-status.md).
