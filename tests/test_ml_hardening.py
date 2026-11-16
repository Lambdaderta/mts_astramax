import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.ml.core import LocalConsultationCore, build_query
from app.ml.corpus import ingest
from app.ml.grounding import CLARIFY_RESPONSES, is_verbatim_supported, validate_generated_result
from app.ml.llm_client import DEFAULT_MODEL, EvidenceSelector, LLMConfig, _read_model_response
from app.ml.protocol import (
    ConsultationRequest, ConsultationResult, ConsultationUnavailable, ContextMessage, Evidence, InvalidEvidence, UserFact,
)


def config(**kwargs):
    return LLMConfig("http://localhost/v1", "mock", "dummy", allow_private_corpus=True, **kwargs)


@pytest.mark.parametrize("content", [
    '{"chunk_ids":[]}',
    '<think>Synthetic reasoning is not answer content.</think>\n{"chunk_ids":[]}',
    '<think>Synthetic reasoning.</think>\n```json\n{"chunk_ids":[]}\n```',
])
def test_qwen_reasoning_envelope_only_exposes_final_json(content):
    response = httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
        "content": content, "reasoning_content": "Separate synthetic reasoning is ignored.",
    }}]})
    assert asyncio.run(_read_model_response(response)) == {"chunk_ids": []}


@pytest.mark.parametrize("content,reason", [
    ('<think>Unfinished reasoning {"chunk_ids":[]}', "stop"),
    ('{"chunk_ids":[]}', "length"),
    ('<think>' + 'x' * 32769 + '</think>{"chunk_ids":[]}', "stop"),
    ('<think><think>Nested</think></think>{"chunk_ids":[]}', "stop"),
    ('<think>First</think><think>Second</think>{"chunk_ids":[]}', "stop"),
    ('{"chunk_ids":[]}', "content_filter"),
])
def test_truncated_or_unclosed_reasoning_is_not_an_answer(content, reason):
    response = httpx.Response(200, json={"choices": [{"finish_reason": reason, "message": {"content": content}}]})
    with pytest.raises(InvalidEvidence):
        asyncio.run(_read_model_response(response))


def request(text, **kwargs):
    return ConsultationRequest(request_id="request", session_id="session", user_message_id="latest", latest_user_text=text, **kwargs)


def make_core(tmp_path, text, *, grounded=True):
    (tmp_path / "synthetic.md").write_text(text, encoding="utf-8")
    ingest(tmp_path).save(tmp_path)
    return LocalConsultationCore(tmp_path, llm_config=config() if grounded else None)


def mock_generated(core, **payload):
    model = AsyncMock(return_value=payload)
    core.selector._json_response = model
    return model


def test_substantive_followup_uses_recent_user_context_without_assistant_claims(tmp_path):
    core = make_core(tmp_path, "# Потери в сети\n\nДля анализа потерь используются данные учета.")
    messages = [
        ContextMessage(id="task", role="user", text="Нужно исследовать потери в электрической сети."),
        ContextMessage(id="details", role="user", text="Сохранился архив показаний приборов учета."),
        ContextMessage(id="assistant", role="assistant", text="Неверная гипотеза про вымышленный продукт."),
    ]
    followup = request("Около года. Этих данных хватит для начала?", messages=messages)
    query = build_query(followup)
    assert query.startswith(followup.latest_user_text)
    assert "потери в электрической сети" in query and "архив показаний" in query
    assert "вымышленный продукт" not in query
    model = mock_generated(core, kind="insufficient_knowledge", answer_text="", chunk_ids=[])
    asyncio.run(core.consult(followup))
    payload = model.call_args.args[1]
    assert payload["SOURCE"]
    assert payload["latest_user_text"] == followup.latest_user_text
    new_topic = request("Какое оборудование учитывает горячую воду?", messages=messages)
    assert build_query(new_topic) == new_topic.latest_user_text


def test_price_lookup_ignores_other_product_and_normalizes_same_price(tmp_path):
    core = make_core(tmp_path, "# Маяк-Демо\n\nСтоимость Маяк-Демо составляет 100 рублей.\n\n"
                     "# Условия Маяк-Демо\n\nСтоимость Маяк-Демо составляет 100 ₽.\n\n"
                     "# Компас-Демо\n\nСтоимость Компас-Демо составляет 200 рублей.", grounded=False)
    result = asyncio.run(core.consult(request("Какая стоимость Маяк-Демо?")))
    assert result.kind == "answer"
    assert "200" not in result.answer_text


def test_selected_clarification_is_targeted_and_does_not_repeat(tmp_path):
    core = make_core(tmp_path, "Учебный стенд поддерживает только описанные модели счетчиков.")
    mock_generated(core, kind="clarify", answer_text="meter_model", chunk_ids=[])
    latest = "Подходит ли учебный стенд для моего счетчика?"
    answer = asyncio.run(core.consult(request(latest)))
    assert answer.answer_text == CLARIFY_RESPONSES["meter_model"]
    messages = [ContextMessage(id="question", role="assistant", text=answer.answer_text)]
    repeated = asyncio.run(core.consult(request(latest, messages=messages)))
    assert repeated.kind == "insufficient_knowledge"
    assert "специалист" in repeated.answer_text


