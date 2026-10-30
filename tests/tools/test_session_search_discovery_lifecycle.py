"""Discovery lifecycle: initialization, supplementary reads and ready CJK parity."""
import json
import sqlite3
import threading

import pytest

from hermes_state import SessionDB
from hermes_state_common import FTS_CJK_STALE_KEY, FTS_STALE_KEY
from tools.interrupt import set_interrupt
from tools.session_search_tool import session_search
pytest_plugins = ('tests.hermes_state.test_fts_cjk_bigram',)

EXPENSIVE = 'WITH RECURSIVE owned_vm(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM owned_vm WHERE x<20000000) SELECT sum(x) FROM owned_vm'


@pytest.mark.parametrize('stage', ['open', 'title', 'hydrate'])
def test_cancel_during_real_sql_in_each_discovery_phase_closes_reader(tmp_path, monkeypatch, stage):
    with SessionDB(tmp_path / 'phases.db') as writer:
        writer.create_session('history', source='cli')
        writer.append_message('history', role='user', content='ownedneedle')
        readers = []
        original_connect = SessionDB._connect_read_only
        original_probe = SessionDB._fts_table_probe
        def connect(store, timeout):
            conn = original_connect(store, timeout)
            readers.append(conn)
            conn.set_trace_callback(lambda sql: set_interrupt(True) if 'owned_vm' in sql else None)
            return conn
        monkeypatch.setattr(SessionDB, '_connect_read_only', connect)
        if stage == 'open':
            def probe(cursor, table):
                cursor.execute(EXPENSIVE).fetchall()
                return original_probe(cursor, table)
            monkeypatch.setattr(SessionDB, '_fts_table_probe', staticmethod(probe))
        else:
            name = 'resolve_session_by_title' if stage == 'title' else 'get_anchored_view'
            original = getattr(SessionDB, name)
            def supplementary(store, *args, **kwargs):
                store._conn.execute(EXPENSIVE).fetchall()
                return original(store, *args, **kwargs)
            monkeypatch.setattr(SessionDB, name, supplementary)
        try:
            result = json.loads(session_search(query='ownedneedle', db=writer))
            assert result['success'] is False and result['code'] == 'session_search_cancelled'
            assert 'results' not in result
            assert readers
            for conn in readers:
                with pytest.raises(sqlite3.ProgrammingError):
                    conn.execute('SELECT 1')
            assert writer._conn.execute('SELECT 1').fetchone()[0] == 1
        finally:
            set_interrupt(False)


def test_cancel_after_payload_is_latched_not_successful_empty(tmp_path, monkeypatch):
    import tools.session_search_tool as tool
    from tools.session_search_cancellation import DiscoveryCancellation
    with SessionDB(tmp_path / 'final.db') as writer:
        original = tool._discover_payload
        def payload(*args, **kwargs):
            result = original(*args, **kwargs)
            set_interrupt(True)
            return result
        monkeypatch.setattr(tool, '_discover_payload', payload)
        try:
            result = json.loads(session_search(query='absentneedle', db=writer))
            assert result['success'] is False and result['code'] == 'session_search_cancelled'
        finally:
            set_interrupt(False)
        control = DiscoveryCancellation()
        set_interrupt(True)
        try:
            assert control.progress() == 1
            set_interrupt(False)
            with pytest.raises(InterruptedError):
                control.check()
        finally:
            set_interrupt(False)


@pytest.mark.parametrize('marker', [None, FTS_STALE_KEY, FTS_CJK_STALE_KEY, 'fts_cjk_rebuild_high_water'])
def test_ready_cjk_uses_loaded_owned_reader_and_respects_stale_markers(cjk_so, tmp_path, monkeypatch, marker):
    monkeypatch.setenv('HERMES_FTS5_CJK_SO', str(cjk_so))
    with SessionDB(tmp_path / 'cjk.db') as writer:
        assert writer._fts_cjk_available
        writer.create_session('both', source='cli')
        writer.create_session('partial', source='cli')
        writer.append_message('both', role='user', content='中文 数据')
        writer.append_message('partial', role='user', content='中文 其他')
        if marker:
            writer._conn.execute('INSERT OR REPLACE INTO state_meta(key,value) VALUES (?,?)', (marker, '1'))
        statements = []
        original = SessionDB._connect_read_only
        def connect(store, timeout):
            conn = original(store, timeout)
            conn.set_trace_callback(statements.append)
            return conn
        monkeypatch.setattr(SessionDB, '_connect_read_only', connect)
        result = json.loads(session_search(query='中文 数据', db=writer))
        assert result['success'] is True
        routed_cjk = any('messages_fts_cjk MATCH' in sql for sql in statements)
        assert routed_cjk is (marker is None)
        if marker is None:
            assert [r['session_id'] for r in result['results']] == ['both']
        else:
            assert 'both' in {r['session_id'] for r in result['results']}


