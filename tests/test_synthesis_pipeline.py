"""Source-review orchestration, not a claim about a live review model's accuracy."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.ml.core import LocalConsultationCore
from app.ml.corpus import ingest
from app.ml.llm_client import LLMConfig
from app.ml.protocol import ConsultationRequest, ConsultationUnavailable, InvalidModelOutput


def make_core(tmp_path, source):
    (tmp_path / "source.md").write_text(source, encoding="utf-8")
    ingest(tmp_path).save(tmp_path)
    config = LLMConfig("http://localhost/v1", "synthetic", "dummy", allow_private_corpus=True)
    return LocalConsultationCore(tmp_path, llm_config=config)


def request(text="Что умеет учебный стенд?"):
    return ConsultationRequest(request_id="r", session_id="s", user_message_id="u",
                               latest_user_text=text, timeout_budget_ms=120000)


def answer(core, text):
    return {"kind": "answer", "answer_text": text, "chunk_ids": [core.corpus.chunks[0].chunk_id]}


def test_supported_paraphrase_survives_source_review(tmp_path):
    core = make_core(tmp_path, "Учебный стенд принимает данные приборов с сим-картами других операторов.")
    payload = answer(core, "К учебному стенду можно подключить приборы с SIM-картами другого оператора.")
    model = core.selector._json_response = AsyncMock(side_effect=[payload, payload])
    result = asyncio.run(core.consult(request()))
    assert result.kind == "answer" and result.answer_text == payload["answer_text"]
    assert result.model_metadata["answer_review"] == "corrected_or_confirmed"
    assert model.await_count == 2
    review_payload = model.await_args_list[1].args[1]
    assert review_payload["SOURCE"]
    core.validate_result(result)


def test_complete_short_source_passage_needs_no_second_model_call(tmp_path):
    source = "Учебный стенд принимает данные датчиков. Отчеты доступны после подключения."
    core = make_core(tmp_path, source)
    model = core.selector._json_response = AsyncMock(return_value=answer(core, source))
    result = asyncio.run(core.consult(request()))
    assert result.kind == "answer" and result.answer_text == source
    assert result.model_metadata["answer_review"] == "verbatim"
    model.assert_awaited_once()


def test_reviewed_answer_over_word_recommendation_is_not_discarded(tmp_path):
    source = "Учебный стенд: " + "показания " * 151 + "."
    core = make_core(tmp_path, source)
    payload = answer(core, source)
    model = core.selector._json_response = AsyncMock(return_value=payload)
    result = asyncio.run(core.consult(request()))
    assert result.kind == "answer" and result.answer_text == source
    assert model.await_count == 2


@pytest.mark.parametrize("draft", [
    "Учебный стенд ремонтирует генераторы.",
    "Учебный стенд поддерживает промышленные датчики.",
    "Архив доступен без подключения к сети.",
    "Цена учебного стенда — 100 тысяч рублей.",
    "Цена учебного стенда — 100 долларов.",
    "Мощность учебного стенда — 50 МВт.",
    "Контроллер проверяет сервер.",
])
def test_unsafe_draft_cannot_bypass_review_and_replace_its_correction(tmp_path, draft):
    source = ("Учебный стенд собирает показания учебных датчиков. "
              "Промышленные датчики не поддерживаются. "
              "Архив доступен только при подключении к сети. "
              "Цена учебного стенда — 100 рублей. Мощность — 50 кВт. "
              "Сервер проверяет контроллер.")
    core = make_core(tmp_path, source)
    correction = "Учебный стенд собирает показания учебных датчиков."
    model = core.selector._json_response = AsyncMock(side_effect=[answer(core, draft), answer(core, correction)])
    result = asyncio.run(core.consult(request()))
    assert result.kind == "answer" and result.answer_text == correction
    assert model.await_count == 2


def test_malformed_generation_gets_one_recovery_attempt(tmp_path):
    source = "Учебный стенд собирает показания датчиков."
    core = make_core(tmp_path, source)
    model = core.selector._json_response = AsyncMock(side_effect=[
        InvalidModelOutput("model_output_truncated"), answer(core, source),
    ])
    result = asyncio.run(core.consult(request()))
    assert result.kind == "answer"
    assert result.model_metadata["initial_output_error"] == "model_output_truncated"
    assert result.model_metadata["answer_review"] == "recovered"
    assert model.await_count == 2


def test_first_call_unavailability_can_use_reserved_recovery_budget(tmp_path):
    source = "Учебный стенд собирает показания датчиков."
    core = make_core(tmp_path, source)
    model = core.selector._json_response = AsyncMock(side_effect=[
        ConsultationUnavailable("Synthetic first call timeout"), answer(core, source),
    ])
    result = asyncio.run(core.consult(request()))
    assert result.kind == "answer"
    assert result.model_metadata["initial_output_error"] == "model_unavailable"
    assert result.model_metadata["answer_review"] == "recovered"
    assert model.await_count == 2


def test_conflicting_price_candidates_cannot_skip_source_review(tmp_path):
    core = make_core(tmp_path, "# Первые сведения\n\nСтоимость учебного стенда составляет 100 рублей.\n\n"
                     "# Другие сведения\n\nСтоимость учебного стенда составляет 200 рублей.")
    model = core.selector._json_response = AsyncMock(side_effect=[
        answer(core, core.corpus.chunks[0].text),
        {"kind": "insufficient_knowledge", "answer_text": "Условия стоимости различаются.", "chunk_ids": []},
    ])
    result = asyncio.run(core.consult(request("Какая стоимость учебного стенда?")))
    assert result.kind == "insufficient_knowledge"
    assert model.await_count == 2
    assert len(model.await_args_list[1].args[1]["SOURCE"]) == 2


@pytest.mark.parametrize("failure,reason", [
    (InvalidModelOutput("invalid_model_output"), "invalid_model_output"),
    (ConsultationUnavailable("Synthetic unavailable"), "answer_review_unavailable"),
])
def test_failed_review_does_not_release_unreviewed_draft_or_loop(tmp_path, failure, reason):
    core = make_core(tmp_path, "Учебный стенд принимает данные датчиков.")
    draft = answer(core, "Учебный стенд бесплатно ремонтирует любые датчики.")
    model = core.selector._json_response = AsyncMock(side_effect=[draft, failure])
    result = asyncio.run(core.consult(request()))
    assert result.kind == "insufficient_knowledge" and result.reason_code == reason
    assert "бесплатно" not in result.answer_text
    assert model.await_count == 2


@pytest.mark.parametrize("query,source,reply", [
    ("Мы стали больше работать вечером, и счет вырос. Можно разобраться, из-за чего?",
     "Счет зависит от объема потребления и цены. Следует сравнить счета за одинаковые периоды.",
     "Счет зависит от объема потребления и цены. Следует сравнить счета за одинаковые периоды."),
    ("Какие схемы подключения поддерживает ваша платформа?",
     "Платформа поддерживает проводные и беспроводные схемы подключения.",
     "Платформа поддерживает проводные и беспроводные схемы подключения."),
    ("Какая стоимость учебного стенда?",
     "Стоимость учебного стенда с одним датчиком составляет 100 рублей. "
     "Стоимость учебного стенда с двумя датчиками составляет 200 рублей.",
     "Стоимость учебного стенда с одним датчиком составляет 100 рублей. "
     "Стоимость учебного стенда с двумя датчиками составляет 200 рублей."),
])
def test_answerable_questions_are_not_preempted_by_broad_regexes(tmp_path, query, source, reply):
    core = make_core(tmp_path, source)
    model = core.selector._json_response = AsyncMock(return_value=answer(core, reply))
    result = asyncio.run(core.consult(request(query)))
    assert result.kind == "answer" and result.answer_text == reply
    model.assert_awaited_once()


def test_review_shares_original_deadline(tmp_path, monkeypatch):
    core = make_core(tmp_path, "Учебный стенд принимает показания датчиков.")
    payload = answer(core, "Учебный стенд получает данные от датчиков.")
    budgets = []
    clock = [100.0]
    monkeypatch.setattr('app.ml.core.time', SimpleNamespace(perf_counter=lambda: clock[0]))

    async def model(policy, data, budget):
        budgets.append(budget)
        clock[0] += 0.02
        return payload

    core.selector._json_response = model
    req = request().model_copy(update={"timeout_budget_ms": 1000})
    result = asyncio.run(core.consult(req))
    assert result.kind == "answer" and len(budgets) == 2
    assert 0 < budgets[0] <= 800
    assert 0 < budgets[1] < 990
    assert result.timings_ms["review"] > 0
