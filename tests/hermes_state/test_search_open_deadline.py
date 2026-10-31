"""Real SQLite opening/probing lock and deadline boundaries (no live store)."""
import sqlite3
import time

import pytest

from hermes_state import SessionDB
from hermes_cli import web_server_sessions


def test_readonly_schema_probe_deadline_stops_real_sql(tmp_path, monkeypatch):
    path = tmp_path / 'state.db'
    SessionDB(path).close()
    # Warm the real opener's module/config imports before timing the SQL probe.
    web_server_sessions._open_session_db_at_path(path, read_only=True).close()
    query = ('WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL '
             'SELECT n+1 FROM x WHERE n<100000000) SELECT sum(n) FROM x')
    monkeypatch.setattr(web_server_sessions, '_session_db_read_probe_statements', lambda: (query,))
    closed = []
    original_close = SessionDB.close
    def close(self):
        original_close(self)
        closed.append(self._conn is None)
    monkeypatch.setattr(SessionDB, 'close', close)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        web_server_sessions._open_session_db_at_path(path, read_only=True, read_deadline=started + 0.05)
    assert time.monotonic() - started < 2
    assert closed == [True]


def test_remaining_open_budget_bounds_real_sqlite_lock_wait(tmp_path):
    path = tmp_path / 'state.db'
    SessionDB(path).close()
    with sqlite3.connect(path, isolation_level=None) as writer:
        writer.execute('PRAGMA journal_mode=DELETE')
        writer.execute('BEGIN EXCLUSIVE')
        try:
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                SessionDB(path, read_only=True, read_deadline=started + 0.05)
            assert time.monotonic() - started < 2
            # Cancelling the RO opener did not abort this independent transaction.
            writer.execute("INSERT INTO state_meta(key,value) VALUES('sentinel','alive')")
            writer.execute('COMMIT')
        finally:
            if writer.in_transaction:
                writer.execute('ROLLBACK')
    with sqlite3.connect(path) as reader:
        assert reader.execute("SELECT value FROM state_meta WHERE key='sentinel'").fetchone() == ('alive',)
