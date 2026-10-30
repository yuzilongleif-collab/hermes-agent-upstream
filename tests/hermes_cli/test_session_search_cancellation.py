"""Abandoning a REST search must stop SQLite, not only its asyncio waiter."""
import asyncio
import sqlite3
import threading
import time

import pytest
from fastapi import HTTPException, Request

from hermes_state import SessionDB
from hermes_cli.web_routers import sessions


@pytest.fixture
def store(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session(session_id='synthetic', source='cli', model='test')
    # A legacy store without the optional CJK index must still use canonical LIKE.
    with db._lock:
        assert db._conn is not None
        db._conn.execute('BEGIN')
        db._conn.executemany(
            "INSERT INTO messages(session_id,role,content,timestamp) VALUES(?,?,?,?)",
            [('synthetic', 'user', 'synthetic content', n) for n in range(10000)])
        db._conn.execute('COMMIT')
    yield db
    db.close()


@pytest.mark.parametrize('abandon', ['cancelled', 'deadline', 'disconnect'])
def test_abandoned_search_interrupts_its_sqlite_worker(store, monkeypatch, abandon, record_property):
    entered, release, closed, interrupted = (threading.Event() for _ in range(4))
    readers = []
    original_read = SessionDB._read_all

    def read(self, sql, params=()):
        try:
            return original_read(self, sql, params)
        except sqlite3.OperationalError as exc:
            if getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT:
                interrupted.set()
            raise

    def open_db(profile, *, read_only, read_deadline=None):
        reader = SessionDB(store.db_path, read_only=True)
        assert reader._fts_enabled
        reader._fts_cjk_available = reader._trigram_available = False
        readers.append(reader)
        original_close = reader.close

        def like(pattern, text, escape=None):
            if text == 'synthetic content' and not entered.is_set():
                entered.set()
                assert release.wait(5), 'fixture gate was not released'
            return 0

        def close():
            try:
                original_close()
            finally:
                closed.set()

        assert reader._conn is not None
        reader._conn.create_function('like', 3, like)
        reader.close = close
        return reader

    monkeypatch.setattr(sessions, '_serving_profile', lambda profile: profile or 'default')
    monkeypatch.setattr(sessions, '_open_session_db_for_profile', open_db)
    monkeypatch.setattr(SessionDB, '_read_all', read)
    if abandon == 'deadline':
        from hermes_cli import web_session_search
        monkeypatch.setattr(web_session_search, 'SEARCH_TIMEOUT_SECONDS', 0.1)

    async def exercise():
        received = asyncio.Queue()
        request = Request({'type': 'http'}, receive=received.get) if abandon == 'disconnect' else None
        kwargs = {'request': request} if request is not None else {}
        task = asyncio.create_task(sessions.search_sessions(q='中文', limit=20, **kwargs))
        try:
            ready = await asyncio.to_thread(entered.wait, 5)
            if not ready and task.done():
                task.result()
            assert ready, 'real LIKE query did not start'
            # The abandoned reader must never own the writer's connection/lock.
            assert (await asyncio.to_thread(store.get_session, 'synthetic'))['id'] == 'synthetic'
            await asyncio.to_thread(store.append_message, 'synthetic', role='user', content='concurrent writer')
            if abandon == 'cancelled':
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                if abandon == 'disconnect':
                    await received.put({'type': 'http.disconnect'})
                with pytest.raises(HTTPException) as caught:
                    await asyncio.wait_for(task, 5)
                assert caught.value.status_code == (504 if abandon == 'deadline' else 499)
            release_time = time.monotonic()
            release.set()
            assert await asyncio.to_thread(closed.wait, 5), 'worker outlived the cancelled search'
            record_property('worker_exit_after_udf_release_ms', (time.monotonic() - release_time) * 1000)
            assert interrupted.is_set(), 'SQLite ran to natural completion after caller cancellation'
            # Unrelated readers and the writer stay usable after the cancelled reader closes.
            assert store.get_session('synthetic')['id'] == 'synthetic'
            store.append_message('synthetic', role='user', content='writer sentinel')
        finally:
            release.set()
            for reader in readers:
                if reader._conn is not None:
                    reader._conn.interrupt()  # fixture-only rescue of the unfixed worker
            await asyncio.to_thread(closed.wait, 5)

    asyncio.run(exercise())
