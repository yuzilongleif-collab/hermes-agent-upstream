"""Account/grant ownership regressions; synthetic credentials only."""
import json
from dataclasses import replace

import pytest

from agent.credential_pool import CredentialPool, PooledCredential, STATUS_DEAD
from hermes_cli import auth as A
from agent import credential_pool as CP


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('HERMES_HOME', str(home))
    path = home / 'auth.json'
    monkeypatch.setattr(A, '_auth_file_path', lambda: path)
    monkeypatch.setattr(A, '_global_auth_file_path', lambda: None)
    monkeypatch.setattr(CP, '_global_auth_file_path', lambda: None)
    monkeypatch.setattr(A, '_import_codex_cli_tokens', lambda: None)
    def entry(id, source, priority):
        return PooledCredential(provider='openai-codex', id=id, label=id,
                                auth_type='oauth', priority=priority, source=source,
                                access_token=id + '-access', refresh_token=id + '-refresh')
    manual = entry('independent', 'manual:device_code', 0)
    seeded = entry('default', 'device_code', 1)
    sibling = entry('sibling', 'manual:device_code', 2)
    store = {'version': 1, 'active_provider': 'anthropic',
             'providers': {'openai-codex': {'tokens': {
                 'access_token': seeded.access_token, 'refresh_token': seeded.refresh_token}}},
             'credential_pool': {'openai-codex': [x.to_dict() for x in [manual, seeded, sibling]]}}
    path.write_text(json.dumps(store), encoding="utf-8")
    return path, CredentialPool('openai-codex', [manual, seeded, sibling]), manual, store


def test_two_preloaded_pools_consume_refresh_grant_once(isolated, monkeypatch):
    path, winner, manual, store = isolated
    waiter = CredentialPool('openai-codex', list(winner.entries()))
    posted = []
    def refresh(access, refresh):
        posted.append((access, refresh))
        return {'access_token': 'rotated-access', 'refresh_token': 'rotated-refresh'}
    monkeypatch.setattr(A, 'refresh_codex_oauth_pure', refresh)
    assert winner._refresh_entry(manual, force=True).access_token == 'rotated-access'
    assert waiter._refresh_entry(manual, force=True).access_token == 'rotated-access'
    assert posted == [(manual.access_token, manual.refresh_token)]


def test_seeded_refresh_only_singleton_still_adopts(isolated):
    path, pool, manual, store = isolated
    seeded = pool.entries()[1]
    store['providers']['openai-codex']['tokens'] = {'refresh_token': 'rotated-default-refresh'}
    path.write_text(json.dumps(store), encoding="utf-8")
    result = pool._sync_entry_from_auth_store(seeded)
    assert result.access_token == seeded.access_token
    assert result.refresh_token == 'rotated-default-refresh'
    assert pool.entries()[0] == manual


def test_manual_never_adopts_default_grant(isolated):
    path, pool, manual, before = isolated
    assert pool._sync_entry_from_auth_store(manual) is manual
    assert json.loads(path.read_text(encoding="utf-8")) == before


def test_refresh_posts_only_named_grant(isolated, monkeypatch):
    path, pool, manual, before = isolated
    posted = []
    def refresh(access, refresh):
        posted.append((access, refresh))
        return {'access_token': 'new-access', 'refresh_token': 'new-refresh'}
    monkeypatch.setattr(A, 'refresh_codex_oauth_pure', refresh)
    result = pool._refresh_entry(manual, force=True)
    assert posted == [(manual.access_token, manual.refresh_token)]
    assert result.access_token == 'new-access'
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after['providers'] == before['providers']
    assert after['active_provider'] == before['active_provider']
    assert after['credential_pool']['openai-codex'][0]['access_token'] == 'new-access'
    assert after['credential_pool']['openai-codex'][0]['refresh_token'] == 'new-refresh'
    assert after['credential_pool']['openai-codex'][1:] == before['credential_pool']['openai-codex'][1:]


@pytest.mark.parametrize('force', [False, True])
def test_waiter_adopts_same_row_rotation_without_replaying(isolated, monkeypatch, force):
    path, pool, manual, before = isolated
    before['credential_pool']['openai-codex'][0].update(access_token='peer-access', refresh_token='peer-refresh')
    path.write_text(json.dumps(before), encoding="utf-8")
    def forbidden(*args, **kwargs):
        pytest.fail('peer already rotated this grant; must not POST again')
    monkeypatch.setattr(A, 'refresh_codex_oauth_pure', forbidden)
    result = pool._refresh_entry(manual, force=force)
    assert result.access_token == 'peer-access'
    assert result.refresh_token == 'peer-refresh'


@pytest.mark.parametrize('code', sorted(A.OAUTH_PROVIDER_FLOWS['openai-codex'].terminal_refresh_codes))
def test_terminal_failure_excludes_only_named_entry(isolated, monkeypatch, code):
    path, pool, manual, before = isolated
    def fail(*args):
        raise A.AuthError('Synthetic terminal failure', provider='openai-codex', code=code, relogin_required=True)
    monkeypatch.setattr(A, 'refresh_codex_oauth_pure', fail)
    assert pool._refresh_entry(manual, force=True) is None
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after['providers'] == before['providers']
    assert after['active_provider'] == before['active_provider']
    rows = after['credential_pool']['openai-codex']
    assert rows[1:] == before['credential_pool']['openai-codex'][1:]
    assert rows[0]['last_status'] == STATUS_DEAD
    pool._replace_entry(pool.entries()[0], replace(pool.entries()[0], last_status_at=1))
    available, pending = pool._available_entries(clear_expired=True, refresh=False)
    assert manual.id not in {e.id for e in available + pending}


