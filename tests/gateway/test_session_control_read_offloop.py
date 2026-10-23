"""Canonical control polls wait for lawful locks without parking the owner event loop."""
import asyncio
from dataclasses import replace
import threading
from types import SimpleNamespace

import pytest
import pytest_asyncio

from gateway.session_contract import SessionRef
from gateway.session_controls import AuthorityConnection
from tests.gateway.test_authority_cold_route_recovery import _Runner, _source


@pytest_asyncio.fixture
async def owner(tmp_path, monkeypatch):
    from gateway.session_authority import initialize_session_authority
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    runner = _Runner(tmp_path)
    authority = await initialize_session_authority(runner, profile_id=str(tmp_path), instance_id='control-read')
    ref = authority.register(_source())
    connection = AuthorityConnection(authority, object(), {
        'provider': 'local', 'user_id': 'viewer', 'profile_id': str(tmp_path),
        'instance_id': 'control-read', 'capabilities': ['session:read']})
    try:
        yield connection, ref
    finally:
        authority.db.close()
        runner.session_store._routing_db.close()


async def poll(connection, ref, **params):
    return await connection.dispatch({'id': 'control', 'method': 'session.control.read',
                                      'params': {'session_id': ref.session_id, **params}})


class _ObservedLock:
    """Keep the real lock; signal when the reader reaches its contended acquire."""
    def __init__(self, lock):
        self.lock = lock
        self.attempted = threading.Event()
        self.holder = None

    def __enter__(self):
        if self.holder is not None and threading.get_ident() != self.holder:
            self.attempted.set()
        self.lock.acquire()
        return self

    def __exit__(self, *exc):
        self.lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_read', [False, True])
