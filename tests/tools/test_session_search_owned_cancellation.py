"""Agent discovery cancellation owns its reader, never the injected writer."""
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
import hermes_state_search as search
from tools.interrupt import set_interrupt

EXPENSIVE = '(WITH RECURSIVE owned_vm(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM owned_vm WHERE x<20000000) SELECT sum(x) FROM owned_vm)'


@pytest.mark.parametrize('role', ['user', 'tool'])
def test_inline_cancel_closes_owned_sql_keeps_writer_transaction_and_peer(tmp_path, monkeypatch, role):
    from agent.inline_tool_executors import _session_search
    with SessionDB(tmp_path / 'owned-synthetic.db') as writer:
        writer.create_session('history', source='cli')
        writer.create_session('current', source='cli')
        writer.append_message('history', role=role, content='ownedneedle', tool_name='fixture')
        writer._conn.execute('CREATE TABLE marker (value TEXT)')
        writer._conn.execute("INSERT INTO marker VALUES ('before')")
        writer._conn.commit()
        # A caller-owned transaction must neither be interrupted nor committed by recall.
        writer._conn.execute('BEGIN IMMEDIATE')
        writer._conn.execute("INSERT INTO marker VALUES ('during')")
        started, exited = threading.Event(), threading.Event()
        writer._conn.set_trace_callback(lambda sql: started.set() if 'owned_vm' in sql else None)
        readers, errors, results = [], [], []
        original_connect = SessionDB._connect_read_only
        original_select = search._search_select_sql
        worker = None
        def connect(store, timeout):
            conn = original_connect(store, timeout)
            if threading.current_thread() is worker:
                readers.append(conn)
                conn.set_trace_callback(lambda sql: started.set() if 'owned_vm' in sql else None)
            return conn
        def slow_select(snippet, table, where, order, limit):
            if threading.current_thread() is worker:
                where = [*where, EXPENSIVE + '>=0']
            return original_select(snippet, table, where, order, limit)
        monkeypatch.setattr(SessionDB, '_connect_read_only', connect)
        monkeypatch.setattr(search, '_search_select_sql', slow_select)
        # The former helper would deliberately fall back to the shared writer here.
        monkeypatch.setattr(writer, '_checkout_read_conn', lambda: None)
        agent = SimpleNamespace(_get_session_db_for_recall=lambda: writer, session_id='current')
        def run():
            try:
                results.append(json.loads(_session_search(agent, {'query':'ownedneedle', 'role_filter':role}, None)))
            except BaseException as exc:
                errors.append(exc)
            finally:
                set_interrupt(False)
                exited.set()
        worker = threading.Thread(target=run, name='owned-reader-fixture')
        worker.start()
        try:
            assert started.wait(5), 'real SQLite search never started'
            with SessionDB(writer.db_path, read_only=True) as peer:
                assert peer.search_messages('ownedneedle', role_filter=[role], fields=['id'])
            set_interrupt(True, worker.ident)
            assert exited.wait(2), 'cancelled SQLite VM kept running'
            worker.join(2)
            assert not errors, [type(e).__name__ for e in errors]
            assert results and results[0].get('success') is False
            assert 'results' not in results[0]
            assert readers and all(conn is not writer._conn for conn in readers)
            for conn in readers:
                with pytest.raises(sqlite3.ProgrammingError):
                    conn.execute('SELECT 1')
            assert writer._conn.in_transaction
            assert [r[0] for r in writer._conn.execute('SELECT value FROM marker')] == ['before', 'during']
            writer._conn.commit()
            writer._conn.execute("INSERT INTO marker VALUES ('after')")
            writer._conn.commit()
            with SessionDB(writer.db_path, read_only=True) as peer:
                assert [r[0] for r in peer._conn.execute('SELECT value FROM marker')] == ['before', 'during', 'after']
        finally:
            set_interrupt(True, worker.ident)
            worker.join(15)
            set_interrupt(False, worker.ident)
            assert not worker.is_alive()


def test_precancel_does_not_start_discovery_and_success_empty_is_not_cancelled(tmp_path, monkeypatch):
    from agent.inline_tool_executors import _session_search
    with SessionDB(tmp_path / 'precancel-synthetic.db') as writer:
        writer.create_session('current', source='cli')
        agent = SimpleNamespace(_get_session_db_for_recall=lambda: writer, session_id='current')
        opened = []
        original = SessionDB._connect_read_only
        def observe(store, timeout):
            opened.append(store.db_path)
            return original(store, timeout)
        monkeypatch.setattr(SessionDB, '_connect_read_only', observe)
        set_interrupt(True)
        try:
            result = json.loads(_session_search(agent, {'query':'missingneedle'}, None))
            assert result.get('success') is False
            assert not opened
        finally:
            set_interrupt(False)
        result = json.loads(_session_search(agent, {'query':'missingneedle'}, None))
        assert result['success'] is True and result['results'] == []


