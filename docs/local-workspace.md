# Prepared local workspace

This note applies only to the workspace where this implementation was created. Portable setup is in the README. Downloaded organizer material remains outside the distributable source directory.

From this project's directory:

```bash
# Run using the existing virtual environment and the privately built real index.
DATABASE_PATH=../../work/local.db \
INDEX_DIR=../../work/private/index \
../../work/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

The prepared environment is `../../work/venv`. Organizer downloads are in `../../work/organizer`; the extracted corpus and index are in `../../work/private`. The technical regulation was retrieved into `../../work/regulation.txt`. These paths are relative to the project directory and are not shipped in the source ZIP.

The index contains the real supplied corpus and is available for local testing. External model calls remain disabled unless explicitly configured. No model credentials were obtained or used. The temporary smoke-test server was stopped after verification.

The organizer's sample question filename is `demo_questions.csv`; the platform expects `questions.csv`. To run the official adapter locally, prepare a private input directory with the expected filenames. Do not commit that input directory or its resulting answers.
