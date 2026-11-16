# Local setup with an authorized corpus

Set up `.venv` as shown in the README. Keep the issued archive and demo questions in a private directory with owner-only access. The platform expects the demo question file under the name `questions.csv` when running the adapter locally.

Run the API with an index built from the extracted corpus.

```bash
.venv/bin/python scripts/ingest.py \
  --corpus-dir /path/to/private/corpus \
  --index-dir /path/to/private/index

INDEX_DIR=/path/to/private/index \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

With the current Compose, set `CORPUS_DIR_HOST` to the document directory and run `docker compose --profile tools run --build --rm --no-deps ingest`. Index files are stored in the `knowledge_index` named volume, read-only for the API. The container user (UID 10001) needs read and traversal access to the source directory. See [Dokploy setup](dokploy.md) for server paths and project names.

For a local file-format check, point the platform adapter at the directory holding `questions.csv` and `knowledge-base.zip`. The offline run uses the lexical baseline and writes a private output file.

```bash
.venv/bin/python solution.py --offline \
  --data-dir /path/to/private/inputs \
  --output /path/to/private/inputs/submission.csv
```
