import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from app.ml.corpus import Corpus, canonical_json, ingest, sha256
from app.ml.protocol import CorpusNotReady
from app.ml.retriever import BM25Retriever


def make_corpus(tmp_path, text="Учебный прибор измеряет потребление."):
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    (source / "test.md").write_text(text, encoding="utf-8")
    return ingest(source)


@pytest.mark.parametrize("value", [None, [], "index"])
def test_load_rejects_non_object_json(tmp_path, value):
    (tmp_path / "index.json").write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(CorpusNotReady):
        Corpus.load(tmp_path)


@pytest.mark.parametrize("field,value", [("title", []), ("page", True), ("page", 0)])
def test_load_rejects_invalid_chunk_metadata_with_valid_checksum(tmp_path, field, value):
    corpus = make_corpus(tmp_path)
    payload = corpus.payload()
    payload["chunks"][0][field] = value
    path = tmp_path / "index.json"
    path.write_text(canonical_json({**payload, "corpus_version": sha256(canonical_json(payload))}), encoding="utf-8")

    with pytest.raises(CorpusNotReady):
        Corpus.load(tmp_path)


def test_ingest_hashes_the_same_source_bytes_it_parses(tmp_path, monkeypatch):
    source = tmp_path / "test.md"
    initial = "Исходный учебный текст."
    source.write_text(initial, encoding="utf-8")
    read_bytes = Path.read_bytes

    def replace_source_after_read(path):
        data = read_bytes(path)
        if path == source:
            source.write_text("Обновленный учебный текст.", encoding="utf-8")
        return data

    monkeypatch.setattr(Path, "read_bytes", replace_source_after_read)
    corpus = ingest(tmp_path)

    assert corpus.documents[0].checksum == sha256(initial)
    assert corpus.chunks[0].text == initial


def test_parallel_index_saves_use_distinct_private_temporary_files(tmp_path, monkeypatch):
    first = make_corpus(tmp_path, "Первая версия учебных сведений.")
    second = make_corpus(tmp_path, "Вторая версия учебных сведений.")
    output = tmp_path / "index"
    barrier = Barrier(2, timeout=5)
    replace = Path.replace
    staging = []

    def overlap_writes(path, target):
        staging.append((path, path.parent.stat().st_mode))
        barrier.wait()
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", overlap_writes)
    with ThreadPoolExecutor(max_workers=2) as pool:
        written = list(pool.map(lambda corpus: corpus.save(output), (first, second)))

    assert written == [output / "index.json"] * 2
    assert Corpus.load(output) in (first, second)
    assert len({path for path, _ in staging}) == 2
    assert all(mode & 0o077 == 0 for _, mode in staging)
    assert (output / "index.json").stat().st_mode & 0o077 == 0
    assert list(output.iterdir()) == [output / "index.json"]


def test_heading_hits_can_be_retrieved_without_counting_as_body_evidence(tmp_path):
    corpus = make_corpus(tmp_path, "# Выбор\n\n## Мониторинг напряжения\n\nСм. справку.\n\n"
                         "## Применение\n\nПрибор выполняет мониторинг напряжения в электрической сети.")

    relevant, heading_only = BM25Retriever(corpus).search("мониторинг напряжения")

    assert relevant.chunk.title == "Выбор / Применение"
    assert (relevant.overlap, relevant.coverage) == (2, 1.0)
    assert heading_only.chunk.title == "Выбор / Мониторинг напряжения"
    assert (heading_only.overlap, heading_only.coverage) == (0, 0.0)


def test_search_distinguishes_single_digit_technical_parameters(tmp_path):
    corpus = make_corpus(tmp_path, "# Один\n\nСчетчик поддерживает 1 фазу.\n\n"
                         "# Три\n\nСчетчик поддерживает 3 фазы.")

    one, three = BM25Retriever(corpus).search("счетчик поддерживает 1 фазу")

    assert one.chunk.title == "Один"
    assert one.coverage == 1.0
    assert three.coverage == 0.75


def test_chunk_keeps_an_if_clause_with_the_claim_it_limits(tmp_path):
    detail = " ".join(["Описание"] * 111)
    condition = "Если напряжение стабильно, прибор может работать в этом режиме."
    corpus = make_corpus(tmp_path, f"{detail} Устройство поддерживает режим.\n\n{condition}")

    assert len(corpus.chunks) == 1
    assert corpus.chunks[0].text.endswith(condition)
