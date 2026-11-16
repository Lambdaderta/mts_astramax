"""Synthetic provider contracts; these do not measure a live model's factual accuracy."""
import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.ml.corpus import Chunk, sha256
from app.ml.llm_client import EvidenceSelector, LLMConfig, _read_model_response
from app.ml.protocol import (
    ConsultationRequest, ConsultationResult, ContextMessage, Evidence, InvalidEvidence, InvalidModelOutput, UserFact,
)
from app.ml.retriever import SearchHit


@pytest.fixture
def candidates():
    texts = [
        "Контур-Демо поддерживает обычные SIM-карты. Ориентир стоимости — 100 рублей; итог зависит от комплектации.",
        "Категория | Точность\n--- | ---\nУчебные электросчетчики | более 95%\n"
        "Показания используются только после проверки. Экономия не гарантируется.",
    ]
    return [
        SearchHit(Chunk(f"chunk-{index}", "synthetic-document", "Учебный контур", text, None, sha256(text)), 1.0, 1, 1.0)
        for index, text in enumerate(texts)
    ]


@pytest.fixture
def consultation():
    return ConsultationRequest(
        request_id="synthetic-request", session_id="synthetic-session", user_message_id="latest",
        latest_user_text="Исправляю: приборов 30. Подойдут ли обычные SIM-карты? Какова точность?",
        messages=[
            ContextMessage(id="prior", role="user", text="У нас 20 приборов. Почта: client@example.test."),
            ContextMessage(id="assistant", role="assistant", text="Предполагаю, что приборов 50."),
            ContextMessage(id="latest", role="user", text="Исправляю: приборов 30."),
        ],
        facts=[
            UserFact(key="meter_count", value=20, source_message_id="prior"),
            UserFact(key="meter_count", value=50, source_message_id="assistant"),
        ],
    )


def selector():
    return EvidenceSelector(LLMConfig("http://localhost/v1", "synthetic-model", "synthetic-key", allow_private_corpus=True))


def answer(text, ids=None):
    return {"kind": "answer", "answer_text": text, "chunk_ids": ids if ids is not None else ["chunk-0"]}


def test_generation_accepts_provider_paraphrase_and_preserves_source_identity(candidates, consultation):
    model = selector()
    text = "Для учебного контура подходят обычные SIM-карты."
    model._json_response = AsyncMock(return_value=answer(text))
    result = asyncio.run(model.generate(consultation.latest_user_text, consultation, candidates, 1300))

    assert result.answer_text == text and result.reason_code == "grounded_answer"
    assert result.evidence[0].quote == candidates[0].chunk.text
    assert result.evidence[0].document_id == "synthetic-document"
    policy, payload, budget = model._json_response.call_args.args
    assert budget == 1300
    assert "можно перефразировать" in policy and "превращать таблицы в предложения" in policy
    assert "его последние исправления важнее прежних сведений" in policy
    assert "в пределах 150 слов" in policy and "сохраняя исходные формулировки" not in policy
    assert payload["latest_user_text"] == consultation.latest_user_text
    assert payload["customer_facts"] == [{"topic": "general", "key": "meter_count", "value": 20}]
    assert "client@example.test" not in json.dumps(payload)
    assert payload["dialogue"][1]["role"] == "assistant"


def test_review_uses_all_candidates_and_returns_corrected_answer(candidates, consultation):
    model = selector()
    first = candidates[0].chunk
    draft = ConsultationResult(
        kind="answer", answer_text="Цена всегда составляет 999 рублей.", reason_code="grounded_answer",
        evidence=[Evidence(document_id=first.document_id, chunk_id=first.chunk_id, quote=first.text)],
    )
    corrected = (
        "Обычные SIM-карты поддерживаются. Ориентир стоимости — 100 рублей, итог зависит от комплектации. "
        "Точность для учебных электросчетчиков — более 95%; показания используются только после проверки."
    )
    model._json_response = AsyncMock(return_value=answer(corrected, ["chunk-0", "chunk-1"]))
    result = asyncio.run(model.review("Вопрос о продукте", consultation, candidates, 700, draft=draft))

    assert result.answer_text == corrected and "999" not in result.answer_text
    assert [item.chunk_id for item in result.evidence] == ["chunk-0", "chunk-1"]
    policy, payload, budget = model._json_response.call_args.args
    assert budget == 700
    assert "Черновик draft не является доказательством" in policy
    assert "совместимость, числа, валюту, масштаб, единицы, условия и ограничения" in policy
    assert payload["draft"] == {"kind": "answer", "answer_text": draft.answer_text, "chunk_ids": ["chunk-0"]}
    assert [item["chunk_id"] for item in payload["SOURCE"]] == ["chunk-0", "chunk-1"]
    assert payload["SOURCE"][1]["text"] == candidates[1].chunk.text
    assert model._json_response.await_count == 1


