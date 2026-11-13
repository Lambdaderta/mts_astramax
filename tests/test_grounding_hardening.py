import pytest

from app.ml.corpus import ingest
from app.ml.grounding import validate_generated_result
from app.ml.protocol import ConsultationResult, Evidence, InvalidEvidence


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
def test_grounded_generation_rejects_unproven_claims(consultation_evidence, answer):
    corpus, evidence = consultation_evidence
    result = ConsultationResult(kind="answer", answer_text=answer, reason_code="grounded_answer", evidence=[evidence])
    with pytest.raises(InvalidEvidence):
        validate_generated_result(result, corpus)


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
