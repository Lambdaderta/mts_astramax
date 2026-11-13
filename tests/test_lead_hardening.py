import asyncio
from uuid import uuid4

import pytest

from app.contracts import TurnInput
from app.ml.protocol import ConsultationResult
from app.privacy import extract_contacts, manager_requested, redact
from app.services import Service
from app.settings import Settings
from app.storage import Database


@pytest.mark.parametrize('phone', [
    '79990000000',
    '7.999.000.00.00',
    '+7 (999) 000–00–00',
    '8 (999) 000-00-00',
    '(999) 000-00-00',
])
def test_chat_phone_extraction_and_redaction_agree(phone):
    text = f'Компания: ООО Тест, телефон: {phone}. Нужен учет тепла.'
    assert extract_contacts(text) == {'phone': '+79990000000', 'company': 'ООО Тест'}
    assert phone not in redact(text)
    assert 'Нужен учет тепла.' in redact(text)


def test_redaction_keeps_numeric_facts_and_masks_contact_variants():
    assert redact('Объем 79990000000 кВт⋅ч и 80000000 рублей') == 'Объем 79990000000 кВт⋅ч и 80000000 рублей'
    text = 'Ф.И.О.: Тестовый Клиент; телефон 999.000.00.00; почта demo@example.invalid'
    safe = redact(text)
    assert all(value not in safe for value in ('Тестовый Клиент', '999.000.00.00', 'demo@example.invalid'))
    safe = redact('Нужна Теплосеть. Контакт: ТЕПЛО. Тестовый  Клиент.', known_values=['Тепло', 'Тестовый Клиент'])
    assert 'Теплосеть' in safe
    assert 'ТЕПЛО.' not in safe
    assert 'Тестовый' not in safe
    assert redact('Мой новый телефон: +79990000000. 20 счетчиков.') == '[телефон скрыт]. 20 счетчиков.'


def test_quoted_company_is_kept_as_one_contact():
    company = 'ООО «Тест, Инжиниринг»'
    text = f'Компания: {company}; нужен учет тепла.'
    assert extract_contacts(text)['company'] == company
    assert redact(text) == '[компания скрыта]; нужен учет тепла.'


@pytest.mark.parametrize('text', [
    'Свяжите меня с менеджером',
    'Можно поговорить со специалистом?',
    'Как связаться с менеджером?',
    'Пожалуйста, перезвоните мне',
    'Хочу оставить заявку на консультацию',
    'Мне нужен менеджер',
    'Свяжите меня с человеком',
    'Не могли бы вы перезвонить?',
])
def test_explicit_handoff(text):
    assert manager_requested(text)


@pytest.mark.parametrize('text', [
    'Связано ли это с тарифом?',
    'Какая связь между тарифами и объемом потребления?',
    'Как данные по учету использует менеджер?',
    'Не перезванивайте, уточните тариф',
    'Не хочу связываться с менеджером',
    'Как менеджер перезвонит после сохранения заявки?',
])
def test_ordinary_question_stays_in_consultation(text):
    assert not manager_requested(text)


def test_preview_preserves_mixed_handoff_context_and_latest_consultation_question(tmp_path):
    class Core:
        corpus_version = 'synthetic'

        async def consult(self, request):
            assert 'demo@example.invalid' not in request.latest_user_text
            return ConsultationResult(
                kind='clarify',
                answer_text='Изменилось потребление тепла или тариф?',
                reason_code='missing_material_context',
            )

        def validate_result(self, result):
            pass

    async def scenario():
        settings = Settings(database_path=tmp_path / 'lead.sqlite3', sync_wait_seconds=1)
        service = Service(Database(settings.database_path), Core(), settings)
        sid = service.create_session()['session']['id']

        async def send(text):
            return await service.post_turn(sid, TurnInput(client_message_id=uuid4(), text=text), str(uuid4()))

        clarification = await send('У нас вырос счет за отопление; почта demo@example.invalid')
        handoff = await send('Свяжите меня с менеджером: нужны показания нестандартного счетчика.')
        assert handoff['decision']['kind'] == 'collecting_contacts'
        contact_reply = await send('Мой телефон 7.999.000.00.00')
        assert contact_reply['lead_draft']['phone'] == '+79990000000'
        preview = service.preview(sid)
        assert preview['status'] == 'partial'
        assert 'нужны показания нестандартного счетчика' in preview['summary']
        assert 'demo@example.invalid' not in preview['summary']
        assert '7.999.000.00.00' not in preview['client_problem']
        assert preview['open_questions'] == [clarification['assistant_message']['text']]
        correction = await send('Исправляю: мой телефон +79991111111')
        assert correction['lead_draft']['phone'] == '+79991111111'
        assert service.preview(sid)['open_questions'] == preview['open_questions']

    asyncio.run(scenario())
