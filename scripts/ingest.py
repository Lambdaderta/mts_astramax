"""Build a private local index from an explicitly supplied corpus directory."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.ml.corpus import ingest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", required=True, type=Path)
    parser.add_argument("--index-dir", required=True, type=Path)
    args = parser.parse_args()
    corpus = ingest(args.corpus_dir)
    path = corpus.save(args.index_dir)
    print(json.dumps({"index": str(path), "corpus_version": corpus.version, "documents": len(corpus.documents), "chunks": len(corpus.chunks)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
