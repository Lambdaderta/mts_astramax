"""Deterministic local-only ingestion. An index contains private source text.

Paragraph boundaries are preserved. Oversized paragraphs are retained intact
rather than cutting a price away from its conditions. PDF extraction needs human
QA for tables, columns and scanned pages; this module does not perform OCR.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from functools import cached_property
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .protocol import CorpusNotReady

FORMAT_VERSION = 1
CHUNK_WORD_TARGET = 115
SUPPORTED_SUFFIXES = {".txt", ".md", ".json", ".jsonl", ".pdf"}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


@dataclass(frozen=True)
class Document:
    document_id: str
    title: str
    source: str
    checksum: str


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    document_id: str
    title: str
    text: str
    page: int | None
    checksum: str


@dataclass(frozen=True)
class Corpus:
    version: str
    documents: tuple[Document, ...]
    chunks: tuple[Chunk, ...]

    @cached_property
    def chunks_by_id(self) -> dict[str, Chunk]:
        return {chunk.chunk_id: chunk for chunk in self.chunks}

    def payload(self) -> dict[str, Any]:
        return {
            "format_version": FORMAT_VERSION,
            "chunk_word_target": CHUNK_WORD_TARGET,
            "documents": [asdict(item) for item in self.documents],
            "chunks": [asdict(item) for item in self.chunks],
        }

    def save(self, directory: str | Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "index.json"
        with TemporaryDirectory(prefix=".index-", dir=directory) as staging:
            temporary = Path(staging) / path.name
            temporary.write_text(canonical_json({**self.payload(), "corpus_version": self.version}) + "\n", encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(path)
        return path

    @classmethod
    def load(cls, directory: str | Path) -> "Corpus":
        path = Path(directory) / "index.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("Index must be a JSON object")
            if raw.get("format_version") != FORMAT_VERSION or raw.get("chunk_word_target") != CHUNK_WORD_TARGET:
                raise ValueError("Unsupported corpus format or chunking configuration")
            corpus = cls(
                version=raw["corpus_version"],
                documents=tuple(Document(**item) for item in raw["documents"]),
                chunks=tuple(Chunk(**item) for item in raw["chunks"]),
            )
            if not corpus.documents or not corpus.chunks:
                raise ValueError("Empty corpus")
            if sha256(canonical_json(corpus.payload())) != corpus.version:
                raise ValueError("Corpus checksum mismatch")
            for document in corpus.documents:
                if not all(isinstance(field, str) for field in (document.document_id, document.title, document.source, document.checksum)):
                    raise ValueError("Invalid document metadata")
            document_ids = {item.document_id for item in corpus.documents}
            if len(document_ids) != len(corpus.documents):
                raise ValueError("Duplicate document IDs")
            for chunk in corpus.chunks:
                if not all(isinstance(field, str) for field in (chunk.chunk_id, chunk.document_id, chunk.title, chunk.text, chunk.checksum)):
                    raise ValueError("Invalid chunk metadata")
                if chunk.page is not None and (type(chunk.page) is not int or chunk.page < 1):
                    raise ValueError("Invalid chunk page")
                if chunk.document_id not in document_ids or sha256(chunk.text) != chunk.checksum or not chunk.text.strip():
                    raise ValueError("Invalid chunk reference or checksum")
            if len(corpus.chunks_by_id) != len(corpus.chunks):
                raise ValueError("Duplicate chunk IDs")
            return corpus
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            raise CorpusNotReady("Local corpus index is missing or invalid; run the ingest command.") from exc


def _paragraph_chunks(text: str) -> list[str]:
    """Pack intact paragraphs; do not truncate tables, footnotes or conditions."""
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text.replace("\r\n", "\n")) if part.strip()]
    chunks: list[str] = []
    pending: list[str] = []
    word_count = 0
    for paragraph in paragraphs:
        words = len(paragraph.split())
        # Keep following qualifications/footnotes with the preceding statement,
        # even when that creates an oversized chunk requiring manual review.
        is_qualification = bool(re.match(
            r"(?:услов|огранич|примечан|только|если\b|при\b|кроме|за исключен|не\b|иначе\b|\*|\[\^?\d+\])",
            paragraph, re.IGNORECASE,
        ))
        if pending and word_count + words > CHUNK_WORD_TARGET and not is_qualification:
            chunks.append("\n\n".join(pending))
            pending, word_count = [], 0
        pending.append(paragraph)
        word_count += words
    if pending:
        chunks.append("\n\n".join(pending))
    return chunks


def _markdown_units(text: str, default_title: str) -> list[tuple[str, str, int | None]]:
    """Retain heading hierarchy as retrieval metadata and respect section breaks."""
    units: list[tuple[str, str, int | None]] = []
    headings: list[tuple[int, str]] = []
    body: list[str] = []
    def flush() -> None:
        content = "\n".join(body).strip()
        if content:
            units.append((" / ".join(title for _, title in headings) or default_title, content, None))
        body.clear()
    for line in text.splitlines():
        match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if match:
            flush()
            depth, title = len(match[1]), match[2].strip()
            while headings and headings[-1][0] >= depth:
                headings.pop()
            headings.append((depth, title))
        elif not re.fullmatch(r"[-*_]{5,}\s*", line):
            body.append(line)
    flush()
    return units


def _read_units(path: Path, data: bytes) -> list[tuple[str, str, int | None]]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data))
        units = [(path.stem, page.extract_text() or "", number) for number, page in enumerate(reader.pages, 1)]
        if any(not text.strip() for _, text, _ in units):
            raise ValueError(f"PDF {path.name} has a page without extractable text; OCR/manual review is required")
        return units
    text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    if suffix == ".md":
        return _markdown_units(text, path.stem)
    if suffix == ".txt":
        return [(path.stem, text, None)]
    records = [json.loads(line) for line in text.splitlines() if line.strip()] if suffix == ".jsonl" else json.loads(text)
    if isinstance(records, dict):
        records = records.get("documents", [records])
    if not isinstance(records, list):
        raise ValueError(f"{path.name}: expected a JSON object or array of {{title,text}} records")
    units = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("text"), str):
            raise ValueError(f"{path.name}: each record must have a string text field")
        title = record.get("title", path.stem)
        if not isinstance(title, str):
            raise ValueError(f"{path.name}: title must be a string")
        units.append((title, record["text"], None))
    return units


def ingest(corpus_directory: str | Path) -> Corpus:
    """Read an explicitly supplied directory; never download or crawl sources."""
    root = Path(corpus_directory).resolve()
    if not root.is_dir():
        raise ValueError("Corpus directory does not exist")
    documents: list[Document] = []
    chunks: list[Chunk] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        resolved = path.resolve()
        if root not in resolved.parents:
            raise ValueError("Corpus symlinks must not escape the supplied directory")
        source = path.relative_to(root).as_posix()
        data = resolved.read_bytes()
        checksum = sha256(data)
        document_id = "doc_" + sha256(source)[:24]
        units = _read_units(path, data)
        documents.append(Document(document_id, path.stem, source, checksum))
        ordinal = 0
        for title, text, page in units:
            for body in _paragraph_chunks(text):
                chunk_id = "chunk_" + sha256(f"{document_id}:{ordinal}:{body}")[:24]
                chunks.append(Chunk(chunk_id, document_id, title, body, page, sha256(body)))
                ordinal += 1
    if not chunks:
        raise ValueError("No nonempty supported documents found")
    corpus = Corpus("", tuple(documents), tuple(chunks))
    return Corpus(sha256(canonical_json(corpus.payload())), corpus.documents, corpus.chunks)
