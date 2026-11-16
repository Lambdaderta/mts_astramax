"""Real PostgreSQL checks for shared sessions and transactional application behavior."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient

from app.main import create_app
from app.ml.protocol import ConsultationResult
from app.settings import Settings


class DelayedCore:
    ready = True
    corpus_version = 'postgres-test'

    def __init__(self):
        self.entered = Event()
        self.release = Event()

    async def consult(self, request):
        self.entered.set()
        while not self.release.is_set():
            await asyncio.sleep(0.01)
        return ConsultationResult(kind='clarify', answer_text='Какой период вас интересует?',
                                  reason_code='need_period')

    def validate_result(self, result):
        pass


def create_session(client):
    response = client.post('/api/v1/sessions', json={})
    assert response.status_code == 201, response.text
    payload = response.json()
    return (f"/api/v1/sessions/{payload['session']['id']}",
            {'Authorization': f"Bearer {payload['session_token']}"})


def test_postgres_shared_turn_progress_concurrent_confirmation_and_restart(postgres_url):
    settings = Settings(database_url=postgres_url, sync_wait_seconds=0, review_token='pg-test-review')
    core = DelayedCore()
    payload = {'client_message_id': str(uuid4()), 'text': 'Почему вырос счёт за газ и какой тариф действует?'}
    keywords = ['Газ', 'Рост расходов', 'Тарифы']
    with TestClient(create_app(settings, core)) as first, TestClient(create_app(settings, core)) as second:
        assert first.get('/health/ready').status_code == 200
        path, headers = create_session(first)
        other_path, other_headers = create_session(second)
        assert other_path != path
        query = {'include_progress': 'true'}
        accepted = first.post(path + '/turns', json=payload, headers=headers, params=query)
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()['progress']['keywords'] == keywords
        turn_path = f"{path}/turns/{accepted.json()['turn_id']}"
        try:
            assert core.entered.wait(3)
            polled = second.get(turn_path, headers=headers, params=query)
            assert polled.status_code == 202, polled.text
            assert polled.json()['progress'] == {
                'stage': 'preparing_answer', 'keywords': keywords,
            }
            duplicate = second.post(path + '/turns', json=payload, headers=headers)
            assert duplicate.status_code == 202
            assert duplicate.json()['turn_id'] == accepted.json()['turn_id']
            conflict = second.post(path + '/turns', json={**payload, 'client_message_id': str(uuid4())},
                                   headers=headers)
            assert conflict.status_code == 409
            assert conflict.json()['error']['code'] == 'TURN_IN_PROGRESS'
            assert second.get(turn_path, headers=other_headers, params=query).status_code == 403
        finally:
            core.release.set()

        deadline = monotonic() + 5
        completed = second.get(turn_path, headers=headers)
        while completed.status_code == 202 and monotonic() < deadline:
            sleep(0.02)
            completed = second.get(turn_path, headers=headers)
        assert completed.status_code == 200, completed.text
        assert completed.json()['status'] == 'completed'
        answer_id = completed.json()['assistant_message']['id']
        draft = first.patch(path + '/lead-draft', headers=headers, json={
            'expected_revision': 0, 'fields': {'phone': '+79990000000'},
        })
        assert draft.status_code == 200, draft.text
        preview = first.post(path + '/lead-preview', headers=headers)
        assert preview.status_code == 200, preview.text
        confirmation = {'preview_id': preview.json()['preview_id'], 'idempotency_key': str(uuid4()), 'confirm': True}

        together = Barrier(2)

        def confirm(client):
            together.wait(timeout=3)
            return client.post(path + '/leads', json=confirmation, headers=headers)

        with ThreadPoolExecutor(max_workers=2) as pool:
            confirmations = list(pool.map(confirm, (first, second)))
        assert sorted(response.status_code for response in confirmations) == [200, 201]
        lead_ids = {response.json()['lead']['id'] for response in confirmations}
        assert len(lead_ids) == 1
        lead_id = lead_ids.pop()

    with TestClient(create_app(settings, core)) as restarted:
        history = restarted.get(path + '/messages', headers=headers).json()['items']
        assert len(history) == 3 and history[-1]['id'] == answer_id
        review = {'Authorization': 'Bearer pg-test-review'}
        assert restarted.get('/api/v1/review/leads/' + lead_id, headers=review).status_code == 200
        assert restarted.get('/api/v1/review/traces/' + answer_id, headers=review).status_code == 200


def test_postgres_readiness_detects_missing_required_index(postgres_url):
    settings = Settings(database_url=postgres_url)
    with TestClient(create_app(settings, DelayedCore())) as client:
        assert client.get('/health/ready').status_code == 200
        with psycopg.connect(postgres_url) as connection:
            connection.execute('DROP INDEX one_processing_turn')
        ready = client.get('/health/ready')
        assert ready.status_code == 503 and ready.json()['database'] is False


def test_postgres_persistence_error_is_sanitized_and_rolls_back(postgres_url):
    settings = Settings(database_url=postgres_url, sync_wait_seconds=1)
    core = DelayedCore()
    core.release.set()
    with TestClient(create_app(settings, core)) as client:
        path, headers = create_session(client)
        invalid = client.post(path + '/turns', headers=headers, json={
            'client_message_id': str(uuid4()), 'text': 'invalid\x00text',
        })
        assert invalid.status_code == 422
        payload = {'client_message_id': str(uuid4()), 'text': 'Какой тариф на газ?'}
        with psycopg.connect(postgres_url) as connection:
            connection.execute('''
                CREATE FUNCTION fail_trace() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                  RAISE EXCEPTION 'test-private-error';
                END $$
            ''')
            connection.execute('''
                CREATE TRIGGER reject_trace BEFORE INSERT ON answer_traces
                FOR EACH ROW EXECUTE FUNCTION fail_trace()
            ''')
        response = client.post(path + '/turns', headers=headers, json=payload)
        assert response.status_code == 503, response.text
        assert response.json()['error']['code'] == 'STORAGE_UNAVAILABLE'
        assert 'test-private-error' not in response.text
        messages = client.get(path + '/messages', headers=headers).json()['items']
        assert [message['role'] for message in messages] == ['assistant', 'user']
        with psycopg.connect(postgres_url) as connection:
            connection.execute('DROP TRIGGER reject_trace ON answer_traces')
        retry = client.post(path + '/turns', headers=headers, json=payload)
        assert retry.status_code == 200, retry.text
        messages = client.get(path + '/messages', headers=headers).json()['items']
        assert [message['role'] for message in messages] == ['assistant', 'user', 'assistant']
