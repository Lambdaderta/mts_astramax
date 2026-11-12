"""Executable checks for the P0 HTTP/persistence guarantees.

All names, contacts, dialogue content and ML outputs here are synthetic.
The fake core exercises transport/storage behavior, not product quality.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import sqlite3
import threading
import time
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.ml.protocol import ConsultationResult, ConsultationUnavailable, InvalidEvidence
from app.settings import Settings


REVIEW = {"Authorization": "Bearer test-review"}
PHONE = "+79990000000"


class FakeCore:
    ready = True
    corpus_version = "synthetic-test-v1"

    def __init__(self):
        self.requests = []
        self.fail = False
        self.reject_evidence = False

    async def consult(self, request):
        self.requests.append(request)
        if self.fail:
            raise ConsultationUnavailable("simulated private provider detail")
        return ConsultationResult(
            kind="clarify",
            answer_text="Уточните, пожалуйста, за какой период изменились расходы?",
            reason_code="missing_material_context",
            retrieval_query=request.latest_user_text,
            timings_ms={"total": 0.1},
            model_metadata={"model": "synthetic-test-only"},
        )

    def validate_result(self, result):
        if self.reject_evidence:
            raise InvalidEvidence("simulated invalid private chunk")


class BlockingCore(FakeCore):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    async def consult(self, request):
        if request.latest_user_text == "slow synthetic request":
            self.entered.set()
            while not self.release.is_set():
                await asyncio.sleep(0.01)
        return await super().consult(request)


@pytest.fixture
def config(tmp_path):
    return Settings(
        database_path=str(tmp_path / "test.sqlite3"),
        index_dir=str(tmp_path / "missing-index"),
        review_token="test-review",
        ml_timeout_seconds=3,
        turn_lease_seconds=10,
    )


@pytest.fixture
def core():
    return FakeCore()


@pytest.fixture
def client(config, core):
    with TestClient(create_app(settings=config, core=core)) as http:
        yield http


def new_session(client):
    response = client.post("/api/v1/sessions", json={})
    assert response.status_code == 201, response.text
    body = response.json()
    UUID(body["session"]["id"])
    return body["session"]["id"], {"Authorization": f"Bearer {body['session_token']}"}, body


def url(session_id, suffix=""):
    return f"/api/v1/sessions/{session_id}{suffix}"


def turn(client, session_id, headers, text="У нас выросли расходы.", key=None):
    return client.post(
        url(session_id, "/turns"), headers=headers,
        json={"client_message_id": key or str(uuid4()), "text": text},
    )


def complete(client, session_id, headers, response):
    """Permit either synchronous completion or the documented polling path."""
    deadline = time.monotonic() + 5
    while response.status_code == 202 and time.monotonic() < deadline:
        time.sleep(0.01)
        response = client.get(url(session_id, f"/turns/{response.json()['turn_id']}"), headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed", response.text
    return response.json()


def history(client, session_id, headers):
    response = client.get(url(session_id, "/messages"), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["items"]


def draft(client, session_id, headers):
    response = client.get(url(session_id, "/lead-draft"), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["draft"]


def patch(client, session_id, headers, fields, revision=None):
    if revision is None:
        revision = draft(client, session_id, headers)["revision"]
    return client.patch(
        url(session_id, "/lead-draft"), headers=headers,
        json={"expected_revision": revision, "fields": fields},
    )


def preview(client, session_id, headers):
    response = client.post(url(session_id, "/lead-preview"), headers=headers, json={})
    assert response.status_code == 200, response.text
    return response.json()


def confirm(client, session_id, headers, preview_id, key=None):
    return client.post(url(session_id, "/leads"), headers=headers, json={
        "idempotency_key": key or str(uuid4()), "preview_id": preview_id, "confirm": True,
    })


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["code"] == code
    UUID(error["request_id"])
    assert response.headers["X-Request-ID"] == error["request_id"]
    assert isinstance(error["retryable"], bool)


def test_session_creation_persists_greeting_and_only_token_hash(client, config):
    sid, headers, body = new_session(client)
    assert body["session"]["status"] == "active"
    assert body["session"]["phase"] == "new"
    assert body["session"]["revision"] == 1
    assert history(client, sid, headers) == [body["greeting"]]
    assert body["greeting"]["seq"] == 1
    with sqlite3.connect(config.database_path) as connection:
        row = connection.execute("SELECT token_hash FROM sessions WHERE id=?", (sid,)).fetchone()
    assert row[0] == hashlib.sha256(body["session_token"].encode()).hexdigest()
    assert body["session_token"] not in json.dumps(client.get(url(sid), headers=headers).json())


def test_session_tokens_do_not_authorize_other_sessions_or_review(client):
    first, first_auth, _ = new_session(client)
    second, second_auth, _ = new_session(client)
    assert client.get(url(first)).status_code == 401
    assert client.get(url(first), headers=second_auth).status_code in (401, 403)
    assert client.get(url(second), headers=first_auth).status_code in (401, 403)
    assert client.get("/api/v1/review/sessions", headers=first_auth).status_code in (401, 403)
    assert client.get(url(first), headers=REVIEW).status_code in (401, 403)


def test_turn_is_idempotent_and_conflicting_text_is_rejected(client, core):
    sid, headers, _ = new_session(client)
    key = str(uuid4())
    first = complete(client, sid, headers, turn(client, sid, headers, key=key))
    for _ in range(2):
        repeated = complete(client, sid, headers, turn(client, sid, headers, key=key))
        assert repeated["turn_id"] == first["turn_id"]
        assert repeated["user_message"] == first["user_message"]
        assert repeated["assistant_message"] == first["assistant_message"]
    assert len(core.requests) == 1
    assert [m["seq"] for m in history(client, sid, headers)] == [1, 2, 3]
    assert_error(turn(client, sid, headers, "Другой запрос", key), 409, "IDEMPOTENCY_CONFLICT")


@pytest.mark.parametrize("payload", [
    {"client_message_id": str(uuid4()), "text": "   "},
    {"client_message_id": str(uuid4()), "text": "я" * 4001},
    {"client_message_id": "invalid-uuid", "text": "Вопрос"},
    {"client_message_id": str(uuid4()), "text": 42},
])
def test_invalid_turn_never_writes_message(client, payload):
    sid, headers, _ = new_session(client)
    response = client.post(url(sid, "/turns"), headers=headers, json=payload)
    assert_error(response, 422, "VALIDATION_ERROR")
    assert len(history(client, sid, headers)) == 1


def test_failed_model_call_is_retried_without_duplicate_user_message(client, core):
    sid, headers, _ = new_session(client)
    key = str(uuid4())
    core.fail = True
    response = turn(client, sid, headers, key=key)
    assert_error(response, 503, "MODEL_UNAVAILABLE")
    assert "private provider detail" not in response.text
    saved = history(client, sid, headers)
    assert [m["role"] for m in saved] == ["assistant", "user"]
    core.fail = False
    result = complete(client, sid, headers, turn(client, sid, headers, key=key))
    assert result["user_message"]["id"] == saved[1]["id"]
    assert len(history(client, sid, headers)) == 3
    assert len(core.requests) == 2


def test_failed_response_commit_never_reports_success_or_leaves_partial_answer(client, config):
    sid, headers, _ = new_session(client)
    key = str(uuid4())
    with sqlite3.connect(config.database_path) as connection:
        connection.execute("""
            CREATE TRIGGER reject_synthetic_answer BEFORE INSERT ON messages
            WHEN NEW.role='assistant' AND NEW.seq>1
            BEGIN SELECT RAISE(ABORT, 'synthetic response write failure'); END
        """)
    response = turn(client, sid, headers, key=key)
    assert response.status_code in (500, 503), response.text
    assert "synthetic response write failure" not in response.text
    assert [message["role"] for message in history(client, sid, headers)] == ["assistant", "user"]
    with sqlite3.connect(config.database_path) as connection:
        state = connection.execute("SELECT status,assistant_message_id FROM turns WHERE session_id=?", (sid,)).fetchone()
        assert state == ("failed", None)
        assert connection.execute("SELECT COUNT(*) FROM answer_traces").fetchone()[0] == 0
        connection.execute("DROP TRIGGER reject_synthetic_answer")
    complete(client, sid, headers, turn(client, sid, headers, key=key))
    assert len(history(client, sid, headers)) == 3


def test_rejected_evidence_never_reaches_user_as_product_answer(client, core):
    sid, headers, _ = new_session(client)

    async def unsupported_answer(request):
        return ConsultationResult(
            kind="answer", answer_text="Цена выдуманного продукта — 987654321 рублей.",
            reason_code="grounded_answer",
        )

    core.consult = unsupported_answer
    core.reject_evidence = True
    response = turn(client, sid, headers)
    assert "987654321" not in response.text
    if response.status_code == 200:
        assert response.json()["decision"]["kind"] == "insufficient_knowledge"
    else:
        assert response.status_code in (500, 503), response.text
    assert "private chunk" not in response.text


def test_context_contains_only_current_session_and_no_credentials(client, core):
    a, a_auth, a_created = new_session(client)
    b, b_auth, b_created = new_session(client)
    complete(client, a, a_auth, turn(client, a, a_auth, "Только первая сессия: 23 прибора."))
    complete(client, b, b_auth, turn(client, b, b_auth, "Только вторая сессия: тепловой учет."))
    context = core.requests[-1].model_dump_json()
    assert "23 прибора" not in context
    assert "тепловой учет" in context
    assert a_created["session_token"] not in context
    assert b_created["session_token"] not in context
    assert "test-review" not in context
    assert core.requests[-1].session_id == b


def test_same_session_single_flight_other_sessions_can_progress(config):
    blocking = BlockingCore()
    with TestClient(create_app(settings=config, core=blocking)) as client:
        sid, headers, _ = new_session(client)
        other, other_auth, _ = new_session(client)
        key = str(uuid4())
        with ThreadPoolExecutor(max_workers=1) as pool:
            initial = pool.submit(turn, client, sid, headers, "slow synthetic request", key)
            try:
                assert blocking.entered.wait(2), "Core never received accepted turn"
                repeated = turn(client, sid, headers, "slow synthetic request", key)
                assert repeated.status_code == 202, repeated.text
                assert repeated.json()["status"] == "processing"
                competing = turn(client, sid, headers, "A new message")
                assert_error(competing, 409, "TURN_IN_PROGRESS")
                complete(client, other, other_auth, turn(client, other, other_auth))
                assert len(history(client, sid, headers)) == 2
            finally:
                blocking.release.set()
            result = complete(client, sid, headers, initial.result(timeout=5))
            assert result["turn_id"] == repeated.json()["turn_id"]
        assert len(history(client, sid, headers)) == 3


def test_close_preserves_history_but_rejects_mutations(client):
    sid, headers, _ = new_session(client)
    complete(client, sid, headers, turn(client, sid, headers))
    before = history(client, sid, headers)
    assert client.post(url(sid, "/close"), headers=headers, json={}).status_code == 200
    assert history(client, sid, headers) == before
    assert_error(turn(client, sid, headers), 409, "SESSION_CLOSED")
    assert_error(patch(client, sid, headers, {"phone": PHONE}), 409, "SESSION_CLOSED")
    review = client.get("/api/v1/review/sessions", headers=REVIEW)
    assert review.status_code == 200
    assert sid in {row["id"] for row in review.json()["items"]}
    other, other_auth, _ = new_session(client)
    assert len(history(client, other, other_auth)) == 1


def test_message_pagination_is_monotonic(client):
    sid, headers, _ = new_session(client)
    complete(client, sid, headers, turn(client, sid, headers))
    first = client.get(url(sid, "/messages?after_seq=0&limit=2"), headers=headers).json()
    assert [message["seq"] for message in first["items"]] == [1, 2]
    assert first["next_after_seq"] == 2
    second = client.get(url(sid, "/messages?after_seq=2&limit=2"), headers=headers).json()
    assert [message["seq"] for message in second["items"]] == [3]
    assert second["next_after_seq"] is None


def test_draft_has_optimistic_versioning_and_explicit_null_clears(client):
    sid, headers, _ = new_session(client)
    empty = draft(client, sid, headers)
    assert empty["revision"] == 0
    assert all(empty[field] is None for field in ("full_name", "phone", "company"))
    first = patch(client, sid, headers, {"phone": "+7 (999) 000-00-00", "company": "ООО Синтетика"}, 0)
    assert first.status_code == 200, first.text
    saved = first.json()["draft"]
    assert saved["phone"] == PHONE
    assert saved["revision"] == 1
    assert_error(patch(client, sid, headers, {"phone": "+79991111111"}, 0), 409, "DRAFT_VERSION_CONFLICT")
    cleared = patch(client, sid, headers, {"company": None}, 1)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["draft"]["company"] is None
    assert cleared.json()["draft"]["phone"] == PHONE


@pytest.mark.parametrize("phone", ["123", "+7999abc00000", "+1234567890123456"])
def test_invalid_phone_is_rejected_without_draft_change(client, phone):
    sid, headers, _ = new_session(client)
    before = draft(client, sid, headers)
    assert_error(patch(client, sid, headers, {"phone": phone}), 422, "VALIDATION_ERROR")
    assert draft(client, sid, headers) == before


def test_preview_without_phone_cannot_be_confirmed(client):
    sid, headers, _ = new_session(client)
    assert patch(client, sid, headers, {"full_name": "Тестовый Клиент"}).status_code == 200
    shown = preview(client, sid, headers)
    assert shown["status"] == "draft"
    assert shown["can_confirm"] is False
    assert_error(confirm(client, sid, headers, shown["preview_id"]), 422, "CONTACT_REQUIRED")
    assert draft(client, sid, headers)["full_name"] == "Тестовый Клиент"


@pytest.mark.parametrize("change", ["new_turn", "contacts"])
def test_preview_becomes_stale_after_dialogue_or_contacts_change(client, change):
    sid, headers, _ = new_session(client)
    assert patch(client, sid, headers, {"phone": PHONE}).status_code == 200
    shown = preview(client, sid, headers)
    if change == "new_turn":
        complete(client, sid, headers, turn(client, sid, headers))
    else:
        assert patch(client, sid, headers, {"company": "ООО Синтетика"}).status_code == 200
    assert_error(confirm(client, sid, headers, shown["preview_id"]), 409, "PREVIEW_STALE")


@pytest.mark.parametrize("fields,status", [
    ({"phone": PHONE}, "partial"),
    ({"phone": PHONE, "full_name": "Тестовый Клиент", "company": "ООО Синтетика"}, "complete"),
])
def test_confirm_lead_is_idempotent_and_preserves_explicit_contacts(client, fields, status):
    sid, headers, _ = new_session(client)
    complete(client, sid, headers, turn(client, sid, headers))
    assert patch(client, sid, headers, fields).status_code == 200
    shown = preview(client, sid, headers)
    assert shown["status"] == status
    assert shown["can_confirm"] is True
    key = str(uuid4())
    saved = confirm(client, sid, headers, shown["preview_id"], key)
    assert saved.status_code == 201, saved.text
    body = saved.json()
    lead = body["lead"]
    assert lead["session_id"] == sid
    assert lead["status"] == status
    assert lead["phone"] == PHONE
    assert lead["company"] == fields.get("company")
    assert body["message"] == "Заявка сохранена в демонстрационном прототипе."
    repeated = confirm(client, sid, headers, shown["preview_id"], key)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["lead"] == lead
    reviewed = client.get(f"/api/v1/review/leads/{lead['id']}", headers=REVIEW)
    assert reviewed.status_code == 200
    assert lead["id"] in reviewed.text
    another_preview = preview(client, sid, headers)
    assert_error(confirm(client, sid, headers, another_preview["preview_id"], key), 409, "IDEMPOTENCY_CONFLICT")


def test_review_trace_requires_separate_credentials_and_excludes_secrets(client):
    sid, headers, created = new_session(client)
    result = complete(client, sid, headers, turn(client, sid, headers))
    path = f"/api/v1/review/traces/{result['assistant_message']['id']}"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=headers).status_code in (401, 403)
    response = client.get(path, headers=REVIEW)
    assert response.status_code == 200, response.text
    trace = response.json()
    assert trace["decision"]["kind"] == "clarify"
    assert trace["evidence"] == []
    assert trace["corpus_version"] == "synthetic-test-v1"
    assert trace["latency_ms"] >= 0
    assert created["session_token"] not in response.text
    assert "test-review" not in response.text


def test_restart_keeps_messages_leads_and_session_authorization(config):
    with TestClient(create_app(settings=config, core=FakeCore())) as first:
        sid, headers, _ = new_session(first)
        complete(first, sid, headers, turn(first, sid, headers))
        assert patch(first, sid, headers, {"phone": PHONE}).status_code == 200
        shown = preview(first, sid, headers)
        lead = confirm(first, sid, headers, shown["preview_id"]).json()["lead"]
        messages = history(first, sid, headers)
    with TestClient(create_app(settings=config, core=FakeCore())) as restarted:
        assert history(restarted, sid, headers) == messages
        response = restarted.get(f"/api/v1/review/leads/{lead['id']}", headers=REVIEW)
        assert response.status_code == 200
        assert lead["id"] in response.text
        fresh, fresh_auth, _ = new_session(restarted)
        assert len(history(restarted, fresh, fresh_auth)) == 1


def test_expired_worker_lease_is_recoverable_without_duplicate_message(config):
    failing = FakeCore()
    failing.fail = True
    key = str(uuid4())
    with TestClient(create_app(settings=config, core=failing)) as first:
        sid, headers, _ = new_session(first)
        assert turn(first, sid, headers, key=key).status_code == 503
        user_id = history(first, sid, headers)[1]["id"]
    # Reproduce durable state left by a worker dying after user-message commit.
    with sqlite3.connect(config.database_path) as connection:
        turn_id = connection.execute("SELECT id FROM turns WHERE session_id=?", (sid,)).fetchone()[0]
        connection.execute(
            "UPDATE turns SET status='processing', lease_until=0, error_code=NULL, retryable=0 WHERE id=?",
            (turn_id,),
        )
    with TestClient(create_app(settings=config, core=FakeCore())) as restarted:
        recovered = restarted.get(url(sid, f"/turns/{turn_id}"), headers=headers)
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["status"] == "failed"
        result = complete(restarted, sid, headers, turn(restarted, sid, headers, key=key))
        assert result["turn_id"] == turn_id
        assert result["user_message"]["id"] == user_id
        assert len(history(restarted, sid, headers)) == 3
    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute("SELECT attempt FROM turns WHERE id=?", (turn_id,)).fetchone()[0] == 2


def test_processing_turn_is_exclusive_across_app_instances(config):
    blocking = BlockingCore()
    with TestClient(create_app(settings=config, core=blocking)) as first:
        sid, headers, _ = new_session(first)
        with TestClient(create_app(settings=config, core=FakeCore())) as second:
            with ThreadPoolExecutor(max_workers=1) as pool:
                initial = pool.submit(turn, first, sid, headers, "slow synthetic request")
                try:
                    assert blocking.entered.wait(2)
                    assert_error(turn(second, sid, headers, "A competing worker request"), 409, "TURN_IN_PROGRESS")
                finally:
                    blocking.release.set()
                complete(first, sid, headers, initial.result(timeout=5))
            assert len(history(second, sid, headers)) == 3


def test_timeout_keeps_user_message_and_allows_same_key_retry(config):
    short_timeout = replace(config, ml_timeout_seconds=0.05, sync_wait_seconds=0.2)
    blocking = BlockingCore()
    with TestClient(create_app(settings=short_timeout, core=blocking)) as client:
        sid, headers, _ = new_session(client)
        key = str(uuid4())
        response = turn(client, sid, headers, "slow synthetic request", key)
        assert_error(response, 503, "MODEL_UNAVAILABLE")
        assert len(history(client, sid, headers)) == 2
        blocking.release.set()
        complete(client, sid, headers, turn(client, sid, headers, "slow synthetic request", key))
        assert len(history(client, sid, headers)) == 3


def test_missing_corpus_reports_unready_and_never_produces_product_answer(config):
    with TestClient(create_app(settings=config)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
        sid, headers, _ = new_session(client)
        response = turn(client, sid, headers, "Какая цена продукта Energytool?")
        if response.status_code == 200:
            assert response.json()["decision"]["kind"] == "insufficient_knowledge"
        else:
            assert_error(response, 503, "KNOWLEDGE_NOT_READY")


def test_direct_manager_request_does_not_wait_for_ml(client, core):
    sid, headers, _ = new_session(client)
    core.fail = True
    result = complete(client, sid, headers, turn(client, sid, headers, "Свяжите меня с менеджером"))
    assert result["decision"]["kind"] == "collecting_contacts"
    assert result["session"]["phase"] == "collecting_contacts"
    assert core.requests == []