def test_recovery_has_no_unusable_draft_and_uses_same_response_contract(candidates, consultation):
    model = selector()
    model._json_response = AsyncMock(return_value={"kind": "clarify", "answer_text": "meter_model", "chunk_ids": []})
    result = asyncio.run(model.review("Нужна проверка совместимости", consultation, candidates, 500))

    assert result.kind == "clarify" and result.answer_text == "meter_model"
    assert result.reason_code == "missing_material_context" and result.evidence == []
    policy, payload, _ = model._json_response.call_args.args
    assert payload["draft"] is None and "сформируй ответ заново" in policy
    assert payload["clarification_options"]["meter_model"]
    assert model._json_response.await_count == 1


@pytest.mark.parametrize("method", ["generate", "review"])
@pytest.mark.parametrize("output,code", [
    ([], "invalid_model_output"),
    ({"kind": "answer", "answer_text": "private-output", "chunk_ids": [], "extra": True}, "invalid_model_output"),
    ({"kind": [], "answer_text": "private-output", "chunk_ids": []}, "invalid_model_output"),
    ({"kind": "answer", "answer_text": 12, "chunk_ids": ["chunk-0"]}, "invalid_model_output"),
    ({"kind": "answer", "answer_text": "", "chunk_ids": ["chunk-0"]}, "invalid_model_output"),
    (answer("private-output", "chunk-0"), "invalid_model_output"),
    (answer("private-output", ["unknown"]), "invalid_model_evidence"),
    (answer("private-output", ["chunk-0", "chunk-0"]), "invalid_model_evidence"),
    (answer("private-output", [{}]), "invalid_model_evidence"),
    (answer("private-output", []), "invalid_model_evidence"),
    ({"kind": "clarify", "answer_text": "meter_model", "chunk_ids": ["chunk-0"]}, "invalid_model_evidence"),
])
def test_generation_and_review_reject_malformed_outputs_with_sanitized_codes(candidates, consultation, method, output, code):
    model = selector()
    model._json_response = AsyncMock(return_value=output)
    with pytest.raises(InvalidModelOutput) as error:
        asyncio.run(getattr(model, method)("query", consultation, candidates, 500))
    assert isinstance(error.value, InvalidEvidence)
    assert error.value.code == code
    assert "private-output" not in str(error.value) and "unknown" not in str(error.value)


@pytest.mark.parametrize("content,finish,code", [
    ('{"kind":"answer"}', "length", "model_output_truncated"),
    ("private-output", "stop", "invalid_model_output"),
    ('{"kind":"answer"}', "content_filter", "invalid_model_output"),
    ('{"kind":"answer"}', "tool_calls", "invalid_model_output"),
    ('{"kind":"answer"}', "unknown", "invalid_model_output"),
    ('<think>private-output', "stop", "invalid_model_output"),
])
def test_provider_completion_failures_have_safe_reason_codes(content, finish, code):
    response = httpx.Response(200, json={"choices": [{"finish_reason": finish, "message": {"content": content}}]})
    with pytest.raises(InvalidModelOutput) as error:
        asyncio.run(_read_model_response(response))
    assert error.value.code == code and "private-output" not in str(error.value)


def test_review_reuses_configured_chat_transport_without_extra_provider_options(monkeypatch, candidates, consultation):
    captured = []
    expected = answer("Учебный контур поддерживает обычную SIM-карту.")

    def handle(req):
        captured.append(req)
        content = "<think>synthetic</think>\n```json\n" + json.dumps(expected) + "\n```"
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": content}}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs))
    model = selector()
    result = asyncio.run(model.review("query", consultation, candidates, 1000))

    assert result.answer_text == expected["answer_text"] and len(captured) == 1
    assert captured[0].url.path == "/v1/chat/completions"
    body = json.loads(captured[0].content)
    assert set(body) == {"model", "temperature", "max_tokens", "messages"}
    assert body["model"] == model.config.model and body["max_tokens"] == model.config.max_tokens
    assert json.loads(body["messages"][1]["content"])["draft"] is None
