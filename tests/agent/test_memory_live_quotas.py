"""Configured live built-in quotas are independent of the frozen prompt snapshot.

Exercise the real constructor and inline executor used by Desktop/TUI as well as
fresh on-disk stores; no model calls, fake validators, or external providers.
"""
from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest
import hermes_yaml as yaml

from agent.agent_init import _init_memory
from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext
from hermes_cli.config import load_config_readonly
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tools import memory_tool as mt


@contextmanager
def _home(home):
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def _config(home, **values):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"memory": {"write_approval": False, **values}}), encoding="utf-8"
    )


def _agent():
    agent = SimpleNamespace(enabled_toolsets=["memory"], disabled_toolsets=[], tools=[])
    # Same built-in constructor as AIAgent, without allocating a model client.
    _init_memory(agent, load_config_readonly(), skip_memory=True, platform="desktop")
    assert agent._memory_store is not None
    return agent


def _call(agent, target, **args):
    return json.loads(INLINE_TOOL_EXECUTORS["memory"](
        agent, {"target": target, **args}, InlineToolContext("synthetic-quota-test")
    ))


@pytest.mark.parametrize("target", ["memory", "user"])
@pytest.mark.parametrize("operation", ["add", "patch", "batch"])
def test_live_inline_adopts_raised_quota_without_changing_snapshot(tmp_path, target, operation):
    home = tmp_path / "profile"
    _config(home, memory_char_limit=32, user_char_limit=32)
    with _home(home):
        # Seed through the native tool so the frozen block is nonempty and meaningful.
        seed = mt.load_on_disk_store()
        assert json.loads(mt.memory_tool(action="add", target=target, content="Synthetic seed", store=seed))["success"]
        agent = _agent()
        store = agent._memory_store
        snapshot = store.format_for_system_prompt(target).encode()
        payload = "Synthetic expanded preference " + "x" * 60
        args = {"action": operation, "content": payload}
        if operation == "patch":
            args["old_text"] = "Synthetic seed"
        elif operation == "batch":
            args = {"operations": [{"action": "add", "content": payload}]}
        assert not _call(agent, target, **args)["success"]
        _config(home, memory_char_limit=256, user_char_limit=256)
        result = _call(agent, target, **args)
        assert result["success"], result
        assert "/256 chars" in result["usage"]
        assert agent._memory_store is store
        assert store.format_for_system_prompt(target).encode() == snapshot
        fresh = mt.load_on_disk_store()
        assert payload in fresh._entries_for(target)
        assert fresh._entries_for(target) == store._entries_for(target)


@pytest.mark.parametrize("target", ["memory", "user"])
def test_lower_quota_preserves_entries_and_permits_legal_reduction(tmp_path, target):
    home = tmp_path / "profile"
    _config(home, memory_char_limit=256, user_char_limit=256)
    with _home(home):
        agent = _agent()
        for content in ("Synthetic alpha", "Synthetic beta"):
            assert _call(agent, target, action="add", content=content)["success"]
        path = agent._memory_store._path_for(target)
        original = path.read_bytes()
        _config(home, memory_char_limit=24, user_char_limit=24)
        result = _call(agent, target, action="add", content="Synthetic gamma")
        assert not result["success"], result
        assert path.read_bytes() == original
        # Reduction must fit the final cap, as in the existing store contract.
        result = _call(agent, target, operations=[
            {"action": "patch", "old_text": "Synthetic alpha", "content": "Alpha"},
            {"action": "patch", "old_text": "Synthetic beta", "content": "Beta"},
        ])
        assert result["success"], result
        assert "/24 chars" in result["usage"]
        assert _call(agent, target, action="remove", old_text="Beta")["success"]
        assert mt.load_on_disk_store()._entries_for(target) == ["Alpha"]


