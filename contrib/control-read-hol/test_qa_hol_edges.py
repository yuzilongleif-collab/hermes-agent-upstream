"""Independent QA-only probes: real authority/handlers; in-memory WS adapter disclosed."""
import asyncio
import threading

import pytest
from tests.waiting import async_wait_until

pytest_plugins = ('tests.gateway.test_sibling_attach_while_profile_starting',)


def send(sock, rid, method, **params):
    sock.inbound.put_nowait({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params})


async def create(rig, sock, profile, rid):
    rig.create(sock, profile, rid)
    frame = await sock.reply(rid)
    assert 'result' in frame, frame
    return frame['result']


@pytest.mark.asyncio
async def test_secondary_control_crosses_real_default_export(rig, monkeypatch):
    sock = await rig.connect()
    a = await create(rig, sock, 'default', 'a')
    b = await create(rig, sock, 'alpha', 'b')
    send(sock, 'warm', 'session.control.read', profile='alpha', session_id=b['session_id'])
    expected = await sock.reply('warm')
    assert 'result' in expected
    primary = rig.runner.session_authorities.for_home(rig.runner.session_authority.profile_id)
    original = primary.db.get_session
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()

    def gated(sid, *args, **kwargs):
        if sid == a['session_id'] and threading.current_thread() is not threading.main_thread():
            entered.set()
            try:
                assert release.wait(10)
            finally:
                exited.set()
        return original(sid, *args, **kwargs)

    monkeypatch.setattr(primary.db, 'get_session', gated)
    send(sock, 'export', 'session.export', session_id=a['session_id'], format='json')
    try:
        await async_wait_until(entered.is_set, timeout=5)
        send(sock, 'secondary', 'session.control.read', profile='alpha', session_id=b['session_id'])
        answer = await sock.reply('secondary', timeout=3)
        assert answer['result'] == expected['result']
        assert 'export' not in sock.replies and not release.is_set()
        send(sock, 'ping-independent', 'gateway.ping')
        assert (await sock.reply('ping-independent'))['result']['ok']
    finally:
        release.set()
    assert 'result' in await sock.reply('export')
    assert exited.is_set()


@pytest.mark.asyncio
async def test_same_session_early_reads_do_not_overlap(rig, monkeypatch):
    from gateway import session_ancillary as ancillary
    sock = await rig.connect()
    a = await create(rig, sock, 'alpha', 'a')
    original = ancillary.read
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def gated(connection, ref, params, **kwargs):
        calls.append(ref.session_id)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return await original(connection, ref, params, **kwargs)

    monkeypatch.setattr(ancillary, 'read', gated)
    send(sock, 'first', 'session.control.read', profile='alpha', session_id=a['session_id'])
    await asyncio.wait_for(entered.wait(), 5)
    send(sock, 'second', 'session.control.read', profile='alpha', session_id=a['session_id'])
    send(sock, 'ping-ordered', 'gateway.ping')
    assert (await sock.reply('ping-ordered'))['result']['ok']
    second = asyncio.create_task(sock.reply('second'))
    try:
        done, _ = await asyncio.wait({second}, timeout=2)
        assert not done and len(calls) == 1
    finally:
        release.set()
    first, other = await asyncio.gather(sock.reply('first'), second)
    assert 'result' in first and first['result'] == other['result']
    assert sock.order.index('first') < sock.order.index('second')