async def test_writer_wait_keeps_approvals_and_ping_live(owner, monkeypatch, cancel_read):
    from hermes_cli.goals import GoalState
    connection, ref = owner
    authority, sid = connection.authority, ref.session_id
    live, db = authority.sessions[sid], authority.db
    db.set_meta('goal:' + sid, GoalState('before', status='paused', created_at=10).to_json())
    after = GoalState('committed', status='paused', created_at=20).to_json()
    observed = _ObservedLock(db._lock)
    monkeypatch.setattr(db, '_lock', observed)
    held, release, heartbeat = threading.Event(), threading.Event(), threading.Event()
    errors, watchdog_results = [], []
    # These are separate projections; a control read must not consume or clear settlement masking.
    settling = {'unpublished': {'status': 'started'}}
    live.settling.update(settling)

    def hold():
        observed.holder = threading.get_ident()
        try:
            def write(conn):
                db.set_meta('goal:' + sid, after, cursor=conn.cursor())
                held.set()
                if not release.wait(10):
                    raise AssertionError('writer release was not signalled')
            db._execute_write(write)
        except BaseException as exc:
            errors.append(exc)

    def watchdog():
        try:
            reached = observed.attempted.wait(5)
            responsive = heartbeat.wait(2) if reached else False
            watchdog_results.append((reached, responsive))
        finally:
            release.set()

    holder = threading.Thread(target=hold, name='control-test-holder')
    guard = threading.Thread(target=watchdog, name='control-test-watchdog')
    holder.start()
    read_task = pulse_task = None
    try:
        assert await asyncio.to_thread(held.wait, 5), 'fixture did not acquire the lock'
        guard.start()
        read_task = asyncio.create_task(poll(connection, ref))

        async def pulse():
            assert await asyncio.to_thread(observed.attempted.wait, 5)
            approvals = await connection.dispatch({'id': 'approvals', 'method': 'approval.pending',
                                                   'params': {'session_id': sid}})
            reply = await connection.dispatch({'id': 'ping', 'method': 'ping', 'params': {}})
            live_before_release = not release.is_set() and not read_task.done()
            if cancel_read:
                read_task.cancel()
                cancelled = await asyncio.gather(read_task, return_exceptions=True)
                assert isinstance(cancelled[0], asyncio.CancelledError)
            if live_before_release and approvals.get('result') == {'approvals': []} and reply.get('result') == {'pong': True}:
                heartbeat.set()
            return live_before_release, approvals, reply

        pulse_task = asyncio.create_task(pulse())
        response, pulse_result = await asyncio.gather(read_task, pulse_task, return_exceptions=True)
    finally:
        release.set()
        # The independent watchdog also releases the initial candidate's frozen-loop composition.
        await asyncio.to_thread(holder.join, 10)
        if guard.ident is not None:
            await asyncio.to_thread(guard.join, 10)
        for task in (read_task, pulse_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        # Cancelling to_thread cancels only its waiter; drain the actual worker before closing DB.
        if cancel_read:
            await asyncio.get_running_loop().shutdown_default_executor()
        assert not holder.is_alive() and not guard.is_alive()
    assert not errors, errors
    assert watchdog_results == [(True, True)], 'owner loop did not answer approvals/ping before writer release'
    assert not isinstance(pulse_result, BaseException), pulse_result
    responsive, approvals, ping = pulse_result
    assert responsive and approvals['result'] == {'approvals': []} and ping['result'] == {'pong': True}
    assert live.settling == settling
    if cancel_read:
        assert isinstance(response, asyncio.CancelledError)
        assert db.get_meta('goal:' + sid) == after, 'cancelling a read must not cancel the writer'
    else:
        assert not isinstance(response, BaseException), response
        assert response['result']['control']['goal']['title'] == 'committed'
        assert response == await poll(connection, ref)


@pytest.mark.asyncio
async def test_persisted_control_does_not_wait_for_unrelated_event_state(owner):
    from hermes_cli.goals import GoalState
    connection, ref = owner
    authority = connection.authority
    live = authority.sessions[ref.session_id]
    authority.db.set_meta('goal:' + ref.session_id, GoalState('persisted').to_json())
    held, finished, release = threading.Event(), threading.Event(), threading.Event()
    observed, errors = [], []

    def publisher():
        try:
            with live.event_stream.lock:
                held.set()
                # This is NOT approval.pending: that handler genuinely projects pending live state.
                observed.append(finished.wait(2))
                release.set()
        except BaseException as exc:
            errors.append(exc)

    holder = threading.Thread(target=publisher, name='control-test-publisher')
    holder.start()
    try:
        assert await asyncio.to_thread(held.wait, 5)
        result = await poll(connection, ref)
        finished.set()
        assert result['result']['control']['goal']['title'] == 'persisted'
    finally:
        finished.set()
        await asyncio.to_thread(holder.join, 10)
        assert not holder.is_alive()
    assert not errors, errors
    assert observed == [True], 'persisted controls unnecessarily waited for the stream lock'
    assert release.is_set()


@pytest.mark.asyncio
async def test_control_return_parity_ordering_and_session_identity(owner):
    from gateway.session_ancillary import control_snapshot
    from hermes_cli.goals import GoalState, GoalGate
    from hermes_cli.loops import LoopState
    from hermes_cli.heartbeat import HeartbeatState
    connection, ref = owner
    authority, sid = connection.authority, ref.session_id
    states = [('goal', GoalState('owned', status='paused', created_at=10, waiting_on_pid=99999999,
                                gates=[GoalGate(command='gate', last_output_tail='PRIVATE')])),
              ('loop', LoopState('loop', status='paused', created_at=20, route={'token': 'PRIVATE'})),
              ('heartbeat', HeartbeatState('heartbeat', 600, status='paused', created_at=30))]
    for kind, state in states:
        await asyncio.to_thread(authority.db.set_meta, kind + ':' + sid, state.to_json())
        authority.db.set_meta(kind + ':' + authority.sessions[sid].route, state.to_json().replace('owned', 'wrong'))
    changes = authority.db._conn.total_changes
    expected = control_snapshot(authority, ref)
    response = await poll(connection, ref)
    assert response == {'jsonrpc': '2.0', 'id': 'control', 'result': {'control': expected}}
    assert expected['goal']['title'] == 'owned' and expected['updated_at'] == 30
    assert expected['goal']['wait_barrier']['target'] == 99999999
    assert 'PRIVATE' not in str(response)
    # The dispatch still awaits this session's preceding write; no detached read or scheduler lane.
    await asyncio.to_thread(authority.db.set_meta, 'goal:' + sid, GoalState('later', created_at=40).to_json())
    assert (await poll(connection, ref))['result']['control']['goal']['title'] == 'later'
    assert authority.db._conn.total_changes == changes + 1


@pytest.mark.asyncio
@pytest.mark.parametrize('case,reason', [
    ('foreign_profile', 'profile_mismatch'), ('no_capability', 'permission_denied'),
    ('foreign_local_owner', 'permission_denied'),
    ('missing_session', 'not_found'), ('invalid_params', 'invalid_params'),
])
async def test_control_refusals_still_precede_projection(owner, monkeypatch, case, reason):
    from gateway import session_ancillary
    from gateway.config import Platform
    connection, ref = owner
    cases = {
        'foreign_profile': {'params': {'profile': 'foreign'}},
        'no_capability': {'actor': replace(connection.actor, capabilities=frozenset())},
        'foreign_local_owner': {'source': SimpleNamespace(platform=Platform.LOCAL, user_id='someone-else')},
        'missing_session': {'ref': SessionRef(ref.profile_id, 'missing')},
        'invalid_params': {'params': {'unexpected': True}},
    }
    setup = cases[case]
    live = connection.authority.sessions[ref.session_id]
    params = setup.get('params', {})
    connection.actor = setup.get('actor', connection.actor)
    live.source = setup.get('source', live.source)
    ref = setup.get('ref', ref)
    def forbidden(*args, **kwargs):
        pytest.fail('unauthorized projection')
    monkeypatch.setattr(session_ancillary, 'control_snapshot', forbidden)
    monkeypatch.setattr(session_ancillary, '_control_snapshot', forbidden, raising=False)
    response = await poll(connection, ref, **params)
    assert response['error']['data']['reason'] == reason


@pytest.mark.asyncio
async def test_moved_messaging_route_keeps_its_persisted_control_readable(owner):
    from hermes_cli.goals import GoalState
    connection, ref = owner
    authority = connection.authority
    authority.db.set_meta('goal:' + ref.session_id, GoalState('historic').to_json())
    live = authority.sessions[ref.session_id]
    with authority.runner.session_store._lock:
        authority.runner.session_store._entries[live.route].session_id = 'new-conversation'
    live.superseded = True
    assert (await poll(connection, ref))['result']['control']['goal']['title'] == 'historic'
    stale = await connection.dispatch({'id': 'stale', 'method': 'subagent.list', 'params': {'session_id': ref.session_id}})
    assert stale['error']['data']['reason'] == 'stale_generation'


@pytest.mark.asyncio
async def test_local_control_still_follows_receipt_and_compression_tip(tmp_path, monkeypatch):
    from hermes_cli.goals import GoalState
    from gateway.session_ancillary import control_snapshot, physical_target
    from tests.gateway.test_local_resume_compression_child import _compressed
    compressed = await _compressed(tmp_path, monkeypatch)
    authority = compressed.warm
    ref = SessionRef(authority.profile_id, compressed.owner)
    connection = AuthorityConnection(authority, object(), {
        'provider': 'local', 'user_id': 'owner', 'profile_id': authority.profile_id,
        'instance_id': authority.instance_id, 'capabilities': ['session:read']})
    try:
        authority.db.set_meta('goal:' + compressed.owner, GoalState('archived-root', status='cleared').to_json())
        authority.db.set_meta('goal:child-2', GoalState('current-local').to_json())
        expected = control_snapshot(authority, ref)
        assert authority.db.get_compression_tip(physical_target(authority, ref)) == 'child-2'
        result = await poll(connection, ref)
        assert result['result'] == {'control': expected}
        assert result['result']['control']['goal']['title'] == 'current-local'
    finally:
        await connection.close()
        authority.db.close()
        authority.runner.session_store._routing_db.close()


def test_get_meta_keeps_writer_transaction_visibility(tmp_path):
    from hermes_state import SessionDB
    db = SessionDB(db_path=tmp_path / 'visibility.db')
    try:
        db.set_meta('progress', 'committed')
        db._conn.execute('BEGIN IMMEDIATE')
        db.set_meta('progress', 'uncommitted', cursor=db._conn.cursor())
        assert db.get_meta('progress') == 'uncommitted'
        db._conn.rollback()
        assert db.get_meta('progress') == 'committed'
    finally:
        db.close()
