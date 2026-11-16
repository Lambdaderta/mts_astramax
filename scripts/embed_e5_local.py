"""Encode frozen retrieval inputs locally with multilingual E5.

Public model card: https://huggingface.co/intfloat/multilingual-e5-small
Retrieval preprocessing: query:/passage: prefixes, attention-mask mean pooling,
L2 normalization, at most 512 tokens. Files must already exist locally; all
network access through Transformers/Hugging Face is disabled before loading.
Output lookup keys hash the ORIGINAL unprefixed text used by HybridRetriever.
Optional torch/transformers dependencies are evaluation-only, not submit deps.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

MODEL_ID = "intfloat/multilingual-e5-small"
MODEL_CARD = "https://huggingface.co/intfloat/multilingual-e5-small"


def prepare_inputs(data: dict) -> list[tuple[str, str]]:
    """Deduplicate within roles and reject ambiguous text-only lookup keys."""
    if data.get("format_version") != 1:
        raise ValueError("Unsupported evaluation input format")
    selected: dict[str, str] = {}
    for key, prefix in (("documents", "passage: "), ("queries", "query: ")):
        rows = data.get(key)
        if not isinstance(rows, list):
            raise ValueError("Evaluation inputs need documents and queries arrays")
        for row in rows:
            text = row.get("text") if isinstance(row, dict) else None
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Every input must contain a nonempty text string")
            if text in selected and selected[text] != prefix:
                raise ValueError("The same original string occurs as both query and passage; text-only lookup would be ambiguous")
            selected[text] = prefix
    if not selected:
        raise ValueError("Evaluation inputs are empty")
    return [(text, prefix) for text, prefix in selected.items()]


def model_fingerprint(directory: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(path for path in directory.iterdir() if path.suffix in {".json", ".txt", ".safetensors", ".bin", ".model"} and path.name != "download-metadata.json")
    if not files:
        raise ValueError("No local model/tokenizer files found")
    for path in files:
        digest.update(path.name.encode("utf-8"))
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--max-length", type=int, default=512)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 64 or not 1 <= args.max_length <= 512:
        parser.error("Batch size must be 1..64 and E5 max length 1..512")
    model_path = args.model_path.resolve(strict=True)
    if not model_path.is_dir():
        parser.error("--model-path must be an existing local directory")
    input_bytes = args.inputs.read_bytes()
    data = json.loads(input_bytes)
    texts = prepare_inputs(data)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    import torch
    import transformers
    from transformers import AutoModel, AutoTokenizer

    started = time.perf_counter()
    fingerprint = model_fingerprint(model_path)
    metadata_path = model_path / "download-metadata.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
    if metadata.get("model_id", MODEL_ID) != MODEL_ID:
        parser.error("Local model metadata does not identify multilingual-e5-small")
    tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True, trust_remote_code=False)
    model = AutoModel.from_pretrained(model_path, local_files_only=True, trust_remote_code=False).eval()
    torch.set_num_threads(min(4, os.cpu_count() or 1))
    limit = min(args.max_length, model.config.max_position_embeddings)
    rows = []
    truncations = {"passage": 0, "query": 0}
    token_lengths = []
    embedding_started = time.perf_counter()
    with torch.inference_mode():
        for start in range(0, len(texts), args.batch_size):
            batch = texts[start:start + args.batch_size]
            encoded_texts = [prefix + text for text, prefix in batch]
            lengths = tokenizer(encoded_texts, truncation=False, add_special_tokens=True)["input_ids"]
            token_lengths.extend(len(item) for item in lengths)
            for (_, prefix), ids in zip(batch, lengths):
                truncations[prefix.rstrip(": ")] += int(len(ids) > limit)
            encoded = tokenizer(encoded_texts, padding=True, truncation=True, max_length=limit, return_tensors="pt")
            hidden = model(**encoded).last_hidden_state
            mask = encoded["attention_mask"]
            masked = hidden.masked_fill(~mask[..., None].bool(), 0.0)
            pooled = masked.sum(dim=1) / mask.sum(dim=1)[..., None]
            vectors = torch.nn.functional.normalize(pooled, p=2, dim=1)
            for (text, _), vector in zip(batch, vectors.tolist()):
                rows.append({"text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "vector": vector})
            if start == 0 or start + args.batch_size >= len(texts) or (start // args.batch_size) % 8 == 0:
                print(f"Locally embedded {min(start + args.batch_size, len(texts))}/{len(texts)} inputs", flush=True)
    embedding_seconds = time.perf_counter() - embedding_started
    identity = f"local:{MODEL_ID}:sha256:{fingerprint}:query-passage-prefix:masked-mean-l2:max-length={limit}"
    result = {
        "format_version": 1, "model": MODEL_ID, "identity": identity,
        "dimensions": model.config.hidden_size, "embeddings": rows,
        "provenance": {
            "model_card": MODEL_CARD, "model_revision": metadata.get("revision", "unrecorded"),
            "model_files_sha256": fingerprint, "input_file_sha256": hashlib.sha256(input_bytes).hexdigest(),
            "corpus_version": data["corpus_version"], "benchmark_sha256": data["benchmark_sha256"],
            "pooling": "attention-mask mean then L2 normalization", "document_prefix": "passage: ", "query_prefix": "query: ",
            "lookup_hash_input": "original text without role prefix", "max_length": limit,
            "truncated_inputs": sum(truncations.values()), "truncations_by_role": truncations,
            "maximum_input_tokens_before_truncation": max(token_lengths), "batch_size": args.batch_size,
            "torch": torch.__version__, "transformers": transformers.__version__, "device": "cpu",
            "embedding_seconds": embedding_seconds, "total_seconds": time.perf_counter() - started,
            "network": "disabled during model loading and encoding",
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=args.output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(result, stream, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(args.output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"Embedded {len(rows)} unique inputs; dimensions={model.config.hidden_size}; truncated={sum(truncations.values())}; network=disabled; embedding_seconds={embedding_seconds:.2f}", flush=True)


if __name__ == "__main__":
    main()
