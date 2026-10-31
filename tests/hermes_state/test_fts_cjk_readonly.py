"""Standalone readers must consume native managed CJK indexes without maintenance."""
import sqlite3

import pytest

from hermes_state import SessionDB

pytest_plugins = ('tests.hermes_state.test_fts_cjk_bigram',)


def test_gui_opener_loads_ready_cjk_without_writes(cjk_so, tmp_path, monkeypatch):
    from hermes_cli.web_server_sessions import _open_session_db_at_path

    monkeypatch.setenv('HERMES_FTS5_CJK_SO', str(cjk_so))
    path = tmp_path / 'state.db'
    with SessionDB(path) as writer:
        writer.create_session('both', source='cli')
        writer.create_session('other', source='cli')
        writer.append_message('both', role='user', content='中文 数据 alpha')
        writer.append_message('other', role='user', content='中文 其他 omega')
        original = SessionDB._connect_read_only
        statements, connections = [], []

        def connect(store, timeout):
            conn = original(store, timeout)
            conn.execute('PRAGMA query_only=ON')
            conn.set_trace_callback(statements.append)
            connections.append(conn)
            return conn

        monkeypatch.setattr(SessionDB, '_connect_read_only', connect)
        before = writer._conn.execute('PRAGMA data_version').fetchone()[0]
        reader = _open_session_db_at_path(path, read_only=True)
        try:
            assert reader._fts_cjk_loaded
            assert reader._fts_cjk_available
            rows = reader.search_messages('中文 数据', fields=('session_id',))
            assert rows == [{'session_id': 'both'}]
            assert any('messages_fts_cjk MATCH' in sql for sql in statements)
            assert not any(' LIKE ' in sql.upper() for sql in statements)
            assert not reader._conn.in_transaction
            assert writer._conn.execute('PRAGMA data_version').fetchone()[0] == before
            with pytest.raises(sqlite3.OperationalError, match='not authorized'):
                reader._conn.execute("SELECT load_extension('not-allowed')")
        finally:
            reader.close()
        assert connections
        for conn in connections:
            with pytest.raises(sqlite3.ProgrammingError):
                conn.execute('SELECT 1')


def _seed_cjk(path):
    with SessionDB(path) as writer:
        writer.create_session('history', source='cli')
        writer.append_message('history', role='user', content='中文 initial')


def test_reused_reader_demotes_after_native_writer_loses_tokenizer(cjk_so, tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_FTS5_CJK_SO', str(cjk_so))
    path = tmp_path / 'stale.db'
    _seed_cjk(path)
    with SessionDB(path, read_only=True) as reader:
        assert reader._fts_cjk_available
        monkeypatch.setenv('HERMES_FTS5_CJK_SO', str(tmp_path / 'absent.so'))
        with SessionDB(path) as incapable:
            incapable.append_message('history', role='user', content='项目 newly written')
        statements = []
        reader._conn.set_trace_callback(statements.append)
        assert len(reader.search_messages('项目', fields=('id',))) == 1
        assert not reader._fts_cjk_available
        assert not any('messages_fts_cjk MATCH' in sql for sql in statements)
        assert not reader._conn.in_transaction


def test_readiness_and_match_share_snapshot_not_idle_transaction(cjk_so, tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_FTS5_CJK_SO', str(cjk_so))
    path = tmp_path / 'snapshot.db'
    _seed_cjk(path)
    with SessionDB(path, read_only=True) as reader:
        original = reader._probe_read_only_cjk
        observed = []

        def probe(cursor):
            ready = original(cursor)
            observed.append(ready)
            if len(observed) == 1:
                with monkeypatch.context() as changed:
                    changed.setenv('HERMES_FTS5_CJK_SO', str(tmp_path / 'absent.so'))
                    with SessionDB(path) as incapable:
                        incapable.append_message('history', role='user', content='中文 later')
                # A valid old snapshot must include canonical rows too. Without
                # a pin, stale MATCH alone still returns 1 and would fool this test.
                assert reader._conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 1
            return ready

        monkeypatch.setattr(reader, '_probe_read_only_cjk', probe)
        first = reader.search_messages('中文', fields=('id',))
        assert observed == [True]
        assert len(first) == 1  # valid old snapshot, not a mixed-state empty result
        assert not reader._conn.in_transaction
        assert len(reader.search_messages('中文', fields=('id',))) == 2
        assert observed == [True, False]
        assert not reader._conn.in_transaction


def test_cjk_probe_retains_native_read_ioerr_retry(cjk_so, tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_FTS5_CJK_SO', str(cjk_so))
    path = tmp_path / 'ioerr.db'
    _seed_cjk(path)
    with SessionDB(path, read_only=True) as reader:
        original = reader._probe_read_only_cjk
        attempts = []

        def probe(cursor):
            attempts.append(1)
            if len(attempts) == 1:
                raise sqlite3.OperationalError('disk I/O error')
            return original(cursor)

        monkeypatch.setattr(reader, '_probe_read_only_cjk', probe)
        assert len(reader.search_messages('中文', fields=('id',))) == 1
        assert attempts == [1, 1]
        assert not reader._conn.in_transaction


def test_ascii_miss_uses_the_same_readonly_cjk_snapshot(cjk_so, tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_FTS5_CJK_SO', str(cjk_so))
    path = tmp_path / 'ascii-miss.db'
    with SessionDB(path) as writer:
        writer.create_session('history', source='cli')
        writer.append_message('history', role='user', content='修改youer服务端')
    with SessionDB(path, read_only=True) as reader:
        original = reader._probe_read_only_cjk
        probes, statements = [], []
        def probe(cursor):
            probes.append(1)
            return original(cursor)
        monkeypatch.setattr(reader, '_probe_read_only_cjk', probe)
        reader._conn.set_trace_callback(statements.append)
        assert len(reader.search_messages('youer', fields=('id',))) == 1
        assert probes == [1]
        assert any('messages_fts_cjk MATCH' in sql for sql in statements)
        assert not reader._conn.in_transaction


def test_ready_readonly_keeps_mixed_boolean_single_character_and_filter_semantics(cjk_so, tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_FTS5_CJK_SO', str(cjk_so))
    path = tmp_path / 'semantics.db'
    with SessionDB(path) as writer:
        writer.create_session('history', source='cli')
        writer.append_message('history', role='user', content='中文 项目 alpha')
        writer.append_message('history', role='assistant', content='中文 数据 omega')
        writer.append_message('history', role='tool', content='中文 项目 tool-only')
    with SessionDB(path, read_only=True) as reader:
        assert len(reader.search_messages('项目 alpha', fields=('id',))) == 1
        assert reader.search_messages('项目 omega', fields=('id',)) == []
        assert len(reader.search_messages('项目 OR 数据', fields=('id',))) == 2
        assert len(reader.search_messages('中', role_filter=['user', 'assistant'], fields=('id',))) == 2
        assert len(reader.search_messages('项目', role_filter=['tool'], fields=('id',))) == 1
        assert reader.search_messages('项目', role_filter=['assistant'], fields=('id',)) == []