@pytest.mark.parametrize('role', ['user', 'tool'])
def test_native_deadline_exits_inline_worker_and_closes_reader(tmp_path, monkeypatch, role):
    # Adapted from the original independently-authored native-deadline regression:
    # keep the real executor/worker fanout, but exercise the actual inline injection.
    import run_agent
    from agent import tool_executor as te
    from agent.inline_tool_executors import _session_search
    from tests.waiting import wait_until
    from tools.interrupt import is_thread_interrupted
    assert run_agent._set_interrupt is set_interrupt
    with SessionDB(tmp_path / 'deadline-synthetic.db') as writer:
        writer.create_session('history', source='cli')
        writer.append_message('history', role=role, content='ownedneedle', tool_name='fixture')
        agent = SimpleNamespace(_get_session_db_for_recall=lambda: writer, session_id='current',
                                _tool_worker_threads=set(), _tool_worker_threads_lock=threading.Lock(),
                                _interrupt_requested=False, _touch_activity=lambda *_: None)
        workers, results, readers, posts = [], [], [], []
        started = threading.Event()
        original_connect, original_select = SessionDB._connect_read_only, search._search_select_sql
        def connect(store, timeout):
            conn = original_connect(store, timeout)
            if workers and threading.current_thread() is workers[0]:
                readers.append(conn)
                conn.set_trace_callback(lambda sql: started.set() if 'owned_vm' in sql else None)
            return conn
        def slow_select(snippet, table, where, order, limit):
            if workers and threading.current_thread() is workers[0]:
                where = [*where, EXPENSIVE + '>=0']
            return original_select(snippet, table, where, order, limit)
        def dispatch(agent_arg, **kwargs):
            workers.append(threading.current_thread())
            result = _session_search(agent_arg, {'query':'ownedneedle', 'role_filter':role}, None)
            results.append(json.loads(result))
            return te._ManagedToolResult(result=result, args={}, middleware_trace=[], blocked=False, dispatched=True)
        monkeypatch.setattr(SessionDB, '_connect_read_only', connect)
        monkeypatch.setattr(search, '_search_select_sql', slow_select)
        monkeypatch.setattr(te, '_run_agent_tool_execution_middleware', dispatch)
        monkeypatch.setattr(te, '_resolve_sequential_tool_timeout', lambda: 0.2)
        monkeypatch.setattr(te, '_emit_terminal_post_tool_call', lambda *a, **kw: posts.append(kw))
        try:
            result = te._run_sequential_tool_execution_middleware(agent, function_name='session_search',
                        function_args={}, effective_task_id='fixture', tool_call_id='fixture-deadline', execute=lambda _: None)
            assert started.is_set()
            assert isinstance(result.result, te._ToolTimeoutResult)
            assert any(p.get('status') == 'timeout' for p in posts)
            assert wait_until(lambda: not agent._tool_worker_threads, timeout=2)
            workers[0].join(2)
            assert not workers[0].is_alive() and not is_thread_interrupted(workers[0].ident)
            assert results[0]['success'] is False and results[0]['code'] == 'session_search_cancelled'
            for conn in readers:
                with pytest.raises(sqlite3.ProgrammingError):
                    conn.execute('SELECT 1')
            assert writer.search_messages('ownedneedle', role_filter=[role], fields=['id'])
        finally:
            for worker in workers:
                set_interrupt(True, worker.ident)
                worker.join(15)
                set_interrupt(False, worker.ident)
                assert not worker.is_alive()


def test_native_timeout_before_discovery_admission_never_opens_reader(tmp_path, monkeypatch):
    from agent import tool_executor as te
    from agent.inline_tool_executors import _session_search
    from tests.waiting import wait_until
    admitted, release = threading.Event(), threading.Event()
    workers, results, opened = [], [], []
    with SessionDB(tmp_path / 'queued.db') as writer:
        agent = SimpleNamespace(_get_session_db_for_recall=lambda: writer, session_id='current',
                                _tool_worker_threads=set(), _tool_worker_threads_lock=threading.Lock(),
                                _interrupt_requested=False, _touch_activity=lambda *_: None)
        original = SessionDB._connect_read_only
        def connect(store, timeout):
            opened.append(store.db_path)
            return original(store, timeout)
        def dispatch(agent_arg, **kwargs):
            workers.append(threading.current_thread())
            admitted.set()
            assert release.wait(5)
            result = _session_search(agent_arg, {'query':'missingneedle'}, None)
            results.append(json.loads(result))
            return te._ManagedToolResult(result=result, args={}, middleware_trace=[], blocked=False, dispatched=True)
        monkeypatch.setattr(SessionDB, '_connect_read_only', connect)
        monkeypatch.setattr(te, '_run_agent_tool_execution_middleware', dispatch)
        monkeypatch.setattr(te, '_resolve_sequential_tool_timeout', lambda: 0.2)
        monkeypatch.setattr(te, '_emit_terminal_post_tool_call', lambda *a, **kw: None)
        try:
            result = te._run_sequential_tool_execution_middleware(agent, function_name='session_search',
                        function_args={}, effective_task_id='fixture', tool_call_id='fixture-queued', execute=lambda _: None)
            assert admitted.is_set() and isinstance(result.result, te._ToolTimeoutResult)
            release.set()
            assert wait_until(lambda: not agent._tool_worker_threads, timeout=2)
            workers[0].join(2)
            assert results[0]['code'] == 'session_search_cancelled' and not opened
        finally:
            release.set()
            for worker in workers:
                worker.join(15)
                set_interrupt(False, worker.ident)
                assert not worker.is_alive()
