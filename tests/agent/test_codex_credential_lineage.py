"""Behavioral regressions for Codex singleton vs independent-manual lineage.

These tests drive the real production paths:

* ``hermes_cli.auth._save_codex_tokens`` / ``_sync_codex_pool_entries``
* ``CredentialPool._sync_codex_entry_from_auth_store``
* ``CredentialPool._refresh_entry`` / ``_refresh_entry_impl``
* ``CredentialPool._sync_device_code_entry_to_auth_store``
* ``PooledCredential.from_dict`` / ``to_dict`` plus ``write_credential_pool``

Network is mocked only at ``refresh_codex_oauth_pure``. Auth stores live under
``tmp_path`` — never the real ``~/.hermes`` tree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import credential_pool as CP
from agent.credential_pool import (
    AUTH_TYPE_OAUTH,
    STATUS_DEAD,
    STATUS_EXHAUSTED,
    CredentialPool,
    PooledCredential,
)
from hermes_cli import auth as A
from hermes_cli.auth import AuthError, _save_codex_tokens, write_credential_pool


PROVIDER = "openai-codex"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _isolate_auth_home(tmp_path, monkeypatch, *, hermes_home: Path | None = None) -> Path:
    """Point every auth-store lookup at a tmp Hermes home. Never the real user store."""
    home = hermes_home or (tmp_path / "hermes")
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "not-the-real-home"))
    monkeypatch.setattr(A, "_global_auth_file_path", lambda: None)
    monkeypatch.setattr(CP, "_global_auth_file_path", lambda: None)
    return home


def _pool_entry(
    *,
    entry_id: str,
    source: str,
    access_token: str,
    refresh_token: str,
    extra: dict | None = None,
    label: str = "cred",
) -> PooledCredential:
    entry = PooledCredential(
        provider=PROVIDER,
        id=entry_id,
        label=label,
        auth_type=AUTH_TYPE_OAUTH,
        priority=0,
        source=source,
        access_token=access_token,
        refresh_token=refresh_token,
    )
    if extra:
        entry.extra.update(extra)
    return entry


def _auth_store(
    *,
    singleton_access: str | None,
    singleton_refresh: str | None,
    pool: list[dict],
    include_provider: bool = True,
) -> dict:
    store: dict = {
        "version": 1,
        "credential_pool": {PROVIDER: pool},
    }
    if include_provider and singleton_access is not None:
        store["providers"] = {
            PROVIDER: {
                "tokens": {
                    "access_token": singleton_access,
                    "refresh_token": singleton_refresh,
                },
                "auth_mode": "chatgpt",
                "last_refresh": "2026-01-01T00:00:00Z",
            }
        }
    return store


def _raw_pool_row(
    *,
    entry_id: str,
    source: str,
    access_token: str,
    refresh_token: str,
    label: str = "cred",
    **extra,
) -> dict:
    row = {
        "id": entry_id,
        "label": label,
        "source": source,
        "auth_type": "oauth",
        "access_token": access_token,
        "refresh_token": refresh_token,
    }
    row.update(extra)
    return row


def _reload_entry(entry: PooledCredential) -> PooledCredential:
    """Force the production serialization boundary: to_dict -> from_dict."""
    return PooledCredential.from_dict(PROVIDER, entry.to_dict())


def _entry_from_store(store: dict, entry_id: str) -> PooledCredential:
    payload = next(row for row in store["credential_pool"][PROVIDER] if row["id"] == entry_id)
    return PooledCredential.from_dict(PROVIDER, payload)


def test_independent_manual_read_side_does_not_adopt_singleton(tmp_path, monkeypatch):
    """A. Distinct manual:device_code with no proven alias lineage stays isolated."""
    home = _isolate_auth_home(tmp_path, monkeypatch)
    _write_json(
        home / "auth.json",
        _auth_store(
            singleton_access="A",
            singleton_refresh="RA",
            pool=[
                _raw_pool_row(
                    entry_id="manual-b",
                    source="manual:device_code",
                    access_token="B",
                    refresh_token="RB",
                ),
            ],
        ),
    )

    entry = _pool_entry(
        entry_id="manual-b",
        source="manual:device_code",
        access_token="B",
        refresh_token="RB",
    )
    pool = CredentialPool(PROVIDER, [entry])
    synced = pool._sync_codex_entry_from_auth_store(entry)

    assert synced.access_token == "B"
    assert synced.refresh_token == "RB"
    store = _read_json(home / "auth.json")
    tokens = store["providers"][PROVIDER]["tokens"]
    assert tokens["access_token"] == "A"
    assert tokens["refresh_token"] == "RA"
    persisted = store["credential_pool"][PROVIDER][0]
    assert persisted["access_token"] == "B"
    assert persisted["refresh_token"] == "RB"


def test_independent_manual_refresh_uses_own_token_and_does_not_touch_singleton(
    tmp_path, monkeypatch
):
    """B. Independent refresh presents RB, updates only itself, never quarantines."""
    home = _isolate_auth_home(tmp_path, monkeypatch)
    _write_json(
        home / "auth.json",
        _auth_store(
            singleton_access="A",
            singleton_refresh="RA",
            pool=[
                _raw_pool_row(
                    entry_id="seeded",
                    source="device_code",
                    access_token="A",
                    refresh_token="RA",
                ),
                _raw_pool_row(
                    entry_id="manual-b",
                    source="manual:device_code",
                    access_token="B",
                    refresh_token="RB",
                ),
            ],
        ),
    )

    presented: list[tuple[str, str]] = []

    def fake_refresh(access_token, refresh_token, **kwargs):
        presented.append((access_token, refresh_token))
        return {
            "access_token": "B2",
            "refresh_token": "RB2",
            "last_refresh": "2026-08-25T00:00:00Z",
        }

    monkeypatch.setattr(A, "refresh_codex_oauth_pure", fake_refresh)

    manual = _pool_entry(
        entry_id="manual-b",
        source="manual:device_code",
        access_token="B",
        refresh_token="RB",
    )
    seeded = _pool_entry(
        entry_id="seeded",
        source="device_code",
        access_token="A",
        refresh_token="RA",
    )
    pool = CredentialPool(PROVIDER, [seeded, manual])
    refreshed = pool._refresh_entry(manual, force=True)

    assert presented == [("B", "RB")]
    assert refreshed is not None
    assert refreshed.id == "manual-b"
    assert refreshed.access_token == "B2"
    assert refreshed.refresh_token == "RB2"

    store = _read_json(home / "auth.json")
    tokens = store["providers"][PROVIDER]["tokens"]
    assert tokens["access_token"] == "A"
    assert tokens["refresh_token"] == "RA"
    assert "last_auth_error" not in store["providers"][PROVIDER]

    by_id = {row["id"]: row for row in store["credential_pool"][PROVIDER]}
    assert by_id["manual-b"]["access_token"] == "B2"
    assert by_id["manual-b"]["refresh_token"] == "RB2"
    assert by_id["seeded"]["access_token"] == "A"
    assert by_id["seeded"]["refresh_token"] == "RA"


def test_singleton_force_refresh_skips_post_after_in_lock_resync(
    tmp_path, monkeypatch
):
    """force=True must not spend a singleton token another process rotated."""
    home = _isolate_auth_home(tmp_path, monkeypatch)
    _write_json(
        home / "auth.json",
        _auth_store(
            singleton_access="A2",
            singleton_refresh="RA2",
            pool=[
                _raw_pool_row(
                    entry_id="seeded",
                    source="device_code",
                    access_token="A",
                    refresh_token="RA",
                )
            ],
        ),
    )

    def unexpected_refresh(*args, **kwargs):
        raise AssertionError("in-lock singleton resync must skip a second POST")

    monkeypatch.setattr(A, "refresh_codex_oauth_pure", unexpected_refresh)
    stale = _pool_entry(
        entry_id="seeded",
        source="device_code",
        access_token="A",
        refresh_token="RA",
    )
    pool = CredentialPool(PROVIDER, [stale])

    result = pool._refresh_entry(stale, force=True)

    assert result is not None
    assert result.access_token == "A2"
    assert result.refresh_token == "RA2"


def test_singleton_force_refresh_exchanges_new_refresh_token_when_access_stale(
    tmp_path, monkeypatch
):
    """A refresh-token-only resync must still exchange the new token."""
    home = _isolate_auth_home(tmp_path, monkeypatch)
    expired_access = "e30.eyJleHAiOjF9.sig"
    _write_json(
        home / "auth.json",
        _auth_store(
            singleton_access=expired_access,
            singleton_refresh="RA2",
            pool=[
                _raw_pool_row(
                    entry_id="seeded",
                    source="device_code",
                    access_token=expired_access,
                    refresh_token="RA",
                )
            ],
        ),
    )

    presented: list[tuple[str, str]] = []

    def fake_refresh(access_token, refresh_token, **kwargs):
        presented.append((access_token, refresh_token))
        return {
            "access_token": "A3",
            "refresh_token": "RA3",
            "last_refresh": "2026-08-27T00:00:00Z",
        }

    monkeypatch.setattr(A, "refresh_codex_oauth_pure", fake_refresh)
    stale = _pool_entry(
        entry_id="seeded",
        source="device_code",
        access_token=expired_access,
        refresh_token="RA",
    )
    pool = CredentialPool(PROVIDER, [stale])

    result = pool._refresh_entry(stale, force=True)

    assert presented == [(expired_access, "RA2")]
    assert result is not None
    assert result.access_token == "A3"
    assert result.refresh_token == "RA3"


def test_independent_manual_force_refresh_adopts_rotated_pool_row(
    tmp_path, monkeypatch
):
    """A lock waiter must not replay a single-use token the winner rotated."""
    home = _isolate_auth_home(tmp_path, monkeypatch)
    _write_json(
        home / "auth.json",
        _auth_store(
            singleton_access="A",
            singleton_refresh="RA",
            pool=[
                _raw_pool_row(
                    entry_id="seeded",
                    source="device_code",
                    access_token="A",
                    refresh_token="RA",
                ),
                _raw_pool_row(
                    entry_id="manual-b",
                    source="manual:device_code",
                    access_token="B2",
                    refresh_token="RB2",
                ),
            ],
        ),
    )

    refresh_calls = []

    def unexpected_refresh(*args, **kwargs):
        refresh_calls.append((args, kwargs))
        raise AssertionError("rotated pool row must skip a second refresh POST")

    monkeypatch.setattr(A, "refresh_codex_oauth_pure", unexpected_refresh)
    stale = _pool_entry(
        entry_id="manual-b",
        source="manual:device_code",
        access_token="B",
        refresh_token="RB",
    )
    seeded = _pool_entry(
        entry_id="seeded",
        source="device_code",
        access_token="A",
        refresh_token="RA",
    )
    pool = CredentialPool(PROVIDER, [seeded, stale])

    result = pool._refresh_entry(stale, force=True)

    assert result is not None
    assert result.access_token == "B2"
    assert result.refresh_token == "RB2"
    assert refresh_calls == []
    in_memory = next(item for item in pool.entries() if item.id == "manual-b")
    assert in_memory.access_token == "B2"
    assert in_memory.refresh_token == "RB2"
    store = _read_json(home / "auth.json")
    singleton = store["providers"][PROVIDER]["tokens"]
    assert singleton == {"access_token": "A", "refresh_token": "RA"}


def test_missing_codex_refresh_token_marks_dead_without_post(
    tmp_path, monkeypatch
):
    """The real missing-refresh path must be terminal and diagnosable."""
    home = _isolate_auth_home(tmp_path, monkeypatch)
    _write_json(
        home / "auth.json",
        _auth_store(
            singleton_access=None,
            singleton_refresh=None,
            include_provider=False,
            pool=[
                _raw_pool_row(
                    entry_id="manual-b",
                    source="manual:device_code",
                    access_token="B",
                    refresh_token="",
                )
            ],
        ),
    )

    def unexpected_refresh(*args, **kwargs):
        raise AssertionError("missing refresh token must not make a refresh POST")

    monkeypatch.setattr(A, "refresh_codex_oauth_pure", unexpected_refresh)
    manual = _pool_entry(
        entry_id="manual-b",
        source="manual:device_code",
        access_token="B",
        refresh_token="",
    )
    pool = CredentialPool(PROVIDER, [manual])

    result = pool._refresh_entry(manual, force=True)

    assert result is None
    row = _read_json(home / "auth.json")["credential_pool"][PROVIDER][0]
    assert row["last_status"] == STATUS_DEAD
    assert row["last_error_reason"] == "codex_auth_missing_refresh_token"
    in_memory = pool.entries()[0]
    assert in_memory.last_status == STATUS_DEAD
    assert in_memory.last_error_reason == "codex_auth_missing_refresh_token"


def test_explicit_non_401_status_never_becomes_dead(tmp_path, monkeypatch):
    """A terminal-looking reason cannot override a transient HTTP status."""
    home = _isolate_auth_home(tmp_path, monkeypatch)
    _write_json(
        home / "auth.json",
        _auth_store(
            singleton_access=None,
            singleton_refresh=None,
            include_provider=False,
            pool=[
                _raw_pool_row(
                    entry_id="manual-b",
                    source="manual:device_code",
                    access_token="B",
                    refresh_token="RB",
                )
            ],
        ),
    )
    manual = _pool_entry(
        entry_id="manual-b",
        source="manual:device_code",
        access_token="B",
        refresh_token="RB",
    )
    pool = CredentialPool(PROVIDER, [manual])

    updated = pool._mark_exhausted(
        manual,
        429,
        {"reason": "invalid_grant", "message": "transient rate limit"},
    )

    assert updated.last_status == STATUS_EXHAUSTED
    assert updated.last_error_code == 429
    assert updated.last_error_reason == "invalid_grant"


@pytest.mark.parametrize(
    ("error_code", "expected_status", "expected_reason"),
    [
        ("refresh_token_reused", STATUS_DEAD, "refresh_token_reused"),
        ("codex_refresh_failed", STATUS_EXHAUSTED, "codex_refresh_failed"),
    ],
)
def test_independent_manual_refresh_failure_does_not_quarantine_singleton(
    tmp_path, monkeypatch, caplog, error_code, expected_status, expected_reason
):
    """B (failure path). Independent refresh failure must not wipe singleton."""
    home = _isolate_auth_home(tmp_path, monkeypatch)
    _write_json(
        home / "auth.json",
        _auth_store(
            singleton_access="A",
            singleton_refresh="RA",
            pool=[
                _raw_pool_row(
                    entry_id="seeded",
                    source="device_code",
                    access_token="A",
                    refresh_token="RA",
                ),
                _raw_pool_row(
                    entry_id="manual-b",
                    source="manual:device_code",
                    access_token="B",
                    refresh_token="RB",
                ),
            ],
        ),
    )

    presented: list[tuple[str, str]] = []

    def boom(access_token, refresh_token, **kwargs):
        presented.append((access_token, refresh_token))
        raise AuthError(
            error_code,
            provider=PROVIDER,
            code=error_code,
            relogin_required=True,
        )

    monkeypatch.setattr(A, "refresh_codex_oauth_pure", boom)

    manual = _pool_entry(
        entry_id="manual-b",
        source="manual:device_code",
        access_token="B",
        refresh_token="RB",
    )
    seeded = _pool_entry(
        entry_id="seeded",
        source="device_code",
        access_token="A",
        refresh_token="RA",
    )
    pool = CredentialPool(PROVIDER, [seeded, manual])
    result = pool._refresh_entry(manual, force=True)

    assert presented == [("B", "RB")]
    assert result is None
    store = _read_json(home / "auth.json")
    tokens = store["providers"][PROVIDER]["tokens"]
    assert tokens["access_token"] == "A"
    assert tokens["refresh_token"] == "RA"
    assert "last_auth_error" not in store["providers"][PROVIDER]
    by_id = {row["id"]: row for row in store["credential_pool"][PROVIDER]}
    assert "seeded" in by_id
    assert by_id["seeded"]["access_token"] == "A"
    assert by_id["seeded"]["refresh_token"] == "RA"
    assert "manual-b" in by_id
    assert by_id["manual-b"]["last_status"] == expected_status
    assert by_id["manual-b"]["last_error_code"] is None
    assert by_id["manual-b"]["last_error_reason"] == expected_reason
    assert by_id["manual-b"]["last_error_message"] == error_code
    manual_after = next(item for item in pool.entries() if item.id == "manual-b")
    assert manual_after.last_status == expected_status
    assert manual_after.last_error_reason == expected_reason
    assert any(
        record.levelname == "WARNING"
        and expected_reason in record.getMessage()
        and expected_status in record.getMessage()
        for record in caplog.records
    )
    in_memory_ids = {item.id for item in pool.entries()}
    assert "seeded" in in_memory_ids


def test_legacy_alias_creation_stamps_only_proven_alias(tmp_path, monkeypatch):
    """C + I. Previous-access-token equality creates durable alias lineage only."""
    home = _isolate_auth_home(tmp_path, monkeypatch)
    _write_json(
        home / "auth.json",
        _auth_store(
            singleton_access="old-at",
            singleton_refresh="old-rt",
            pool=[
                _raw_pool_row(
                    entry_id="seeded",
                    source="device_code",
                    access_token="old-at",
                    refresh_token="old-rt",
                ),
                _raw_pool_row(
                    entry_id="legacy-alias",
                    source="manual:device_code",
                    access_token="old-at",
                    refresh_token="old-rt",
                    label="legacy",
                ),
                _raw_pool_row(
                    entry_id="independent",
                    source="manual:device_code",
                    access_token="B",
                    refresh_token="RB",
                    label="independent",
                ),
            ],
        ),
    )

    _save_codex_tokens(
        {"access_token": "fresh-at", "refresh_token": "fresh-rt"},
        last_refresh="2026-08-25T00:00:00Z",
    )

    store = _read_json(home / "auth.json")
    by_id = {row["id"]: row for row in store["credential_pool"][PROVIDER]}

    assert by_id["seeded"]["access_token"] == "fresh-at"
    assert by_id["seeded"]["refresh_token"] == "fresh-rt"

    alias = by_id["legacy-alias"]
    assert alias["access_token"] == "fresh-at"
    assert alias["refresh_token"] == "fresh-rt"
    assert alias.get("codex_lineage") == "singleton_alias"
    assert "old-at" not in json.dumps(alias.get("codex_lineage"))
    assert "fresh-at" not in json.dumps(alias.get("codex_lineage"))
    assert "fresh-rt" not in json.dumps(alias.get("codex_lineage"))

    independent = by_id["independent"]
    assert independent["access_token"] == "B"
    assert independent["refresh_token"] == "RB"
    assert independent.get("codex_lineage") != "singleton_alias"


def test_alias_lineage_survives_pooledcredential_and_auth_store_reload(
    tmp_path, monkeypatch
):
    """D. Lineage survives to_dict/from_dict and write_credential_pool reload."""
    home = _isolate_auth_home(tmp_path, monkeypatch)
    _write_json(
        home / "auth.json",
        _auth_store(
            singleton_access="old-at",
            singleton_refresh="old-rt",
            pool=[
                _raw_pool_row(
                    entry_id="legacy-alias",
                    source="manual:device_code",
                    access_token="old-at",
                    refresh_token="old-rt",
                ),
                _raw_pool_row(
                    entry_id="independent",
                    source="manual:device_code",
                    access_token="B",
                    refresh_token="RB",
                ),
            ],
        ),
    )
    _save_codex_tokens(
        {"access_token": "fresh-at", "refresh_token": "fresh-rt"},
        last_refresh="2026-08-25T00:00:00Z",
    )

    store = _read_json(home / "auth.json")
    alias = _entry_from_store(store, "legacy-alias")
    independent = _entry_from_store(store, "independent")

    assert alias.extra.get("codex_lineage") == "singleton_alias"
    assert independent.extra.get("codex_lineage") != "singleton_alias"

    reloaded_alias = _reload_entry(alias)
    reloaded_independent = _reload_entry(independent)
    assert reloaded_alias.extra.get("codex_lineage") == "singleton_alias"
    assert reloaded_independent.extra.get("codex_lineage") != "singleton_alias"

    write_credential_pool(
        PROVIDER,
        [reloaded_alias.to_dict(), reloaded_independent.to_dict()],
    )
    persisted = _read_json(home / "auth.json")
    persisted_alias = next(
        row for row in persisted["credential_pool"][PROVIDER] if row["id"] == "legacy-alias"
    )
    persisted_independent = next(
        row for row in persisted["credential_pool"][PROVIDER] if row["id"] == "independent"
    )
    assert persisted_alias.get("codex_lineage") == "singleton_alias"
    assert persisted_independent.get("codex_lineage") != "singleton_alias"
    again = PooledCredential.from_dict(PROVIDER, persisted_alias)
    assert again.extra.get("codex_lineage") == "singleton_alias"


def test_persisted_alias_adopts_rotated_singleton_independent_does_not(
    tmp_path, monkeypatch
):
    """E. After reload, a genuine alias adopts singleton rotation; independent does not."""
    home = _isolate_auth_home(tmp_path, monkeypatch)
    _write_json(
        home / "auth.json",
        _auth_store(
            singleton_access="old-at",
            singleton_refresh="old-rt",
            pool=[
                _raw_pool_row(
                    entry_id="legacy-alias",
                    source="manual:device_code",
                    access_token="old-at",
                    refresh_token="old-rt",
                ),
                _raw_pool_row(
                    entry_id="independent",
                    source="manual:device_code",
                    access_token="B",
                    refresh_token="RB",
                ),
            ],
        ),
    )
    _save_codex_tokens(
        {"access_token": "fresh-at", "refresh_token": "fresh-rt"},
        last_refresh="2026-08-25T00:00:00Z",
    )

    store = _read_json(home / "auth.json")
    store["providers"][PROVIDER]["tokens"] = {
        "access_token": "A2",
        "refresh_token": "RA2",
    }
    _write_json(home / "auth.json", store)

    alias = _reload_entry(_entry_from_store(store, "legacy-alias"))
    independent = _reload_entry(_entry_from_store(store, "independent"))
    pool = CredentialPool(PROVIDER, [alias, independent])

    synced_alias = pool._sync_codex_entry_from_auth_store(alias)
    synced_independent = pool._sync_codex_entry_from_auth_store(independent)

    assert synced_alias.access_token == "A2"
    assert synced_alias.refresh_token == "RA2"
    assert synced_independent.access_token == "B"
    assert synced_independent.refresh_token == "RB"


def test_device_code_singleton_still_adopts_auth_store(tmp_path, monkeypatch):
    """F. Ordinary device_code singleton synchronization is unchanged."""
    home = _isolate_auth_home(tmp_path, monkeypatch)
    _write_json(
        home / "auth.json",
        _auth_store(
            singleton_access="A2",
            singleton_refresh="RA2",
            pool=[
                _raw_pool_row(
                    entry_id="seeded",
                    source="device_code",
                    access_token="A",
                    refresh_token="RA",
                ),
            ],
        ),
    )
    entry = _pool_entry(
        entry_id="seeded",
        source="device_code",
        access_token="A",
        refresh_token="RA",
    )
    pool = CredentialPool(PROVIDER, [entry])
    synced = pool._sync_codex_entry_from_auth_store(entry)
    assert synced.access_token == "A2"
    assert synced.refresh_token == "RA2"


def test_profile_independent_manual_does_not_shadow_global_root_singleton(
    tmp_path, monkeypatch
):
    """G. Profile-only independent B/RB must not mutate or shadow root A/RA."""
    profile_path = tmp_path / "profiles" / "work" / "auth.json"
    root_path = tmp_path / "root" / "auth.json"
    monkeypatch.setenv("HERMES_HOME", str(profile_path.parent))
    monkeypatch.setenv("HOME", str(tmp_path / "not-the-real-home"))
    monkeypatch.setattr(A, "_auth_file_path", lambda: profile_path)
    monkeypatch.setattr(A, "_global_auth_file_path", lambda: root_path)
    monkeypatch.setattr(CP, "_global_auth_file_path", lambda: root_path)
    monkeypatch.setattr(CP, "_same_path", lambda a, b: a == b)

    _write_json(
        root_path,
        {
            "version": 1,
            "providers": {
                PROVIDER: {
                    "tokens": {"access_token": "A", "refresh_token": "RA"},
                    "auth_mode": "chatgpt",
                }
            },
        },
    )
    _write_json(
        profile_path,
        {
            "version": 1,
            "credential_pool": {
                PROVIDER: [
                    _raw_pool_row(
                        entry_id="manual-b",
                        source="manual:device_code",
                        access_token="B",
                        refresh_token="RB",
                    )
                ]
            },
        },
    )

    presented: list[tuple[str, str]] = []

    def fake_refresh(access_token, refresh_token, **kwargs):
        presented.append((access_token, refresh_token))
        return {
            "access_token": "B2",
            "refresh_token": "RB2",
            "last_refresh": "2026-08-25T00:00:00Z",
        }

    monkeypatch.setattr(A, "refresh_codex_oauth_pure", fake_refresh)

    entry = _pool_entry(
        entry_id="manual-b",
        source="manual:device_code",
        access_token="B",
        refresh_token="RB",
    )
    pool = CredentialPool(PROVIDER, [entry])
    synced = pool._sync_codex_entry_from_auth_store(entry)
    assert synced.access_token == "B"
    assert synced.refresh_token == "RB"

    refreshed = pool._refresh_entry(entry, force=True)
    assert presented == [("B", "RB")]
    assert refreshed is not None
    assert refreshed.access_token == "B2"
    assert refreshed.refresh_token == "RB2"

    profile = _read_json(profile_path)
    root = _read_json(root_path)
    assert "openai-codex" not in profile.get("providers", {})
    assert root["providers"][PROVIDER]["tokens"]["access_token"] == "A"
    assert root["providers"][PROVIDER]["tokens"]["refresh_token"] == "RA"
    persisted = profile["credential_pool"][PROVIDER][0]
    assert persisted["access_token"] == "B2"
    assert persisted["refresh_token"] == "RB2"


def test_ambiguous_divergent_manual_defaults_independent(tmp_path, monkeypatch):
    """H. No heuristic alias inference from inequality, labels, or timestamps."""
    home = _isolate_auth_home(tmp_path, monkeypatch)
    _write_json(
        home / "auth.json",
        _auth_store(
            singleton_access="A",
            singleton_refresh="RA",
            pool=[
                _raw_pool_row(
                    entry_id="ambiguous",
                    source="manual:device_code",
                    access_token="B",
                    refresh_token="RB",
                    label="openai-codex",
                    last_refresh="2026-01-01T00:00:00Z",
                ),
            ],
        ),
    )
    entry = _pool_entry(
        entry_id="ambiguous",
        source="manual:device_code",
        access_token="B",
        refresh_token="RB",
        label="openai-codex",
        extra={"last_refresh": "2026-01-01T00:00:00Z"},
    )
    pool = CredentialPool(PROVIDER, [entry])
    synced = pool._sync_codex_entry_from_auth_store(entry)
    assert synced.access_token == "B"
    assert synced.refresh_token == "RB"
    assert synced.extra.get("codex_lineage") != "singleton_alias"


def test_already_diverged_manual_is_not_classified_as_alias_on_reauth(
    tmp_path, monkeypatch
):
    """I. Bootstrap still requires previous-access-token equality; no silent aliasing."""
    home = _isolate_auth_home(tmp_path, monkeypatch)
    _write_json(
        home / "auth.json",
        _auth_store(
            singleton_access="A",
            singleton_refresh="RA",
            pool=[
                _raw_pool_row(
                    entry_id="diverged",
                    source="manual:device_code",
                    access_token="B",
                    refresh_token="RB",
                ),
            ],
        ),
    )
    _save_codex_tokens(
        {"access_token": "A2", "refresh_token": "RA2"},
        last_refresh="2026-08-25T00:00:00Z",
    )
    store = _read_json(home / "auth.json")
    diverged = next(row for row in store["credential_pool"][PROVIDER] if row["id"] == "diverged")
    assert diverged["access_token"] == "B"
    assert diverged["refresh_token"] == "RB"
    assert diverged.get("codex_lineage") != "singleton_alias"


def test_marked_alias_refresh_writes_through_profile_owned_singleton(
    tmp_path, monkeypatch
):
    """J (profile-owned). Alias refresh must rotate the owning profile singleton."""
    home = _isolate_auth_home(tmp_path, monkeypatch)
    _write_json(
        home / "auth.json",
        _auth_store(
            singleton_access="old-at",
            singleton_refresh="old-rt",
            pool=[
                _raw_pool_row(
                    entry_id="legacy-alias",
                    source="manual:device_code",
                    access_token="old-at",
                    refresh_token="old-rt",
                ),
            ],
        ),
    )
    _save_codex_tokens(
        {"access_token": "A", "refresh_token": "RA"},
        last_refresh="2026-08-25T00:00:00Z",
    )

    presented: list[tuple[str, str]] = []

    def fake_refresh(access_token, refresh_token, **kwargs):
        presented.append((access_token, refresh_token))
        return {
            "access_token": "A2",
            "refresh_token": "RA2",
            "last_refresh": "2026-08-25T01:00:00Z",
        }

    monkeypatch.setattr(A, "refresh_codex_oauth_pure", fake_refresh)

    store = _read_json(home / "auth.json")
    alias = _reload_entry(_entry_from_store(store, "legacy-alias"))
    assert alias.extra.get("codex_lineage") == "singleton_alias"
    pool = CredentialPool(PROVIDER, [alias])
    refreshed = pool._refresh_entry(alias, force=True)

    assert presented == [("A", "RA")]
    assert refreshed is not None
    assert refreshed.access_token == "A2"
    assert refreshed.refresh_token == "RA2"

    after = _read_json(home / "auth.json")
    tokens = after["providers"][PROVIDER]["tokens"]
    assert tokens["access_token"] == "A2"
    assert tokens["refresh_token"] == "RA2"
    assert tokens["refresh_token"] != "RA"
    persisted = next(
        row for row in after["credential_pool"][PROVIDER] if row["id"] == "legacy-alias"
    )
    assert persisted["access_token"] == "A2"
    assert persisted["refresh_token"] == "RA2"


def test_marked_alias_refresh_writes_through_global_root_owned_singleton(
    tmp_path, monkeypatch
):
    """J (global-root). Alias refresh must rotate root and not create a profile shadow."""
    profile_path = tmp_path / "profiles" / "work" / "auth.json"
    root_path = tmp_path / "root" / "auth.json"
    monkeypatch.setenv("HERMES_HOME", str(profile_path.parent))
    monkeypatch.setenv("HOME", str(tmp_path / "not-the-real-home"))
    monkeypatch.setattr(A, "_auth_file_path", lambda: profile_path)
    monkeypatch.setattr(A, "_global_auth_file_path", lambda: root_path)
    monkeypatch.setattr(CP, "_global_auth_file_path", lambda: root_path)
    monkeypatch.setattr(CP, "_same_path", lambda a, b: a == b)

    _write_json(
        root_path,
        {
            "version": 1,
            "providers": {
                PROVIDER: {
                    "tokens": {"access_token": "old-at", "refresh_token": "old-rt"},
                    "auth_mode": "chatgpt",
                }
            },
        },
    )
    _write_json(
        profile_path,
        {
            "version": 1,
            "providers": {
                PROVIDER: {
                    "tokens": {"access_token": "old-at", "refresh_token": "old-rt"},
                    "auth_mode": "chatgpt",
                }
            },
            "credential_pool": {
                PROVIDER: [
                    _raw_pool_row(
                        entry_id="legacy-alias",
                        source="manual:device_code",
                        access_token="old-at",
                        refresh_token="old-rt",
                    )
                ]
            },
        },
    )

    # Establish durable alias lineage via the real write-side equality rule.
    _save_codex_tokens(
        {"access_token": "A", "refresh_token": "RA"},
        last_refresh="2026-08-25T00:00:00Z",
    )
    # Profile now owns a providers block from _save_codex_tokens. Rebuild the
    # global-root-owned topology: keep the stamped pool row, drop the profile
    # singleton, put the current pair on root only.
    profile = _read_json(profile_path)
    alias_row = next(
        row for row in profile["credential_pool"][PROVIDER] if row["id"] == "legacy-alias"
    )
    assert alias_row.get("codex_lineage") == "singleton_alias"
    _write_json(
        profile_path,
        {
            "version": 1,
            "credential_pool": {PROVIDER: [alias_row]},
        },
    )
    _write_json(
        root_path,
        {
            "version": 1,
            "providers": {
                PROVIDER: {
                    "tokens": {"access_token": "A", "refresh_token": "RA"},
                    "auth_mode": "chatgpt",
                }
            },
        },
    )

    presented: list[tuple[str, str]] = []

    def fake_refresh(access_token, refresh_token, **kwargs):
        presented.append((access_token, refresh_token))
        return {
            "access_token": "A2",
            "refresh_token": "RA2",
            "last_refresh": "2026-08-25T01:00:00Z",
        }

    monkeypatch.setattr(A, "refresh_codex_oauth_pure", fake_refresh)

    alias = PooledCredential.from_dict(PROVIDER, alias_row)
    pool = CredentialPool(PROVIDER, [alias])
    refreshed = pool._refresh_entry(alias, force=True)

    assert presented == [("A", "RA")]
    assert refreshed is not None
    assert refreshed.access_token == "A2"
    assert refreshed.refresh_token == "RA2"

    profile_after = _read_json(profile_path)
    root_after = _read_json(root_path)
    assert "openai-codex" not in profile_after.get("providers", {})
    root_tokens = root_after["providers"][PROVIDER]["tokens"]
    assert root_tokens["access_token"] == "A2"
    assert root_tokens["refresh_token"] == "RA2"
    assert root_tokens["refresh_token"] != "RA"
