"""Regression tests for durable turn ownership, API errors and customer facts."""
import asyncio
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4

from fastapi.testclient import TestClient

from app.contracts import TurnInput
from app.main import create_app
from app.ml.protocol import ConsultationResult, ConsultationUnavailable
from app.services import Service
from app.settings import Settings
from app.storage import Database


class RecordingCore:
    ready = True
    corpus_version = 'synthetic-v1'

    def __init__(self):
        self.requests = []

    async def consult(self, request):
        self.requests.append(request)
        return ConsultationResult(kind='clarify', answer_text='Что нужно уточнить?',
                                  reason_code='missing_material_context')

    def validate_result(self, result):
        pass


def test_v1_database_upgrade_preserves_history_and_enables_facts(tmp_path):
    path = tmp_path / 'db.sqlite3'
    schema = (Path(__file__).resolve().parents[1] / 'schema.sql').read_text()
    v1_schema = schema.split('CREATE TABLE IF NOT EXISTS facts (', 1)[0]
    with sqlite3.connect(path) as conn:
        conn.executescript(v1_schema + 'PRAGMA user_version=1; COMMIT;')
        conn.execute("INSERT INTO sessions VALUES ('old','hash','active','new',1,'stamp',NULL)")
        conn.execute("INSERT INTO messages VALUES ('greeting','old',1,'assistant','Старое сообщение','stamp')")
    first = Database(path)
    assert first.ready()
    restarted = Database(path)
    with restarted.read() as conn:
        assert conn.execute("SELECT text FROM messages WHERE session_id='old'").fetchone()[0] == 'Старое сообщение'
        assert conn.execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 0


def test_readiness_detects_missing_schema_and_invalid_json_is_400(tmp_path):
    settings = Settings(database_path=tmp_path / 'db.sqlite3')
    with TestClient(create_app(settings, RecordingCore())) as client:
        assert client.get('/health/ready').status_code == 200
        invalid = client.post('/api/v1/sessions', content=b'{"synthetic secret":',
                              headers={'Content-Type': 'application/json'})
        assert invalid.status_code == 400
        assert invalid.json()['error']['code'] == 'BAD_REQUEST'
        assert 'synthetic secret' not in invalid.text
        with sqlite3.connect(settings.database_path) as conn:
            conn.execute('DROP INDEX one_processing_turn')
        response = client.get('/health/ready')
        assert response.status_code == 503
        assert response.json() == {'status':'not_ready', 'database':False, 'knowledge':True,
                                   'corpus_version':'synthetic-v1'}
        assert client.get('/health/live').status_code == 200


def test_turn_poll_does_not_wait_for_an_unrelated_writer(tmp_path):
    settings = Settings(database_path=tmp_path / 'db.sqlite3')
    service = Service(Database(settings.database_path), RecordingCore(), settings)
    sid = service.create_session()['session']['id']
    turn, _ = service.accept_turn(sid, TurnInput(client_message_id=uuid4(), text='Учет воды'))
    finished = threading.Event()
    result = {}

    def poll():
        result.update(service.get_turn(sid, turn['id'], str(uuid4())))
        finished.set()

    with service.db.transaction():
        worker = threading.Thread(target=poll)
        worker.start()
        returned_before_unlock = finished.wait(1)
    worker.join(5)
    assert returned_before_unlock
    assert result == {'turn_id':turn['id'], 'status':'processing', 'poll_after_ms':700}


