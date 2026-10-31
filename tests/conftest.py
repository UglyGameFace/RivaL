"""Isolated backend coverage for learned-policy evidence and challenger gates."""
import os
import uuid

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
    from psycopg.conninfo import make_conninfo

    schema = 'learning_test_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        store = PostgresHotStore(make_conninfo(dsn, options=f'-c search_path={schema}'))
        try:
            yield store
        finally:
            store.close()
            connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
