"""CJK snapshot failure classification, handler ownership and trusted executable paths."""
import shutil
import sqlite3

import pytest

from hermes_state import SessionDB

pytest_plugins = ('tests.hermes_state.test_fts_cjk_bigram',)


@pytest.mark.parametrize('message,exception', [
    ('database is locked', sqlite3.OperationalError),
    ('interrupted', sqlite3.OperationalError),
    ('disk I/O error', sqlite3.OperationalError),
    ('database disk image is malformed', sqlite3.DatabaseError),
])
def test_cjk_snapshot_failures_propagate_and_release_only_own_transaction(cjk_so, tmp_path, monkeypatch, message, exception):
    import hermes_state
    monkeypatch.setenv('HERMES_FTS5_CJK_SO', str(cjk_so))
    monkeypatch.setattr(hermes_state, '_READ_ONLY_IOERR_RETRY_BACKOFF_S', 0)
    path = tmp_path / 'errors.db'
    with SessionDB(path) as writer:
        writer.create_session('history', source='cli')
        writer.append_message('history', role='user', content='中文 项目')
    with SessionDB(path, read_only=True) as reader:
        attempts = []
        def fail(cursor):
            attempts.append(1)
            error = exception(message)
            if message == 'interrupted':
                error.sqlite_errorcode = sqlite3.SQLITE_INTERRUPT
            raise error
        def no_repair(*args, **kwargs):
            raise AssertionError('RO must never enter writer repair')
        monkeypatch.setattr(reader, '_probe_read_only_cjk', fail)
        monkeypatch.setattr(reader, '_enter_fts_fail_open', no_repair)
        with pytest.raises(exception, match=message):
            reader.search_messages('项目', fields=('id',))
        expected = hermes_state._READ_ONLY_IOERR_RETRY_ATTEMPTS + 1 if message == 'disk I/O error' else 1
        assert attempts == [1] * expected
        assert not reader._conn.in_transaction
        assert not reader._db_corrupt


def test_foreign_data_path_never_selects_neighbor_extension(cjk_so, tmp_path, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    home = tmp_path / 'trusted'
    home.mkdir()
    (home / 'lib').mkdir()
    shutil.copyfile(cjk_so, home / 'lib/libfts5_cjk.so')
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    (foreign / 'lib').mkdir()
    (foreign / 'lib/libfts5_cjk.so').write_bytes(b'not trusted executable code')
    monkeypatch.delenv('HERMES_FTS5_CJK_SO', raising=False)
    monkeypatch.setenv('HERMES_HOME', str(home))
    token = set_hermes_home_override(home)
    try:
        with SessionDB(foreign / 'state.db') as writer:
            assert writer._fts_cjk_loaded
            writer.create_session('history', source='cli')
            writer.append_message('history', role='user', content='中文 项目')
        with SessionDB(foreign / 'state.db', read_only=True) as reader:
            assert reader._fts_cjk_loaded and reader._fts_cjk_available
            assert len(reader.search_messages('项目', fields=('id',))) == 1
        # Foreign data has a neighboring extension, but the trusted route does not.
        (home / 'lib/libfts5_cjk.so').unlink()
        with SessionDB(foreign / 'state.db', read_only=True) as reader:
            assert not reader._fts_cjk_loaded
            assert len(reader.search_messages('项目', fields=('id',))) == 1
    finally:
        reset_hermes_home_override(token)


def test_match_keeps_external_progress_handler_and_cancel_releases_snapshot(cjk_so, tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_FTS5_CJK_SO', str(cjk_so))
    path = tmp_path / 'cancel.db'
    with SessionDB(path) as writer:
        writer.create_session('history', source='cli')
        writer.append_message('history', role='user', content='中文 项目')
    with SessionDB(path, read_only=True) as reader:
        calls = []
        reader._conn.set_progress_handler(lambda: calls.append(1) and 0, 1)
        assert len(reader.search_messages('项目', fields=('id',))) == 1
        assert calls
        calls.clear()
        reader._conn.execute('SELECT 1').fetchone()
        assert calls  # snapshot must not overwrite/uninstall its caller's handler
        reader._conn.set_progress_handler(None, 0)
        original = reader._probe_read_only_cjk
        def cancel_after_probe(cursor):
            ready = original(cursor)
            reader._conn.set_progress_handler(lambda: 1, 1)
            return ready
        monkeypatch.setattr(reader, '_probe_read_only_cjk', cancel_after_probe)
        try:
            with pytest.raises(sqlite3.OperationalError, match='interrupted'):
                reader.search_messages('项目', fields=('id',))
        finally:
            reader._conn.set_progress_handler(None, 0)
        assert not reader._conn.in_transaction
        assert reader._conn.execute('SELECT 1').fetchone()[0] == 1
        with SessionDB(path) as writer:
            writer.append_message('history', role='user', content='中文 later')
        # in_transaction only describes explicit transactions. New committed data
        # also proves no cancelled implicit read cursor still pins the old snapshot.
        assert reader._conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 2
