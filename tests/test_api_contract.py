"""HTTP boundary tests separate from consultation and storage behavior."""
import json
import logging
import pytest
import yaml
from fastapi.testclient import TestClient
from app.main import create_app
from app.settings import Settings


def test_generated_openapi_matches_implementation():
    from pathlib import Path
    assert yaml.safe_load(Path('openapi.yaml').read_text()) == create_app().openapi()


def test_body_limit_and_validation_do_not_echo_input(tmp_path):
    with TestClient(create_app(Settings(database_path=tmp_path/'db'))) as client:
        response=client.post('/api/v1/sessions',content=b'x'*33000,headers={'Content-Type':'application/json'})
        assert response.status_code==413
        assert response.json()['error']['code']=='BODY_TOO_LARGE'
        response=client.post('/api/v1/sessions',json={'secret_test_input':'must not leak'})
        assert response.status_code==422
        assert 'must not leak' not in response.text
        assert response.headers['X-Request-ID']==response.json()['error']['request_id']


def test_rate_limit_and_unknown_endpoint_use_error_contract(tmp_path):
    with TestClient(create_app(Settings(database_path=tmp_path/'db',requests_per_minute=2))) as client:
        assert client.post('/api/v1/sessions',json={}).status_code==201
        assert client.post('/api/v1/sessions',json={}).status_code==201
        response=client.post('/api/v1/sessions',json={})
        assert response.status_code==429
        assert response.headers['Retry-After']=='60'
        response=client.get('/unknown')
        assert response.status_code==404
        assert response.json()['error']['request_id']==response.headers['X-Request-ID']


def test_review_and_cors_are_closed_by_default(tmp_path):
    with TestClient(create_app(Settings(database_path=tmp_path/'db'))) as client:
        response=client.get('/api/v1/review/sessions',headers={'Authorization':'Bearer anything'})
        assert response.status_code==403
        response=client.options('/api/v1/sessions',headers={'Origin':'https://unapproved.invalid','Access-Control-Request-Method':'POST'})
        assert 'access-control-allow-origin' not in response.headers


def test_model_opt_in_requires_valid_config(tmp_path,monkeypatch):
    for key in ('LLM_BASE_URL','OPENAI_BASE_URL','LLM_MODEL','OPENAI_MODEL','GEN_MODEL','LLM_API_KEY','OPENAI_API_KEY'):
        monkeypatch.delenv(key,raising=False)
    settings=Settings(database_path=tmp_path/'db',llm_enabled=True)
    with pytest.raises(ValueError):
        with TestClient(create_app(settings)):
            pass


@pytest.mark.parametrize('setting', ['turn_lease_seconds', 'sync_wait_seconds'])
@pytest.mark.parametrize('value', [float('nan'), float('inf')])
def test_turn_time_configuration_must_be_finite(setting, value):
    with pytest.raises(ValueError):
        Settings(**{setting: value})


def test_http_events_include_route_without_request_content(tmp_path, caplog):
    settings = Settings(database_path=tmp_path / 'db', max_body_bytes=16)
    with caplog.at_level(logging.INFO, logger='uvicorn.error'), TestClient(create_app(settings)) as client:
        created = client.post('/api/v1/sessions', json={}).json()
        session_id, token = created['session']['id'], created['session_token']
        headers = {'Authorization': f'Bearer {token}'}
        response = client.get(f'/api/v1/sessions/{session_id}?contact=private-input', headers=headers)
        rejected = client.post('/api/v1/sessions', content=b'private-body-input', headers=headers)

    events = [json.loads(record.getMessage()) for record in caplog.records if record.getMessage().startswith('{')]
    request = next(event for event in events if event['request_id'] == response.headers['X-Request-ID'])
    assert request['route'] == '/api/v1/sessions/{session_id}'
    assert request['status'] == 200
    body_error = next(event for event in events if event['request_id'] == rejected.headers['X-Request-ID'])
    assert body_error['error_code'] == 'BODY_TOO_LARGE'
    for private in (token, session_id, 'private-input', 'private-body-input'):
        assert private not in caplog.text