@pytest.mark.asyncio
async def test_full_run_slots_disconnect_drops_waiter_and_releases_successor(rig, monkeypatch):
    from gateway import session_ancillary as ancillary
    from gateway.session_controls import AuthorityConnection
    sock = await rig.connect()
    rows = [await create(rig, sock, 'alpha', 'create-' + str(i)) for i in range(3)]
    original_read, original_close = ancillary.read, AuthorityConnection.close
    two_started, release, closing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls, cancelled = [], []

    async def gated(connection, ref, params, **kwargs):
        calls.append(ref.session_id)
        if len(calls) == 2:
            two_started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.append(ref.session_id)
            raise
        return await original_read(connection, ref, params, **kwargs)

    async def closing_observer(connection):
        closing.set()
        return await original_close(connection)

    monkeypatch.setattr(ancillary, 'read', gated)
    monkeypatch.setattr(AuthorityConnection, 'close', closing_observer)
    for i in range(2):
        send(sock, 'running-' + str(i), 'session.control.read', profile='alpha', session_id=rows[i]['session_id'])
    await asyncio.wait_for(two_started.wait(), 5)
    send(sock, 'waiting', 'session.control.read', profile='alpha', session_id=rows[2]['session_id'])
    send(sock, 'successor', 'session.mutate', profile='alpha', session_id=rows[2]['session_id'],
         request_id='qa-rename', expected_revision=rows[2]['revision'],
         operation='rename', payload={'title': 'QA_DRAIN_SUCCESSOR'})
    send(sock, 'ping-before-leave', 'gateway.ping')
    assert (await sock.reply('ping-before-leave'))['result']['ok']
    sock.inbound.put_nowait(None)
    # Native read loop observes disconnect; it must cancel only the unstarted third read.
    done, _ = await asyncio.wait({sock.task}, timeout=2)
    assert not done and not closing.is_set() and len(calls) == 2 and not cancelled
    release.set()
    await asyncio.wait_for(sock.task, 10)
    assert closing.is_set() and len(calls) == 2 and not cancelled
    assert 'waiting' not in sock.replies
    assert 'result' in sock.replies['successor'], sock.replies
    owner = rig.runner.session_authorities.for_home(rig.alpha)
    assert owner.db.get_session(rows[2]['session_id'])['title'] == 'QA_DRAIN_SUCCESSOR'


@pytest.mark.asyncio
async def test_failed_early_reply_drains_real_unrelated_export(rig, monkeypatch):
    from gateway import session_tui_ops
    export_handler = session_tui_ops.export_session
    completion, cancellation = [], []

    async def observed_export(*args, **kwargs):
        try:
            result = await export_handler(*args, **kwargs)
            completion.append(result)
            return result
        except asyncio.CancelledError:
            cancellation.append(True)
            raise

    monkeypatch.setattr(session_tui_ops, 'export_session', observed_export)
    sock = await rig.connect()
    a = await create(rig, sock, 'alpha', 'a')
    b = await create(rig, sock, 'alpha', 'b')
    owner = rig.runner.session_authorities.for_home(rig.alpha)
    original_read, original_send = owner.db.get_session, sock.send_text
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    failed = asyncio.Event()

    def gated(sid, *args, **kwargs):
        if sid == a['session_id'] and threading.current_thread() is not threading.main_thread():
            entered.set()
            try:
                assert release.wait(10)
            finally:
                exited.set()
        return original_read(sid, *args, **kwargs)

    async def failing_send(line):
        import json
        if json.loads(line).get('id') == 'read-fails':
            failed.set()
            raise RuntimeError('synthetic peer write failure')
        return await original_send(line)

    monkeypatch.setattr(owner.db, 'get_session', gated)
    monkeypatch.setattr(sock, 'send_text', failing_send)
    send(sock, 'export-survives', 'session.export', profile='alpha', session_id=a['session_id'], format='json')
    try:
        await async_wait_until(entered.is_set, timeout=5)
        send(sock, 'read-fails', 'session.control.read', profile='alpha', session_id=b['session_id'])
        await asyncio.wait_for(failed.wait(), 5)
        assert not sock.task.done() and not exited.is_set()
    finally:
        release.set()
    await asyncio.wait_for(sock.task, 10)
    assert exited.is_set()
    assert len(completion) == 1 and completion[0]['format'] == 'json' and completion[0]['content']
    assert not cancellation
    # The transport is closed by the first write failure; no second reply is promised.
    assert 'export-survives' not in sock.replies
