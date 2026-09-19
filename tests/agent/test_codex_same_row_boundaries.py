"""Exact-row Codex recovery: synthetic grants and real temporary store I/O."""
import base64
import json
import time
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from agent.credential_pool import CredentialPool, PooledCredential
from hermes_cli import auth


def token(generation, expires_in=7200):
    payload = {"sub": "synthetic-user", "https://api.openai.com/auth": {"chatgpt_account_id": "synthetic-account"},
               "exp": int(time.time() + expires_in), "generation": generation}
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    return encode({"alg": "none"}) + "." + encode(payload) + ".synthetic"


@pytest.fixture
def grant(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr(auth, "_import_codex_cli_tokens", lambda: None)
    row = PooledCredential(provider="openai-codex", id="row-a", label="synthetic",
                           auth_type="oauth", source="manual:device_code", priority=0,
                           access_token=token("old", -600), refresh_token="synthetic-grant-0")
    auth.write_credential_pool("openai-codex", [row.to_dict()])
    return row


@pytest.mark.parametrize("force", [False, True])
def test_two_preloaded_pools_exchange_one_grant_once(grant, monkeypatch, force):
    first = CredentialPool("openai-codex", [grant])
    second = CredentialPool("openai-codex", [replace(grant)])
    exchanges = []
    fresh = token("rotated")
    def exchange(access, refresh):
        exchanges.append(refresh)
        return {"access_token": fresh, "refresh_token": "synthetic-grant-1"}
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", exchange)
    assert first._refresh_entry(grant, force=force).access_token == fresh
    assert second._refresh_entry(second.entries()[0], force=force).access_token == fresh
    assert exchanges == ["synthetic-grant-0"]


@pytest.mark.parametrize("rotated", [False, True])
@pytest.mark.parametrize("status", ["dead", "exhausted"])
def test_persisted_unavailable_row_is_not_recovered(grant, monkeypatch, rotated, status):
    pool = CredentialPool("openai-codex", [grant])
    peer = replace(grant, last_status=status, last_status_at=time.time(),
                   last_error_reset_at=time.time() + 3600)
    if rotated:
        peer = replace(peer, access_token=token("peer"), refresh_token="synthetic-peer-grant")
    auth.write_credential_pool("openai-codex", [peer.to_dict()])
    posts = []
    def exchange(access, refresh):
        posts.append(refresh)
        return {"access_token": token("unexpected"), "refresh_token": "unexpected-grant"}
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", exchange)
    result = pool.try_refresh_matching(credential_id=grant.id)
    assert result is None, "a current DEAD/cooling row must not be returned as recovered"
    assert posts == [], "a peer already quarantined this row; do not refresh it"
    assert auth.read_credential_pool("openai-codex")[0]["last_status"] == status


def test_refresh_only_peer_is_exchanged_not_returned_empty(grant, monkeypatch):
    pool = CredentialPool("openai-codex", [grant])
    auth.write_credential_pool("openai-codex", [replace(grant, access_token="", refresh_token="synthetic-peer-only").to_dict()])
    posts = []
    fresh = token("recovered")
    def exchange(access, refresh):
        posts.append((access, refresh))
        return {"access_token": fresh, "refresh_token": "synthetic-next"}
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", exchange)
    result = pool.try_refresh_matching(credential_id=grant.id)
    assert result is not None and result.access_token == fresh
    assert posts == [("", "synthetic-peer-only")]


def test_expired_peer_uses_updated_grant(grant, monkeypatch):
    pool = CredentialPool("openai-codex", [grant])
    auth.write_credential_pool("openai-codex", [replace(grant, access_token=token("expired-peer", -600), refresh_token="synthetic-peer-only").to_dict()])
    posts = []
    def exchange(access, refresh):
        posts.append(refresh)
        return {"access_token": token("recovered"), "refresh_token": "synthetic-next"}
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", exchange)
    assert pool.try_refresh_matching(credential_id=grant.id) is not None
    assert posts == ["synthetic-peer-only"]


def test_elapsed_cooldown_does_not_prevent_recovery(grant, monkeypatch):
    pool = CredentialPool("openai-codex", [grant])
    peer = replace(grant, last_status="exhausted", last_status_at=time.time() - 7200,
                   last_error_reset_at=time.time() - 60)
    auth.write_credential_pool("openai-codex", [peer.to_dict()])
    posts = []
    def exchange(access, refresh):
        posts.append(refresh)
        return {"access_token": token("after-cooldown"), "refresh_token": "synthetic-next"}
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", exchange)
    recovered = pool.try_refresh_matching(credential_id=grant.id)
    assert recovered is not None and recovered.last_status == "ok"
    assert posts == [grant.refresh_token]


def test_peer_reauthentication_can_clear_stale_dead_state(grant, monkeypatch):
    stale = replace(grant, last_status="dead", last_status_at=time.time())
    pool = CredentialPool("openai-codex", [stale])
    fresh = replace(grant, access_token=token("reauthenticated"), refresh_token="synthetic-reauth", last_status="ok")
    auth.write_credential_pool("openai-codex", [fresh.to_dict()])
    monkeypatch.setattr(auth, "refresh_codex_oauth_pure", lambda *args: pytest.fail("fresh reauthentication must be adopted"))
    recovered = pool.try_refresh_matching(credential_id=grant.id)
    assert recovered is not None and recovered.access_token == fresh.access_token
    assert recovered.last_status == "ok"


def test_two_real_processes_share_one_refresh_exchange(grant, tmp_path):
    child = '''
import os, time
from pathlib import Path
from agent.credential_pool import CredentialPool, PooledCredential
from hermes_cli import auth
root = Path(os.environ["SYNTH_BARRIER"])
entry = PooledCredential.from_dict("openai-codex", auth.read_credential_pool("openai-codex")[0])
pool = CredentialPool("openai-codex", [entry])
(root / ("ready-" + os.environ["SYNTH_CHILD"])).touch()
deadline = time.monotonic() + 10
while not (root / "go").exists():
    if time.monotonic() > deadline:
        raise TimeoutError("barrier not released")
    time.sleep(0.01)
def exchange(access, refresh):
    attempts = root / "exchanges"
    with attempts.open("a") as log:
        log.write("attempt\\n")
    assert len(attempts.read_text().splitlines()) == 1, "single-use grant was replayed"
    return {"access_token": os.environ["SYNTH_FRESH"], "refresh_token": "synthetic-final"}
auth.refresh_codex_oauth_pure = exchange
result = pool.try_refresh_matching(credential_id=entry.id)
assert result is not None and result.access_token == os.environ["SYNTH_FRESH"]
'''
    env = dict(os.environ, SYNTH_BARRIER=str(tmp_path), SYNTH_FRESH=token("process-peer"))
    children = [subprocess.Popen([sys.executable, "-c", child], env=dict(env, SYNTH_CHILD=str(i)),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for i in range(2)]
    try:
        deadline = time.monotonic() + 10
        while not all((tmp_path / f"ready-{i}").exists() for i in range(2)):
            assert time.monotonic() < deadline, "children failed to preload"
            time.sleep(0.01)
        (tmp_path / "go").touch()
        results = [(p, p.communicate(timeout=15)) for p in children]
        assert all(p.returncode == 0 for p, _ in results), [output for _, output in results]
        assert (tmp_path / "exchanges").read_text().splitlines() == ["attempt"]
    finally:
        for p in children:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=5)
