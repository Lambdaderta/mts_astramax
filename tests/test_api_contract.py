"""HTTP boundary tests separate from consultation and storage behavior."""
from dataclasses import replace
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