def test_lost_lease_fences_stale_worker_after_retry(tmp_path):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        class DelayedCore(RecordingCore):
            async def consult(self, request):
                entered.set()
                await release.wait()
                return ConsultationResult(kind='clarify', answer_text='Устаревший ответ', reason_code='stale')

        settings = Settings(database_path=tmp_path / 'db.sqlite3', sync_wait_seconds=0)
        slow = Service(Database(settings.database_path), DelayedCore(), settings)
        retry = Service(Database(settings.database_path), RecordingCore(), settings)
        sid = slow.create_session()['session']['id']
        request_id = str(uuid4())
        body = TurnInput(client_message_id=uuid4(), text='Учет воды')
        accepted = await slow.post_turn(sid, body, request_id)
        await asyncio.wait_for(entered.wait(), 2)
        try:
            with slow.db.transaction() as conn:
                conn.execute('UPDATE turns SET lease_until=0 WHERE id=?', (accepted['turn_id'],))
            assert retry.get_turn(sid, accepted['turn_id'], request_id)['error']['code'] == 'INTERRUPTED'
            retry_turn, started = retry.accept_turn(sid, body)
            assert started and retry_turn['attempt'] == 2
            await retry.run_turn(retry_turn, request_id)
        finally:
            release.set()
            await asyncio.gather(*slow.tasks)
        result = retry.get_turn(sid, accepted['turn_id'], request_id)
        assert result['assistant_message']['text'] == 'Что нужно уточнить?'
        assert len(retry.messages(sid, 0, 10)['items']) == 3
        with retry.db.read() as conn:
            assert conn.execute('SELECT COUNT(*) FROM answer_traces').fetchone()[0] == 1

    asyncio.run(scenario())


def test_rejected_evidence_has_safe_review_trace(tmp_path):
    class BadEvidence(RecordingCore):
        async def consult(self, request):
            return ConsultationResult(kind='answer', answer_text='Неподтвержденная цена 987654321 рублей',
                                      retrieval_query='private rejected query', reason_code='grounded_answer')

    settings = Settings(database_path=tmp_path / 'db.sqlite3', review_token='test-review')
    with TestClient(create_app(settings, BadEvidence())) as client:
        created = client.post('/api/v1/sessions', json={}).json()
        result = client.post(f"/api/v1/sessions/{created['session']['id']}/turns",
                             headers={'Authorization':f"Bearer {created['session_token']}"},
                             json={'client_message_id':str(uuid4()), 'text':'Сколько стоит продукт?'})
        assert result.status_code == 200, result.text
        data = result.json()
        assert data['decision'] == {'kind':'insufficient_knowledge','reason_code':'invalid_evidence'}
        assert '987654321' not in result.text
        trace = client.get(f"/api/v1/review/traces/{data['assistant_message']['id']}",
                           headers={'Authorization':'Bearer test-review'}).json()
        assert trace['decision'] == data['decision']
        assert trace['evidence'] == trace['source_ids'] == []
        assert trace['retrieval_query'] == ''


def test_customer_facts_follow_topic_survive_failure_and_preserve_correction(tmp_path):
    class FailingCore(RecordingCore):
        async def consult(self, request):
            raise ConsultationUnavailable('synthetic failure')

    async def scenario():
        settings = Settings(database_path=tmp_path / 'db.sqlite3')
        core = RecordingCore()
        service = Service(Database(settings.database_path), core, settings)
        sid = service.create_session()['session']['id']

        async def send(service, text):
            turn, started = service.accept_turn(sid, TurnInput(client_message_id=uuid4(), text=text))
            assert started
            await service.run_turn(turn, str(uuid4()))
            return turn['user_message_id']

        first = await send(service, 'У нас 20 счетчиков электроэнергии.')
        failed = Service(service.db, FailingCore(), settings)
        correction = await send(failed, 'Исправляю, не 20, а 30 счетчиков.')
        water = await send(service, 'По воде у нас 5 счётчиков.')
        assert [(fact.topic_key, fact.value, fact.source_message_id) for fact in core.requests[-1].facts] == [('water',5,water)]
        restarted = Service(Database(settings.database_path), core, settings)
        await send(restarted, 'Что потребуется для учета электроэнергии?')
        assert [(fact.topic_key, fact.value, fact.source_message_id) for fact in core.requests[-1].facts] == [('electricity',30,correction)]
        await send(restarted, 'Если бы у нас было 99 счетчиков воды, что бы изменилось?')
        assert [fact.value for fact in core.requests[-1].facts] == [5]
        with service.db.read() as conn:
            history = conn.execute('SELECT source_message_id, superseded_at FROM facts ORDER BY id').fetchall()
        assert [row['source_message_id'] for row in history] == [first, correction, water]
        assert history[0]['superseded_at'] and history[1]['superseded_at'] is None
        assert {fact['value'] for fact in restarted.preview(sid)['facts']} == {5,30}
        fresh = restarted.create_session()['session']['id']
        assert restarted.preview(fresh)['facts'] == []

    asyncio.run(scenario())
