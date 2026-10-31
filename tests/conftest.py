"""Isolated backend coverage for learned-policy evidence and challenger gates."""
import os
import uuid
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import pytest

from parlay_bot.storage.hot import PostgresHotStore, SQLiteHotStore


@pytest.fixture(params=['sqlite', 'postgres'])
def learning_store(request, tmp_path):
    if request.param == 'sqlite':
        store = SQLiteHotStore(tmp_path/'learning.sqlite')
        try:
            yield store
        finally:
            store.close()
        return
    dsn = os.getenv('RIVAL_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('PostgreSQL CI service required')
    import psycopg
    from psycopg import sql

    schema = 'learning_test_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        parts = urlsplit(dsn)
        query = dict(parse_qsl(parts.query))
        query['options'] = (query.get('options', '') + f' -c search_path={schema}').strip()
        # libpq URI query parameters require %20 for spaces. urlencode()
        # defaults to '+', which libpq treats literally in 'options'.
        scoped_url = urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))
        store = None
        try:
            store = PostgresHotStore(scoped_url)
            yield store
        finally:
            if store is not None:
                store.close()
            connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
