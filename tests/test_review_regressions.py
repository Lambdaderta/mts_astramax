"""Behavioral regressions discovered during independent backend review.

Every contact, request and core response in this file is synthetic.
"""
import asyncio
from uuid import uuid4

import pytest

from app.contracts import DraftPatch, TurnInput
from app.ml.protocol import ConsultationResult
from app.privacy import extract_contacts
from app.services import Service
from app.settings import Settings
from app.storage import Database


class RecordingCore:
    ready = True
    corpus_version = "synthetic-review-v1"

    def __init__(self):
        self.requests = []

    async def consult(self, request):
        self.requests.append(request)
        return ConsultationResult(
            kind="clarify",
            answer_text="За какой период изменились расходы?",
            reason_code="missing_material_context",
            model_metadata={
                "model": "synthetic-review-model",
                "mode": "test-only",
                "corpus_version": self.corpus_version,
            },
        )

    def validate_result(self, result):
        pass


@pytest.fixture
def service(tmp_path):
    settings = Settings(database_path=tmp_path / "review.sqlite3", sync_wait_seconds=1)
    return Service(Database(settings.database_path), RecordingCore(), settings)


def change_contacts(service, session_id, **fields):
    revision = service.get_draft(session_id)["draft"]["revision"]
    return service.patch_draft(
        session_id, DraftPatch(expected_revision=revision, fields=fields)
    )


async def send(service, session_id, text):
    result = await service.post_turn(
        session_id,
        TurnInput(client_message_id=uuid4(), text=text),
        str(uuid4()),
    )
    assert result["status"] == "completed", result
    return result


def test_corrected_and_cleared_contact_names_stay_out_of_model_history(service):
    async def scenario():
        sid = service.create_session()["session"]["id"]
        old_name, new_name = "Тестовый Старый", "Тестовый Новый"
        change_contacts(service, sid, full_name=old_name)
        await send(service, sid, f"{old_name} спрашивает про учет энергоресурсов")
        change_contacts(service, sid, full_name=new_name)
        await send(service, sid, f"{new_name} уточняет период измерений")
        change_contacts(service, sid, full_name=None)
        await send(service, sid, "Какие исходные данные нужно подготовить?")

        request = service.core.requests[-1]
        model_text = "\n".join(message.text for message in request.messages)
        assert old_name not in model_text
        assert new_name not in model_text
        assert "[контакт скрыт]" in model_text

        # The privacy boundary must not destroy the authorized review history.
        stored_text = "\n".join(
            message["text"] for message in service.messages(sid, 0, 200)["items"]
        )
        assert old_name in stored_text
        assert new_name in stored_text
        preview = service.preview(sid)
        assert old_name not in preview["summary"]
        assert new_name not in preview["summary"]

    asyncio.run(scenario())


def test_historical_contact_redaction_survives_service_restart(service):
    async def scenario():
        sid = service.create_session()["session"]["id"]
        change_contacts(service, sid, full_name="Тестовый Предыдущий")
        await send(service, sid, "Тестовый Предыдущий уточняет потребление")
        change_contacts(service, sid, full_name="Тестовый Текущий")

        restarted = Service(
            Database(service.settings.database_path), RecordingCore(), service.settings
        )
        await send(restarted, sid, "Какой период сравнивать?")
        model_text = "\n".join(
            message.text for message in restarted.core.requests[-1].messages
        )
        assert "Тестовый Предыдущий" not in model_text

    asyncio.run(scenario())


@pytest.mark.parametrize("text", [
    "Компания: ООО Тест, телефон +79990000000",
    "Компания: ООО Тест телефон: +79990000000",
    "ФИО: Тестовый Клиент; Компания: ООО Тест, телефон: +79990000000",
])
def test_labeled_phone_does_not_become_part_of_company(text):
    fields = extract_contacts(text)
    assert fields["company"] == "ООО Тест"
    assert fields["phone"] == "+79990000000"


def test_review_trace_records_actual_core_mode_and_model(service):
    async def scenario():
        sid = service.create_session()["session"]["id"]
        completed = await send(service, sid, "Как подготовить данные по потреблению?")
        trace = service.trace(completed["assistant_message"]["id"])
        assert trace["model_metadata"] == {
            "model": "synthetic-review-model",
            "mode": "test-only",
            "corpus_version": "synthetic-review-v1",
        }

    asyncio.run(scenario())


def test_lead_synopsis_filters_greeting_and_preserves_recent_correction(service):
    async def scenario():
        sid = service.create_session()["session"]["id"]
        await send(service, sid, "Привет")
        await send(service, sid, "Нужно наладить учет: у нас 20 счетчиков")
        await send(service, sid, "Исправляю, не 20, а 30 счетчиков")
        await send(service, sid, "Свяжите с менеджером")
        change_contacts(service, sid, phone="+79990000000")
        preview = service.preview(sid)
        assert preview["can_confirm"] is True
        assert "Привет" not in preview["client_problem"]
        assert "Свяжите" not in preview["client_problem"]
        assert "30 счетчиков" in preview["client_problem"]
        assert "+79990000000" not in preview["summary"]
        assert preview["summary"].index("20 счетчиков") < preview["summary"].index("30 счетчиков")

    asyncio.run(scenario())


def test_redaction_expansion_does_not_fail_a_valid_max_length_turn(service):
    async def scenario():
        sid = service.create_session()["session"]["id"]
        prefix = "Телефон +79990000000. "
        text = prefix + "а" * (4000 - len(prefix))
        assert len(text) == 4000
        completed = await send(service, sid, text)
        assert completed["user_message"]["text"] == text
        assert "+79990000000" not in service.core.requests[-1].latest_user_text

    asyncio.run(scenario())
