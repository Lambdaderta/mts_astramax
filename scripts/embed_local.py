"""Embed private evaluation inputs with an already downloaded CLS encoder.

Optional local-only dependencies: torch, transformers, numpy. They are not
needed or included in the competition ZIP. No model or corpus network calls.
The cached rubert-tiny2 model card specifies normalized CLS sentence embeddings.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-length", type=int, default=2048)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 64 or not 1 <= args.max_length <= 8192:
        parser.error("Unsupported batch size or token limit")
    model_path = args.model_path.resolve(strict=True)
    if not model_path.is_dir():
        parser.error("--model-path must be a local directory")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    import torch
    import transformers
    from transformers import AutoModel, AutoTokenizer

    data = json.loads(args.inputs.read_text(encoding="utf-8"))
    if data.get("format_version") != 1:
        parser.error("Unsupported evaluation input format")
    texts = list(dict.fromkeys(row["text"] for row in data["documents"] + data["queries"]))
    if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
        parser.error("Inputs must contain nonempty strings")
    digest = hashlib.sha256()
    sources = sorted(path for path in model_path.iterdir() if path.suffix in {".json", ".txt", ".safetensors", ".bin"})
    for path in sources:
        digest.update(path.name.encode())
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True, trust_remote_code=False)
    model = AutoModel.from_pretrained(model_path, local_files_only=True, trust_remote_code=False).eval()
    # CPU makes the small development run portable and avoids device setup.
    torch.set_num_threads(min(4, os.cpu_count() or 1))
    limit = min(args.max_length, model.config.max_position_embeddings)
    rows = []
    truncated = 0
    with torch.inference_mode():
        for start in range(0, len(texts), args.batch_size):
            batch = texts[start:start + args.batch_size]
            lengths = tokenizer(batch, truncation=False, add_special_tokens=True)["input_ids"]
            truncated += sum(len(item) > limit for item in lengths)
            encoded = tokenizer(batch, padding=True, truncation=True, max_length=limit, return_tensors="pt")
            vectors = torch.nn.functional.normalize(model(**encoded).last_hidden_state[:, 0, :], p=2, dim=1)
            for text, vector in zip(batch, vectors.tolist()):
                rows.append({"text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "vector": vector})
    identity = f"local:{args.model_id}:sha256:{digest.hexdigest()}:cls-l2:max-length={limit}"
    result = {
        "format_version": 1, "model": args.model_id, "identity": identity,
        "dimensions": model.config.hidden_size, "embeddings": rows,
        "provenance": {"corpus_version": data["corpus_version"], "benchmark_sha256": data["benchmark_sha256"],
                       "pooling": "CLS then L2 normalization", "max_length": limit, "truncated_inputs": truncated,
                       "torch": torch.__version__, "transformers": transformers.__version__, "device": "cpu"},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(result, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.write("\n")
        temporary.replace(args.output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"Embedded {len(rows)} unique inputs; dimensions={model.config.hidden_size}; truncated={truncated}; network=disabled")


if __name__ == "__main__":
    main()
