"""Optional CJK readiness failures preserve the existing canonical fallback, without writes."""
import hashlib
import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_state_common import FTS_CJK_STALE_KEY, FTS_STALE_KEY

pytest_plugins = ('tests.hermes_state.test_fts_cjk_bigram',)


@pytest.mark.parametrize('state', [
    'absent-table', 'absent-view', 'absent-trigger', 'wrong-trigger', 'unknown-table', 'unknown-view',
    'base-stale', 'cjk-stale-zero', 'high-water-zero', 'orphan-progress', 'completed-pair',
    'missing-library', 'invalid-library', 'disabled',
])
def test_optional_index_not_ready_keeps_fallback_and_never_repairs(cjk_so, tmp_path, monkeypatch, state):
    monkeypatch.setenv('HERMES_FTS5_CJK_SO', str(cjk_so))
    path = tmp_path / 'state.db'
    with SessionDB(path) as writer:
        writer.create_session('history', source='cli')
        writer.append_message('history', role='user', content='中文 项目 数据库 alpha')
        conn = writer._conn
        schema_changes = {
            'absent-table': ['DROP TABLE messages_fts_cjk'],
            'absent-view': ['DROP VIEW messages_fts_cjk_src'],
            'absent-trigger': ['DROP TRIGGER messages_fts_cjk_insert'],
            'wrong-trigger': [
                'DROP TRIGGER messages_fts_cjk_insert',
                'CREATE TRIGGER messages_fts_cjk_insert AFTER INSERT ON messages BEGIN SELECT 1; END',
            ],
            'unknown-table': [
                'DROP TABLE messages_fts_cjk',
                "CREATE VIRTUAL TABLE messages_fts_cjk USING fts5(content, tokenize='cjk_unicode61')",
            ],
            'unknown-view': [
                'DROP VIEW messages_fts_cjk_src',
                'CREATE VIEW messages_fts_cjk_src AS SELECT id, role, content, tool_name, tool_calls FROM messages WHERE 0',
            ],
        }
        for statement in schema_changes.get(state, []):
            conn.execute(statement)
        keys = {
            'base-stale': [(FTS_STALE_KEY, '0')],
            'cjk-stale-zero': [(FTS_CJK_STALE_KEY, '0')],
            'high-water-zero': [('fts_cjk_rebuild_high_water', '0')],
            'orphan-progress': [('fts_cjk_rebuild_progress', '0')],
            'completed-pair': [('fts_cjk_rebuild_high_water', '1'), ('fts_cjk_rebuild_progress', '1')],
        }.get(state, [])
        conn.executemany('INSERT OR REPLACE INTO state_meta(key,value) VALUES (?,?)', keys)
        # Base/trigram stamp does not certify this CJK layout or coverage.
        conn.execute("INSERT OR REPLACE INTO state_meta VALUES ('fts_storage_version', '3')")
    if state == 'missing-library':
        monkeypatch.setenv('HERMES_FTS5_CJK_SO', str(tmp_path / 'missing.so'))
    elif state == 'invalid-library':
        bad = tmp_path / 'invalid.so'
        bad.write_bytes(b'not an executable library')
        monkeypatch.setenv('HERMES_FTS5_CJK_SO', str(bad))
    elif state == 'disabled':
        monkeypatch.setenv('HERMES_CJK_FTS', '0')
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    statements, writes = [], []
    original = SessionDB._connect_read_only
    denied = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
              sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_VIEW, sqlite3.SQLITE_CREATE_TRIGGER,
              sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_DROP_TRIGGER}

    def connect(store, timeout):
        rc = original(store, timeout)
        rc.execute('PRAGMA query_only=ON')
        def authorize(action, arg1, arg2, database, source):
            if action in denied:
                writes.append((action, arg1))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        rc.set_authorizer(authorize)
        rc.set_trace_callback(statements.append)
        return rc

    monkeypatch.setattr(SessionDB, '_connect_read_only', connect)
    with SessionDB(path, read_only=True) as reader:
        assert not reader._fts_cjk_available
        assert len(reader.search_messages('项目', fields=('id',))) == 1
        assert not reader._fts_cjk_available
        assert len(reader.search_messages('数据库', fields=('id',))) == 1
        assert len(reader.search_messages('alpha', fields=('id',))) == 1
        assert not any('messages_fts_cjk MATCH' in sql for sql in statements)
        assert not reader._conn.in_transaction
        if state != 'base-stale':
            assert reader._fts_enabled and reader._trigram_available
        with pytest.raises(sqlite3.OperationalError, match='not authorized'):
            reader._conn.execute("SELECT load_extension('not-allowed')")
    assert not writes
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_loaded_pending_reader_promotes_after_native_backfill_without_reloading(cjk_so, tmp_path, monkeypatch):
    path = tmp_path / 'pending.db'
    monkeypatch.setenv('HERMES_FTS5_CJK_SO', str(tmp_path / 'absent.so'))
    with SessionDB(path) as seed:
        seed.create_session('history', source='cli')
        seed.append_message('history', role='user', content='中文 项目')
    monkeypatch.setenv('HERMES_FTS5_CJK_SO', str(cjk_so))
    with SessionDB(path) as writer:
        with SessionDB(path, read_only=True) as reader:
            assert reader._fts_cjk_loaded and not reader._fts_cjk_available
            assert len(reader.search_messages('项目', fields=('id',))) == 1
            assert not reader._fts_cjk_available
            assert writer.optimize_fts_storage(vacuum=False)['ok']
            statements = []
            reader._conn.set_trace_callback(statements.append)
            assert len(reader.search_messages('项目', fields=('id',))) == 1
            assert reader._fts_cjk_available
            assert any('messages_fts_cjk MATCH' in sql for sql in statements)
            assert not reader._conn.in_transaction


def test_explicit_reader_transaction_is_not_ended(cjk_so, tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_FTS5_CJK_SO', str(cjk_so))
    path = tmp_path / 'transaction.db'
    with SessionDB(path) as writer:
        writer.create_session('history', source='cli')
        writer.append_message('history', role='user', content='中文 项目')
    with SessionDB(path, read_only=True) as reader:
        reader._conn.execute('BEGIN')
        try:
            assert len(reader.search_messages('项目', fields=('id',))) == 1
            assert reader._conn.in_transaction
        finally:
            reader._conn.rollback()
