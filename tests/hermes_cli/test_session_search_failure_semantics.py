"""Search failure semantics and deadline boundaries, using isolated SQLite stores."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import sqlite3
import threading
import time

import pytest
from fastapi import HTTPException

from hermes_state import SessionDB
from hermes_cli import web_server_sessions, web_session_search
from hermes_cli.web_routers import sessions


@pytest.fixture
def db(tmp_path):
    writer = SessionDB(tmp_path / 'state.db')
    writer.create_session(session_id='needle-session', source='cli', model='test')
    writer.append_message('needle-session', role='user', content='needle 中文 material')
    yield writer
    writer.close()


@pytest.fixture
def interrupted():
    with sqlite3.connect(':memory:') as conn:
        conn.set_progress_handler(lambda: 1, 1)
        try:
            conn.execute('WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM x WHERE n<100000) SELECT sum(n) FROM x').fetchone()
        except sqlite3.OperationalError as exc:
            assert exc.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
            return exc
    pytest.fail('SQLite did not interrupt')


@pytest.mark.parametrize('lane', ['ascii', 'cjk', 'gap', 'context', 'stale'])
def test_interrupt_is_never_an_empty_or_fallback_search(db, monkeypatch, interrupted, lane):
    def fail(*args, **kwargs):
        raise interrupted

    if lane in ('ascii', 'cjk'):
        monkeypatch.setattr(db, '_read_all', fail)
        if lane == 'cjk':
            db._fts_cjk_available = True
        run = lambda: db.search_messages('中文' if lane == 'cjk' else 'needle', fields=('id',))
    elif lane == 'gap':
        monkeypatch.setattr(db, 'fts_rebuild_status', lambda: {'indexed': 0, 'total': 10})
        monkeypatch.setattr(db, '_search_unindexed_gap', fail)
        run = lambda: db.search_messages('needle', limit=10, fields=('id',))
    elif lane == 'context':
        @contextmanager
        def failing_context():
            raise interrupted
            yield
        monkeypatch.setattr(db, '_read_ctx', failing_context)
        run = lambda: db._finalize_search_matches([{'id': 1}])
    else:
        monkeypatch.setattr(db, '_read_one', fail)
        run = db._refresh_fts_stale_state
    with pytest.raises(sqlite3.OperationalError) as caught:
        run()
    assert caught.value is interrupted


@pytest.mark.parametrize('query', ['needle', '中文'])
def test_readonly_corruption_never_enters_writer_recovery(db, monkeypatch, query):
    reader = SessionDB(db.db_path, read_only=True)
    reader._fts_cjk_available = True
    corruption = sqlite3.DatabaseError('database disk image is malformed')
    def corrupt(*args, **kwargs):
        raise corruption
    def forbidden(*args, **kwargs):
        pytest.fail('read-only search entered writer recovery')
    monkeypatch.setattr(reader, '_read_all', corrupt)
    monkeypatch.setattr(reader, '_enter_fts_fail_open', forbidden)
    try:
        with pytest.raises(sqlite3.DatabaseError) as caught:
            reader.search_messages(query, fields=('id',))
        assert caught.value is corruption
    finally:
        reader.close()


@pytest.mark.parametrize('stage', ['id', 'content', 'lineage', 'rich', 'title'])
def test_http_busy_is_503_instead_of_success_empty(db, monkeypatch, stage):
    # Fault injection uses SQLite's stable error code, not a string-only oracle.
    busy = sqlite3.OperationalError('database is locked')
    busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
    def fail(*args, **kwargs):
        raise busy
    def opener(profile, *, read_only, read_deadline=None):
        reader = SessionDB(db.db_path, read_only=True, read_deadline=read_deadline)
        names = {'id': 'search_sessions_by_id', 'content': 'search_messages',
                 'lineage': 'get_session', 'rich': 'get_session_rich_row'}
        if stage == 'title':
            monkeypatch.setattr(reader, 'search_sessions_by_id', lambda *a, **k: [])
            monkeypatch.setattr(reader, 'search_messages', lambda *a, **k: [])
            monkeypatch.setattr(reader, 'list_sessions_rich', fail)
        else:
            monkeypatch.setattr(reader, names[stage], fail)
        return reader
    monkeypatch.setattr(sessions, '_serving_profile', lambda profile: profile or 'default')
    monkeypatch.setattr(sessions, '_open_session_db_for_profile', opener)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(sessions.search_sessions(q='needle'))
    assert caught.value.status_code == 503


def test_expired_executor_queue_never_opens_a_database(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    opens = []
    def blocked():
        entered.set()
        assert release.wait(5)
    def forbidden(*args, **kwargs):
        opens.append(True)
        pytest.fail('expired queued search opened SQLite')
    monkeypatch.setattr(sessions, '_serving_profile', lambda profile: 'default')
    monkeypatch.setattr(sessions, '_open_session_db_for_profile', forbidden)
    monkeypatch.setattr(web_session_search, 'SEARCH_TIMEOUT_SECONDS', 0.05)
    async def exercise():
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            loop.set_default_executor(pool)
            blocker = loop.run_in_executor(None, blocked)
            assert entered.wait(5)
            try:
                with pytest.raises(HTTPException) as caught:
                    await sessions.search_sessions(q='needle')
                assert caught.value.status_code == 504
                assert caught.value.detail['code'] == 'session_search_timeout'
            finally:
                release.set()
                await blocker
            await asyncio.to_thread(lambda: None)  # queued search has now passed its worker gate
            assert opens == []
    asyncio.run(exercise())


def test_expired_readonly_open_checks_before_connect(db, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('expired RO open called connect')
    monkeypatch.setattr(SessionDB, '_connect_read_only', forbidden)
    with pytest.raises(TimeoutError):
        SessionDB(db.db_path, read_only=True, read_deadline=time.monotonic() - 1)


def test_search_schema_drift_is_read_only_and_unavailable(db, monkeypatch):
    import hermes_state_reconcile
    with db._lock:
        db._conn.execute('DROP TABLE async_delegations')
    def forbidden(*args, **kwargs):
        pytest.fail('search attempted schema reconciliation')
    monkeypatch.setattr(hermes_state_reconcile, 'reconcile_drift_for_browse', forbidden)
    with pytest.raises(HTTPException) as caught:
        web_server_sessions._open_session_db_at_path(
            db.db_path, read_only=True, read_deadline=time.monotonic() + 5)
    assert caught.value.status_code == 503
    assert db._read_one("SELECT 1 FROM sqlite_master WHERE name='async_delegations'") is None


def test_normal_http_empty_and_cjk_result_remain_distinct(db, monkeypatch):
    monkeypatch.setattr(sessions, '_serving_profile', lambda profile: profile or 'default')
    monkeypatch.setattr(web_server_sessions, '_session_db_path_for_profile', lambda profile: db.db_path)
    assert asyncio.run(sessions.search_sessions(q='totallyabsent')) == {'results': []}
    response = asyncio.run(sessions.search_sessions(q='中文', source='cli', limit=1))
    assert len(response['results']) == 1
    assert response['results'][0]['session_id'] == 'needle-session'
    assert response['results'][0]['profile'] == 'default'


@pytest.mark.parametrize('kind', ['io', 'corrupt', 'fts_corrupt', 'unknown'])
def test_mounted_http_failure_is_not_an_empty_success(db, monkeypatch, kind):
    import httpx
    from fastapi import FastAPI
    def opener(profile, *, read_only, read_deadline=None):
        reader = SessionDB(db.db_path, read_only=True, read_deadline=read_deadline)
        error = (sqlite3.OperationalError('disk I/O error') if kind == 'io' else
                 sqlite3.DatabaseError('database disk image is malformed') if kind == 'corrupt' else
                 sqlite3.DatabaseError('fts5: corrupt structure record') if kind == 'fts_corrupt' else
                 ValueError('synthetic private error'))
        if kind == 'fts_corrupt':
            error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT_VTAB
        def fail(*args, **kwargs):
            raise error
        monkeypatch.setattr(reader, 'search_messages', fail)
        return reader
    monkeypatch.setattr(sessions, '_serving_profile', lambda profile: 'default')
    monkeypatch.setattr(sessions, '_open_session_db_for_profile', opener)
    app = FastAPI()
    app.include_router(sessions.search_router)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            blank = await client.get('/api/sessions/search')
            assert blank.status_code == 200 and blank.json() == {'results': []}
            response = await client.get('/api/sessions/search', params={'q': 'needle'})
            assert response.status_code == (500 if kind == 'unknown' else 503)
            assert 'results' not in response.json()
            assert 'synthetic private error' not in response.text
    asyncio.run(exercise())