def test_injected_readonly_is_not_owned_and_is_not_closed(tmp_path, monkeypatch):
    with SessionDB(tmp_path / 'readonly.db') as writer:
        with SessionDB(writer.db_path, read_only=True) as injected:
            opened = []
            original = SessionDB._connect_read_only
            def connect(store, timeout):
                conn = original(store, timeout)
                opened.append(conn)
                return conn
            monkeypatch.setattr(SessionDB, '_connect_read_only', connect)
            result = json.loads(session_search(query='absentneedle', db=injected))
            assert result['success'] is True and result['results'] == []
            assert len(opened) == 1 and opened[0] is not injected._conn
            with pytest.raises(sqlite3.ProgrammingError):
                opened[0].execute('SELECT 1')
            assert injected._conn.execute('SELECT 1').fetchone()[0] == 1


def test_cancelled_busy_read_is_failure_not_empty_and_closes_reader(tmp_path, monkeypatch):
    import tools.session_search_tool as tool
    path = tmp_path / 'busy.db'
    with SessionDB(path) as seed:
        seed.create_session('history', source='cli')
        seed.append_message('history', role='user', content='ownedneedle')
    blocker = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    assert blocker.execute('PRAGMA journal_mode=DELETE').fetchone()[0] == 'delete'
    locked, exited = threading.Event(), threading.Event()
    results, errors, readers, lock_codes = [], [], [], []
    original_title = tool._title_match_result
    original_connect = SessionDB._connect_read_only
    worker = None
    def connect(store, timeout):
        conn = original_connect(store, timeout)
        if threading.current_thread() is worker:
            readers.append(conn)
        return conn
    def title(*args, **kwargs):
        blocker.execute('BEGIN EXCLUSIVE')
        locked.set()
        try:
            return original_title(*args, **kwargs)
        except sqlite3.Error as exc:
            lock_codes.append(exc.sqlite_errorcode & 0xff)
            raise
    monkeypatch.setattr(SessionDB, '_connect_read_only', connect)
    monkeypatch.setattr(tool, '_title_match_result', title)
    with SessionDB(path, read_only=True) as injected:
        def run():
            try:
                results.append(json.loads(session_search(query='ownedneedle', db=injected)))
            except BaseException as exc:
                errors.append(exc)
            finally:
                set_interrupt(False)
                exited.set()
        worker = threading.Thread(target=run)
        worker.start()
        try:
            assert locked.wait(5)
            set_interrupt(True, worker.ident)
            assert exited.wait(2)
            worker.join(2)
            assert not errors and results[0]['code'] == 'session_search_cancelled'
            assert results[0]['success'] is False and 'results' not in results[0]
            assert lock_codes == [sqlite3.SQLITE_BUSY]
            assert blocker.in_transaction
            for conn in readers:
                with pytest.raises(sqlite3.ProgrammingError):
                    conn.execute('SELECT 1')
        finally:
            blocker.rollback()
            worker.join(15)
            set_interrupt(False, worker.ident)
            blocker.close()
            assert not worker.is_alive()


@pytest.mark.parametrize('invalid', [None, '', ':memory:'])
def test_unreopenable_injected_path_never_falls_back_to_default(tmp_path, monkeypatch, invalid):
    with SessionDB(tmp_path / 'path.db') as writer:
        opened = []
        original = SessionDB._connect_read_only
        def connect(store, timeout):
            opened.append(store.db_path)
            return original(store, timeout)
        monkeypatch.setattr(SessionDB, '_connect_read_only', connect)
        with monkeypatch.context() as context:
            context.setattr(writer, 'db_path', invalid)
            result = json.loads(session_search(query='ownedneedle', db=writer))
            assert result['success'] is False and result['code'] == 'session_search_unavailable'
            assert not opened
        assert writer._conn.execute('SELECT 1').fetchone()[0] == 1


def test_discovery_reopens_special_character_filename_without_uri_aliasing(tmp_path):
    with SessionDB(tmp_path / 'question?# space.db') as writer:
        writer.create_session('history', source='cli')
        writer.append_message('history', role='user', content='ownedneedle')
        result = json.loads(session_search(query='ownedneedle', db=writer))
        assert result['success'] is True and result['results'][0]['session_id'] == 'history'
