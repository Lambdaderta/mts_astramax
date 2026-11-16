import os
from urllib.parse import parse_qsl, urlencode, urlsplit
from uuid import uuid4

import pytest


@pytest.fixture
def postgres_url():
    """Use a dedicated, disposable schema in the configured test server."""
    server_url = os.getenv('TEST_POSTGRES_URL')
    if not server_url:
        pytest.skip('Set TEST_POSTGRES_URL to run PostgreSQL integration tests')

    import psycopg
    from psycopg import sql

    schema = 'energytool_test_' + uuid4().hex
    parts = urlsplit(server_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query['options'] = (query.get('options', '') + f' -csearch_path={schema}').strip()
    # urlunsplit can drop // for a URI with no host (Unix socket DSNs).
    scoped_url = f'{parts.scheme}://{parts.netloc}{parts.path}?{urlencode(query)}'
    with psycopg.connect(server_url, autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        try:
            yield scoped_url
        finally:
            connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