def test_profiles_custom_limits_and_fresh_store_remain_independent(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _config(a, memory_char_limit=32, user_char_limit=32)
    _config(b, memory_char_limit=32, user_char_limit=32)
    with _home(a):
        agent_a = _agent()
        fresh_a = mt.load_on_disk_store()
    with _home(b):
        agent_b = _agent()
        custom = mt.MemoryStore(16, 16)
        custom.load_from_disk()
    _config(a, memory_char_limit=256, user_char_limit=256)
    payload = "Synthetic profile-local preference " + "x" * 40
    with _home(b):
        assert not _call(agent_b, "user", action="add", content=payload)["success"]
        assert not json.loads(mt.memory_tool(store=custom, target="memory", action="add", content=payload))["success"]
        assert (custom.memory_char_limit, custom.user_char_limit) == (16, 16)
    with _home(a):
        assert _call(agent_a, "user", action="add", content=payload)["success"]
        assert json.loads(mt.memory_tool(store=fresh_a, target="memory", action="add", content=payload))["success"]
        assert mt.load_on_disk_store().user_entries == [payload]
    with _home(b):
        assert mt.load_on_disk_store().user_entries == []


@pytest.mark.parametrize("action", ["patch", "remove"])
def test_lower_quota_does_not_misclassify_a_previously_valid_entry_as_drift(tmp_path, action):
    home = tmp_path / "profile"
    _config(home, memory_char_limit=256, user_char_limit=256)
    with _home(home):
        agent = _agent()
        content = "Synthetic long preference " + "x" * 60
        assert _call(agent, "user", action="add", content=content)["success"]
        _config(home, memory_char_limit=24, user_char_limit=24)
        args = {"action": action, "old_text": content}
        if action == "patch":
            args["content"] = "Short fact"
        result = _call(agent, "user", **args)
        assert result["success"], result
        assert "/24 chars" in result["usage"]
        assert mt.load_on_disk_store().user_entries == (["Short fact"] if action == "patch" else [])


def test_unset_limits_restore_defaults_on_the_same_live_store(tmp_path):
    home = tmp_path / "profile"
    _config(home, memory_char_limit=5000, user_char_limit=5000)
    with _home(home):
        agent = _agent()
        _config(home)
        result = _call(agent, "memory", action="add", content="x" * 3000)
        assert not result["success"], result
        assert agent._memory_store.memory_char_limit == mt.MemoryStore().memory_char_limit
        assert agent._memory_store.user_char_limit == mt.MemoryStore().user_char_limit


def test_configured_limit_refresh_keeps_external_drift_guard(tmp_path):
    home = tmp_path / "profile"
    _config(home, memory_char_limit=256, user_char_limit=256)
    with _home(home):
        agent = _agent()
        path = agent._memory_store._path_for("memory")
        raw = "Synthetic external free-form text " + "x" * 300
        path.write_text(raw, encoding="utf-8")
        _config(home, memory_char_limit=24, user_char_limit=24)
        result = _call(agent, "memory", action="remove", old_text=raw)
        assert not result["success"], result
        assert "drift_backup" in result
        assert path.read_text(encoding="utf-8") == raw


@pytest.mark.parametrize("bad_value", [None, "not-an-integer"])
def test_invalid_live_limit_does_not_destroy_last_usable_budget(tmp_path, bad_value):
    home = tmp_path / "profile"
    _config(home, memory_char_limit=128, user_char_limit=128)
    with _home(home):
        agent = _agent()
        _config(home, memory_char_limit=bad_value, user_char_limit=bad_value)
        assert _call(agent, "memory", action="add", content="Synthetic fact")["success"]
        assert agent._memory_store.memory_char_limit == 128


def test_pending_replay_uses_current_quota_without_reloading_snapshot(tmp_path):
    home = tmp_path / "profile"
    _config(home, memory_char_limit=32, user_char_limit=32)
    with _home(home):
        agent = _agent()
        store = agent._memory_store
        snapshot = dict(store._system_prompt_snapshot)
        _config(home, memory_char_limit=256, user_char_limit=256)
        payload = {"action": "add", "target": "user", "content": "Synthetic pending preference " + "x" * 40}
        result = mt.apply_memory_pending(payload, store)
        assert result["success"], result
        assert store._system_prompt_snapshot == snapshot
        assert mt.load_on_disk_store().user_entries == [payload["content"]]
