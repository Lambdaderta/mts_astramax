import pytest

from app.ml.corpus import ingest
from app.ml.grounding import is_verbatim_supported, validate_generated_result
from app.ml.protocol import ConsultationResult, Evidence


@pytest.fixture
def consultation_evidence(tmp_path):
    (tmp_path / "synthetic.md").write_text(
        "# Учебный стенд\n\n"
        "Маяк-Демо собирает показания учебных датчиков. "
        "Маяк-Демо не поддерживает промышленные датчики. "
        "Архив доступен только при наличии подключения к сети. "
        "Монитор показывает отчеты. Тариф стенда составляет 100 рублей.",
        encoding="utf-8",
    )
    corpus = ingest(tmp_path)
    chunk = corpus.chunks[0]
    return corpus, Evidence(document_id=chunk.document_id, chunk_id=chunk.chunk_id, quote=chunk.text)


@pytest.mark.parametrize("answer", [
    "Маяк-Демо собирает показания спутников.",
    "Маяк-Демо собирает показания учебных датчиков. Он ремонтирует генераторы.",
    "Маяк-Демо поддерживает промышленные датчики.",
    "Архив доступен.",
    "Монитор-Демо показывает отчеты.",
    "Монитор показывает отчеты за 100 рублей.",
])
def test_unproven_claims_require_semantic_review(consultation_evidence, answer):
    corpus, evidence = consultation_evidence
    result = ConsultationResult(kind="answer", answer_text=answer, reason_code="grounded_answer", evidence=[evidence])
    assert not is_verbatim_supported(result, corpus)


@pytest.mark.parametrize("answer", [
    "Маяк-Демо собирает показания учебных датчиков.",
    "Маяк-Демо не поддерживает промышленные датчики.",
    "Архив доступен только при наличии подключения к сети.",
    "Тариф стенда составляет 100 ₽. Могу помочь подготовить обращение специалисту.",
])
def test_grounded_generation_accepts_supported_claims(consultation_evidence, answer):
    corpus, evidence = consultation_evidence
    result = ConsultationResult(kind="answer", answer_text=answer, reason_code="grounded_answer", evidence=[evidence])
    validate_generated_result(result, corpus)
    assert is_verbatim_supported(result, corpus)


@pytest.mark.parametrize("answer", [
    "Маяк-Демо получает показания от учебных датчиков.",
    "Архив можно открыть, если есть подключение к сети.",
])
def test_supported_paraphrases_are_not_rejected_by_provenance_checks(consultation_evidence, answer):
    corpus, evidence = consultation_evidence
    result = ConsultationResult(kind="answer", answer_text=answer, reason_code="grounded_answer", evidence=[evidence])
    validate_generated_result(result, corpus)
    assert not is_verbatim_supported(result, corpus)


def test_short_complete_source_is_a_valid_answer(consultation_evidence):
    corpus, evidence = consultation_evidence
    result = ConsultationResult(kind="answer", answer_text=evidence.quote, reason_code="grounded_answer", evidence=[evidence])
    validate_generated_result(result, corpus)
    assert is_verbatim_supported(result, corpus)


@pytest.mark.parametrize("answer", ["Экономию нельзя гарантировать.", "Экономия 5–15% — это не гарантия результата."])
def test_explicit_denial_of_economic_guarantee_is_allowed(consultation_evidence, answer):
    corpus, evidence = consultation_evidence
    result = ConsultationResult(kind="answer", answer_text=answer, reason_code="grounded_answer", evidence=[evidence])
    validate_generated_result(result, corpus)


@pytest.mark.parametrize("spaces", [" ", "  ", "\t"])
def test_list_normalization_does_not_erase_numeric_sign(tmp_path, spaces):
    (tmp_path / "source.md").write_text(f"Температурный порог:\n-{spaces}5 °C.", encoding="utf-8")
    corpus = ingest(tmp_path)
    chunk = corpus.chunks[0]
    result = ConsultationResult(kind="answer", answer_text="Температурный порог:\n+ 5 °C.", reason_code="grounded_answer",
                                evidence=[Evidence(document_id=chunk.document_id, chunk_id=chunk.chunk_id, quote=chunk.text)])
    assert not is_verbatim_supported(result, corpus)
