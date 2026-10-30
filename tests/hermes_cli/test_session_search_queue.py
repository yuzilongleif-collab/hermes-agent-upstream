"""Abandoned queued searches must not open SQLite; a successor must still work."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest
from fastapi import HTTPException, Request

from hermes_state import SessionDB
from hermes_cli import web_session_search
from hermes_cli.web_routers import sessions


@pytest.mark.parametrize('abandon', ['cancelled', 'disconnect', 'deadline'])
def test_abandoned_queue_drains_without_open_and_successor_works(tmp_path, monkeypatch, abandon):
    path = tmp_path / 'state.db'
    writer = SessionDB(path)
    try:
        writer.create_session(session_id='successor', source='cli', model='test')
        writer.append_message('successor', role='user', content='successor needle')
    finally:
        writer.close()
    opened, closed = [], []
    def opener(profile, *, read_only, read_deadline=None):
        opened.append(profile)
        reader = SessionDB(path, read_only=read_only, read_deadline=read_deadline)
        original_close = reader.close
        def close():
            original_close()
            closed.append(reader._conn is None)
        reader.close = close
        return reader
    monkeypatch.setattr(sessions, '_serving_profile', lambda profile: profile or 'default')
    monkeypatch.setattr(sessions, '_open_session_db_for_profile', opener)
    entered, release = threading.Event(), threading.Event()
    def blocker():
        entered.set()
        assert release.wait(5)

    async def exercise():
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            loop.set_default_executor(pool)
            blocked = loop.run_in_executor(None, blocker)
            assert entered.wait(5)
            submitted = asyncio.Event()
            original_to_thread = asyncio.to_thread
            async def observed_to_thread(fn, *args, **kwargs):
                submitted.set()
                return await original_to_thread(fn, *args, **kwargs)
            monkeypatch.setattr(asyncio, 'to_thread', observed_to_thread)
            received = asyncio.Queue()
            request = Request({'type': 'http'}, receive=received.get) if abandon == 'disconnect' else None
            if abandon == 'deadline':
                monkeypatch.setattr(web_session_search, 'SEARCH_TIMEOUT_SECONDS', 0.05)
            task = asyncio.create_task(sessions.search_sessions(q='abandoned', request=request))
            try:
                await asyncio.wait_for(submitted.wait(), 5)
                if abandon == 'cancelled':
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    if abandon == 'disconnect':
                        await received.put({'type': 'http.disconnect'})
                    with pytest.raises(HTTPException) as caught:
                        await asyncio.wait_for(task, 5)
                    assert caught.value.status_code == (499 if abandon == 'disconnect' else 504)
                    if abandon == 'deadline':
                        assert caught.value.detail['code'] == 'session_search_timeout'
                assert opened == []
            finally:
                release.set()
                await blocked
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            # FIFO executor barrier: the abandoned worker has actually passed its entry gate.
            await original_to_thread(lambda: None)
            assert opened == []
            monkeypatch.setattr(web_session_search, 'SEARCH_TIMEOUT_SECONDS', 25.0)
            result = await sessions.search_sessions(q='successor', profile='next-profile', limit=1)
            assert [row['session_id'] for row in result['results']] == ['successor']
            assert result['results'][0]['profile'] == 'next-profile'
            assert opened == ['next-profile']
            assert closed == [True]
    asyncio.run(exercise())
