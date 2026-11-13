import asyncio
from threading import Event
from uuid import uuid4

from fastapi.testclient import TestClient

from app.contracts import TurnInput, TurnProcessing
from app.main import create_app
from app.ml.protocol import ConsultationResult
from app.services import Service
from app.settings import Settings
from app.storage import Database


class DelayedCore:
    corpus_version = 'synthetic-v1'

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def consult(self, request):
        self.entered.set()
        await self.release.wait()
        return ConsultationResult(kind='clarify', answer_text='Уточните задачу.', reason_code='synthetic')

    def validate_result(self, result):
        pass


def test_progress_is_opt_in_safe_and_visible_to_other_workers(tmp_path):
    class PausedService(Service):
        context_entered = Event()
        context_release = Event()
        storage_entered = Event()
        storage_release = Event()

        def turn_context(self, *args):
            self.context_entered.set()
            self.context_release.wait(5)
            return super().turn_context(*args)

        def complete_turn(self, *args):
            self.storage_entered.set()
            self.storage_release.wait(5)
            return super().complete_turn(*args)

    async def scenario():
        settings = Settings(database_path=tmp_path / 'progress.sqlite3')
        core = DelayedCore()
        worker = PausedService(Database(settings.database_path), core, settings)
        reader = Service(Database(settings.database_path), core, settings)
        sid = worker.create_session()['session']['id']
        body = TurnInput(client_message_id=uuid4(), text=(
            'Какой тариф на воду и какие счётчики подходят для продукта СуперИкс? '
            'Компания: Газ и Тепло; меня зовут Электрик; телефон: +7 912 345-67-89.'
        ))
        request_id = str(uuid4())
        turn, _ = worker.accept_turn(sid, body)
        tid = turn['id']
        assert reader.get_turn(sid, tid, request_id, include_progress=True)['progress'] == {
            'stage': 'queued', 'keywords': [],
        }
        task = asyncio.create_task(worker.run_turn(turn, request_id))
        try:
            assert await asyncio.to_thread(worker.context_entered.wait, 2)
            assert reader.get_turn(sid, tid, request_id, include_progress=True)['progress'] == {
                'stage': 'analyzing', 'keywords': [],
            }
            worker.context_release.set()
            await asyncio.wait_for(core.entered.wait(), 2)
            assert reader.get_turn(sid, tid, request_id) == {
                'turn_id': tid, 'status': 'processing', 'poll_after_ms': 700,
            }
            progress = reader.get_turn(sid, tid, request_id, include_progress=True)
            assert TurnProcessing.model_validate(progress).model_dump()['progress'] == {
                'stage': 'preparing_answer', 'keywords': ['Вода', 'Счётчики', 'Тарифы'],
            }
            repeated = await reader.post_turn(sid, body, request_id, include_progress=True)
            assert repeated == progress
            core.release.set()
            assert await asyncio.to_thread(worker.storage_entered.wait, 2)
            assert reader.get_turn(sid, tid, request_id, include_progress=True)['progress'] == {
                'stage': 'finalizing', 'keywords': ['Вода', 'Счётчики', 'Тарифы'],
            }
        finally:
            worker.context_release.set()
            core.release.set()
            worker.storage_release.set()
            await task
        completed = reader.get_turn(sid, tid, request_id, include_progress=True)
        assert completed['status'] == 'completed'
        assert 'progress' not in completed

    asyncio.run(scenario())


def test_retry_resets_progress_and_fences_the_previous_attempt(tmp_path):
    settings = Settings(database_path=tmp_path / 'progress.sqlite3')
    service = Service(Database(settings.database_path), DelayedCore(), settings)
    sid = service.create_session()['session']['id']
    body = TurnInput(client_message_id=uuid4(), text='Учёт воды')
    turn, _ = service.accept_turn(sid, body)
    tid = turn['id']
    assert service.update_turn_progress(turn, 'preparing_answer', ['Вода'])
    with service.db.transaction(session_id=sid) as conn:
        conn.execute('UPDATE turns SET lease_until=0 WHERE id=?', (tid,))
    retry, started = service.accept_turn(sid, body)
    assert started and retry['attempt'] == 2
    assert not service.update_turn_progress(turn, 'finalizing', ['Вода'])
    assert service.get_turn(sid, tid, str(uuid4()), include_progress=True)['progress'] == {
        'stage': 'queued', 'keywords': [],
    }


def test_http_progress_is_opt_in_on_post_and_poll(tmp_path):
    class PausedCore(DelayedCore):
        def __init__(self):
            self.entered = Event()
            self.release = Event()

        async def consult(self, request):
            self.entered.set()
            await asyncio.to_thread(self.release.wait, 5)
            return ConsultationResult(kind='clarify', answer_text='Уточните задачу.', reason_code='synthetic')

    settings = Settings(database_path=tmp_path / 'progress.sqlite3', sync_wait_seconds=0)
    core = PausedCore()
    with TestClient(create_app(settings, core)) as client:
        session = client.post('/api/v1/sessions', json={}).json()
        url = f"/api/v1/sessions/{session['session']['id']}/turns"
        headers = {'Authorization': f"Bearer {session['session_token']}"}
        body = {'client_message_id': str(uuid4()), 'text': 'Тариф на газ'}
        try:
            posted = client.post(url, params={'include_progress': 'true'}, headers=headers, json=body)
            assert posted.status_code == 202
            assert 'progress' in posted.json()
            assert core.entered.wait(2)
            poll = f"{url}/{posted.json()['turn_id']}"
            legacy = client.get(poll, headers=headers)
            assert legacy.status_code == 202
            assert legacy.json() == {
                'turn_id': posted.json()['turn_id'], 'status': 'processing', 'poll_after_ms': 700,
            }
            updated = client.get(poll, params={'include_progress': 'true'}, headers=headers)
            assert updated.status_code == 202
            assert updated.json()['progress'] == {'stage': 'preparing_answer', 'keywords': ['Газ', 'Тарифы']}
        finally:
            core.release.set()
