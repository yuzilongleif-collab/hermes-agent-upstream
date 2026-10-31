"""Real Agent registry discovery uses its standalone RO tokenizer, not a writer pool."""
import json
from pathlib import Path

import pytest

from hermes_state import SessionDB

pytest_plugins = ('tests.hermes_state.test_fts_cjk_bigram',)


@pytest.mark.parametrize('consumer', ['standalone', 'managed', 'foreign', 'injected'])
def test_agent_discovery_loads_once_and_uses_owned_cjk(cjk_so, tmp_path, monkeypatch, consumer):
    from agent.runtime_session_store import RuntimeSessionStore
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    import hermes_state
    import hermes_state_fts
    import tools.session_search_tool  # registers the real handler
    from tools.registry import registry

    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    home = tmp_path / '.hermes'
    home.mkdir()
    token = set_hermes_home_override(home)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_FTS5_CJK_SO', str(cjk_so))
    target = home / 'profiles/foreign' if consumer == 'foreign' else home
    target.mkdir(parents=True, exist_ok=True)
    writer = SessionDB(target / 'state.db')
    try:
        writer.create_session('both', source='cli')
        writer.create_session('other', source='cli')
        writer.append_message('both', role='user', content='中文 数据 alpha')
        writer.append_message('other', role='user', content='中文 其他 omega')
        original = SessionDB._connect_read_only
        loader = hermes_state_fts.load_fts5_cjk_extension
        loads, statements, connections = [], [], []

        def connect(store, timeout):
            conn = original(store, timeout)
            conn.execute('PRAGMA query_only=ON')
            conn.set_trace_callback(statements.append)
            connections.append(conn)
            return conn

        def load(conn):
            loads.append(conn)
            return loader(conn)

        monkeypatch.setattr(SessionDB, '_connect_read_only', connect)
        monkeypatch.setattr(hermes_state, 'load_fts5_cjk_extension', load)
        monkeypatch.setattr(hermes_state_fts, 'load_fts5_cjk_extension', load)
        kwargs = {}
        if consumer == 'managed':
            def owner_rpc(*args, **kwargs):
                raise AssertionError('discovery must not borrow an owner RPC/writer')
            kwargs['db'] = RuntimeSessionStore(owner_rpc, {'session_id': 'worker'}, home / 'outbox')
        elif consumer == 'injected':
            kwargs['db'] = writer
        args = {'query': '中文 数据', 'limit': 3}
        if consumer == 'foreign':
            args['profile'] = 'foreign'
        result = json.loads(registry.get_entry('session_search').handler(args, **kwargs))
        assert result['success'] is True, result
        assert [row['session_id'] for row in result['results']] == ['both']
        assert any('messages_fts_cjk MATCH' in sql for sql in statements)
        assert not any(' LIKE ' in sql.upper() and 'M.CONTENT' in sql.upper() for sql in statements)
        assert len(connections) == 1
        assert loads == connections  # the opener owns loading; no second preserve-CJK path
        import sqlite3
        for conn in connections:
            with pytest.raises(sqlite3.ProgrammingError):
                conn.execute('SELECT 1')
        assert writer._conn.execute('SELECT 1').fetchone()[0] == 1
    finally:
        writer.close()
        reset_hermes_home_override(token)