def test_provider_facts_are_topic_and_source_bound(tmp_path):
    core = make_core(tmp_path, "В синтетическом примере электрические приборы учета собирают данные.")
    model = mock_generated(core, kind="insufficient_knowledge", answer_text="", chunk_ids=[])
    messages = [
        ContextMessage(id="past", role="user", text="У нас 30 приборов учета электричества."),
        ContextMessage(id="assistant", role="assistant", text="Ассистент предполагает 50."),
    ]
    facts = [
        UserFact(topic_key="electricity", key="meter_count", value=30, source_message_id="past"),
        UserFact(topic_key="electricity", key="meter_count", value=50, source_message_id="assistant"),
        UserFact(topic_key="water", key="meter_count", value=90, source_message_id="past"),
        UserFact(topic_key="general", key="phone", value="+7 999 123 45 67", source_message_id="past"),
        UserFact(topic_key="general", key="meter_count", value=200, source_message_id="another-session"),
    ]
    asyncio.run(core.consult(request("Нужен учет электричества на объекте.", messages=messages, facts=facts, active_topic="electricity")))
    payload = model.call_args.args[1]
    assert payload["customer_facts"] == [{"topic": "electricity", "key": "meter_count", "value": 30}]


def test_invalid_model_evidence_uses_safe_reply(tmp_path):
    core = make_core(tmp_path, "Учебный стенд собирает показания приборов.")
    mock_generated(core, kind="answer", answer_text="Цена составляет 987 рублей.", chunk_ids=["unknown"])
    result = asyncio.run(core.consult(request("Как стенд собирает показания?")))
    assert result.reason_code == "invalid_model_evidence"
    assert result.evidence == [] and "987" not in result.answer_text


def test_fast_path_requires_adjacent_qualification_and_preserves_roles(tmp_path):
    source = "Сервер проверяет контроллер. Только после запуска.\n\nДатчики показывают текущие\nпоказания."
    core = make_core(tmp_path, source)
    chunk = core.corpus.chunks[0]
    evidence = [Evidence(document_id=chunk.document_id, chunk_id=chunk.chunk_id, quote=chunk.text)]
    for answer in ("Контроллер проверяет сервер. Только после запуска.", "Сервер проверяет контроллер."):
        assert not is_verbatim_supported(ConsultationResult(kind="answer", answer_text=answer, reason_code="test", evidence=evidence), core.corpus)
    for answer in ("Сервер проверяет контроллер. Только после запуска.", "Датчики показывают текущие показания.", source):
        result = ConsultationResult(kind="answer", answer_text=answer, reason_code="test", evidence=evidence)
        validate_generated_result(result, core.corpus)
        assert is_verbatim_supported(result, core.corpus)


def test_recommended_word_length_routes_to_review_instead_of_rejecting(tmp_path):
    source = "Сведения " * 151 + "."
    core = make_core(tmp_path, source)
    chunk = core.corpus.chunks[0]
    result = ConsultationResult(kind="answer", answer_text=source, reason_code="test", evidence=[
        Evidence(document_id=chunk.document_id, chunk_id=chunk.chunk_id, quote=chunk.text),
    ])
    validate_generated_result(result, core.corpus)
    assert not is_verbatim_supported(result, core.corpus)


def test_default_provider_model(monkeypatch):
    for name in ("LLM_MODEL", "OPENAI_MODEL", "GEN_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost/v1")
    monkeypatch.setenv("LLM_API_KEY", "dummy")
    assert LLMConfig.from_env(platform=True).model == DEFAULT_MODEL


def mock_transport(monkeypatch, handler):
    real_client = httpx.AsyncClient
    clients = []

    def create_client(**kwargs):
        client = real_client(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", create_client)
    return clients


def test_total_provider_budget_cancels_slow_stream_and_closes_client(monkeypatch):
    class SlowStream(httpx.AsyncByteStream):
        closed = False
        delivered = 0

        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(0.01)
                self.delivered += 1
                yield b"x"

        async def aclose(self):
            self.closed = True

    stream = SlowStream()
    clients = mock_transport(monkeypatch, lambda _: httpx.Response(200, stream=stream))
    selector = EvidenceSelector(config(max_retries=0))
    with pytest.raises(ConsultationUnavailable, match="time budget"):
        asyncio.run(selector._json_response("policy", {}, 100))
    assert 0 < stream.delivered < 100
    assert stream.closed and clients[0].is_closed


def test_transport_retries_throttling(monkeypatch):
    requests = []

    def throttle_once(req):
        requests.append(req)
        if len(requests) == 1:
            return httpx.Response(429)
        content = "```json\n" + json.dumps({"chunk_ids": []}) + "\n```"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    mock_transport(monkeypatch, throttle_once)
    selector = EvidenceSelector(config(max_retries=1))
    assert asyncio.run(selector._json_response("policy", {}, 1500)) == {"chunk_ids": []}
    assert len(requests) == 2
    assert all(req.url.path == "/v1/chat/completions" for req in requests)


def test_transport_does_not_follow_redirect(monkeypatch):
    requests = []

    def redirect(req):
        requests.append(req)
        return httpx.Response(302, headers={"location": "https://elsewhere.test/"})

    mock_transport(monkeypatch, redirect)
    selector = EvidenceSelector(config(max_retries=1))
    with pytest.raises(ConsultationUnavailable, match="rejected"):
        asyncio.run(selector._json_response("policy", {}, 1000))
    assert len(requests) == 1