def test_seeded_entry_still_adopts_default_rotation(isolated):
    path, pool, manual, before = isolated
    seeded = pool.entries()[1]
    before['providers']['openai-codex']['tokens'].update(access_token='default-new', refresh_token='default-new-r')
    path.write_text(json.dumps(before), encoding="utf-8")
    result = pool._sync_entry_from_auth_store(seeded)
    assert result.access_token == 'default-new'
    assert result.refresh_token == 'default-new-r'
    assert pool.entries()[0] == manual


def test_pool_only_fallback_skips_dead_manual(isolated):
    from hermes_cli.auth_codex import _pool_codex_access_token
    path, pool, manual, store = isolated
    store['providers'] = {}
    store['credential_pool']['openai-codex'][0].update(last_status='dead', last_error_reset_at=None)
    path.write_text(json.dumps(store), encoding="utf-8")
    assert _pool_codex_access_token() == 'default-access'


@pytest.mark.parametrize('status', ['dead', 'exhausted'])
def test_peer_rotation_does_not_bypass_unavailable_status(isolated, monkeypatch, status):
    import time
    path, pool, manual, store = isolated
    store['credential_pool']['openai-codex'][0].update(
        access_token='peer-access', refresh_token='peer-refresh', last_status=status,
        last_status_at=time.time(), last_error_reset_at=time.time() + 600)
    path.write_text(json.dumps(store), encoding="utf-8")
    monkeypatch.setattr(A, 'refresh_codex_oauth_pure', lambda *args: pytest.fail('unavailable grant'))
    assert pool._refresh_entry(manual, force=True) is None


def test_expired_peer_rotation_refreshes_the_peer_not_default(isolated, monkeypatch):
    path, pool, manual, store = isolated
    store['credential_pool']['openai-codex'][0].update(access_token='peer-access', refresh_token='peer-refresh')
    path.write_text(json.dumps(store), encoding="utf-8")
    monkeypatch.setattr(pool, '_entry_needs_refresh', lambda e: True)
    seen = []
    def refresh(access, refresh):
        seen.append((access, refresh))
        return {'access_token': 'latest-access', 'refresh_token': 'latest-refresh'}
    monkeypatch.setattr(A, 'refresh_codex_oauth_pure', refresh)
    assert pool._refresh_entry(manual, force=True).access_token == 'latest-access'
    assert seen == [('peer-access', 'peer-refresh')]


@pytest.mark.parametrize('change', [
    {'source': 'device_code', 'access_token': 'peer-access'},
    {'access_token': '', 'refresh_token': ''},
])
def test_pool_adoption_rejects_wrong_source_or_empty_row(isolated, change):
    path, pool, manual, store = isolated
    store['credential_pool']['openai-codex'][0].update(change)
    path.write_text(json.dumps(store), encoding="utf-8")
    assert pool._sync_entry_from_pool_store(manual) is manual


@pytest.mark.parametrize('status', ['dead', 'exhausted'])
def test_peer_status_only_change_blocks_refresh(isolated, monkeypatch, status):
    import time
    path, pool, manual, store = isolated
    store['credential_pool']['openai-codex'][0].update(
        last_status=status, last_status_at=time.time(),
        last_error_reset_at=time.time() + 600)
    path.write_text(json.dumps(store), encoding="utf-8")
    monkeypatch.setattr(A, 'refresh_codex_oauth_pure', lambda *a: pytest.fail('known unavailable grant'))
    assert pool._refresh_entry(manual, force=True) is None


def test_refresh_token_only_peer_row_is_refreshed_not_returned(isolated, monkeypatch):
    path, pool, manual, store = isolated
    store['credential_pool']['openai-codex'][0].update(access_token='', refresh_token='peer-refresh')
    path.write_text(json.dumps(store), encoding="utf-8")
    posted = []
    def refresh(access, refresh):
        posted.append((access, refresh))
        return {'access_token': 'usable-access', 'refresh_token': 'latest-refresh'}
    monkeypatch.setattr(A, 'refresh_codex_oauth_pure', refresh)
    result = pool._refresh_entry(manual, force=True)
    assert posted == [('', 'peer-refresh')]
    assert result.access_token == 'usable-access'


@pytest.mark.parametrize('terminal', [False, True])
def test_failure_preserves_access_only_peer_rotation(isolated, terminal):
    path, pool, manual, store = isolated
    store['credential_pool']['openai-codex'][0]['access_token'] = 'peer-access'
    path.write_text(json.dumps(store), encoding="utf-8")
    exc = A.AuthError('synthetic', provider='openai-codex', code='invalid_grant', relogin_required=True) if terminal else RuntimeError('synthetic')
    result = pool._recover_failed_refresh(manual, exc)
    assert result.access_token == 'peer-access'
    assert result.last_status != STATUS_DEAD
    assert json.loads(path.read_text(encoding="utf-8")) == store


@pytest.mark.parametrize('state', ['dead', 'exhausted', 'expired', 'missing_access'])
def test_failure_does_not_return_unusable_peer(isolated, monkeypatch, state):
    import time
    path, pool, manual, store = isolated
    row = store['credential_pool']['openai-codex'][0]
    row.update(access_token='peer-access', refresh_token='peer-refresh')
    if state in {'dead', 'exhausted'}:
        row.update(last_status=state, last_status_at=time.time(), last_error_reset_at=time.time() + 600)
    elif state == 'missing_access':
        row['access_token'] = ''
    else:
        monkeypatch.setattr(pool, '_entry_needs_refresh', lambda e: True)
    path.write_text(json.dumps(store), encoding="utf-8")
    exc = A.AuthError('synthetic', provider='openai-codex', code='invalid_grant', relogin_required=True)
    assert pool._recover_failed_refresh(manual, exc) is None
    assert json.loads(path.read_text(encoding="utf-8")) == store
