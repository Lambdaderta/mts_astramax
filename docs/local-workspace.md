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

With Docker, set `INDEX_DIR_HOST` to the index directory. Index files are written with owner-only permissions. On a Linux bind mount, grant the container user (UID 10001) read access to the index and traversal access to the mounted directory through ownership or a file ACL. Keep the mount read-only.

For a local file-format check, point the platform adapter at the directory holding `questions.csv` and `knowledge-base.zip`. The offline run uses the lexical baseline and writes a private output file.

```bash
.venv/bin/python solution.py --offline \
  --data-dir /path/to/private/inputs \
  --output /path/to/private/inputs/submission.csv
```
