"""A Home row must not acquire the reconnecting transport's execution fallback.

Complements #97881: durable desktop/NULL provenance survives a TUI-default
resume and live reuse. Model, transport and schedulers are doubles; resume,
workdir policy and SQLite writes use real candidate imports.
"""
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tui_gateway import server

KEY = "20261029_120000_ab12cd"


@pytest.fixture
def home_runtime(monkeypatch, tmp_path):
    fallback = tmp_path / "execution-fallback"
    fallback.mkdir()
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    monkeypatch.delenv("HERMES_DESKTOP_TERMINAL", raising=False)
    monkeypatch.setenv("TERMINAL_CWD", str(fallback))
    for name, value in {
        "_get_db": lambda: db,
        "_load_cfg": dict,
        "_resolve_model": lambda: "test-model",
        "_enable_gateway_prompts": lambda: None,
        "_schedule_agent_build": lambda *a, **k: None,
        "_schedule_resume_hydration": lambda *a, **k: None,
        "_schedule_session_cap_enforcement": lambda: None,
        "_maybe_schedule_auto_continue": lambda *a: None,
        "_child_run_active": lambda *a: False,
        "_make_agent_in_context": lambda *a, **k: SimpleNamespace(close=lambda: None),
        "_wire_session_agent": lambda *a: None,
        "_start_session_services": lambda *a: None,
        "_schedule_mcp_late_refresh": lambda *a: None,
        "_session_info": lambda *a: {},
        "_emit": lambda *a: None,
        "_persist_session_git_meta": lambda *a, **k: None,
    }.items():
        monkeypatch.setattr(server, name, value)
    monkeypatch.setattr(server.git_probe, "repo_root", lambda _cwd: None)
    monkeypatch.setattr(server.git_probe, "common_repo_root", lambda _cwd: None)
    monkeypatch.setattr(server.git_probe, "branch", lambda _cwd: None)
    known = set(server._sessions)

    def resume(**params):
        response = server.handle_request({
            "id": "resume", "method": "session.resume", "params": {"session_id": KEY, **params},
        })
        assert "error" not in response, response
        sid = response["result"]["session_id"]
        return sid, server._sessions[sid]

    yield SimpleNamespace(db=db, fallback=fallback, resume=resume)
    for sid in set(server._sessions) - known:
        server._sessions.pop(sid, None)
    db.close()


@pytest.mark.parametrize("flags", [{}, {"lazy": True}, {"defer_history": True}, {"eager_build": True}],
                         ids=["cold", "lazy", "deferred", "eager"])
@pytest.mark.parametrize("source", [None, "desktop", "tui"], ids=["omitted", "desktop", "tui"])
def test_home_survives_resume_reuse_hydration_and_upsert(home_runtime, flags, source):
    env = home_runtime
    env.db.create_session(KEY, source="desktop")
    params = {"omit_messages": True, **flags}
    if source is not None:
        params["source"] = source
    sid, live = env.resume(**params)
    original_source, original_agent = live["source"], live["agent"]
    assert env.db.get_session(KEY)["cwd"] is None
    sid2, reused = env.resume(source="desktop", omit_messages=True, defer_history=True)
    assert sid2 == sid and reused is live
    assert (reused["source"], reused["agent"]) == (original_source, original_agent)
    assert server._ensure_session_db_row(reused) is True
    assert env.db.get_session(KEY)["cwd"] is None
    server._hydrate_session_cwd(sid, KEY, env.db, None)
    assert server._ensure_session_db_row(reused) is True
    row = env.db.get_session(KEY)
    assert (row["source"], row["cwd"]) == ("desktop", None)
    assert live["cwd"] == str(env.fallback)  # execution still has a usable directory
    from tui_gateway.project_tree import build_tree

    tree = build_tree([], [row], [], resolve=lambda _cwd: None,
                      is_junk_cwd=lambda _cwd: False, is_junk_root=lambda _root: False,
                      exists=lambda _cwd: True, hydrate=True)
    assert tree["projects"][0]["id"] == "__no_project__"


@pytest.mark.parametrize("source", ["desktop", "tui"])
def test_home_display_healing_cannot_persist_execution_parent(home_runtime, source):
    env = home_runtime
    env.db.create_session(KEY, source="desktop")
    _, live = env.resume(source=source, omit_messages=True)
    live["cwd"] = str(env.fallback / "deleted-child")
    assert server._display_session_cwd(live) == str(env.fallback)
    assert env.db.get_session(KEY)["cwd"] is None


@pytest.mark.parametrize("source", ["desktop", "tui"])
def test_existing_workspace_and_explicit_cwd_set_survive(home_runtime, tmp_path, source):
    env = home_runtime
    workspace = tmp_path / "chosen"
    workspace.mkdir()
    env.db.create_session(KEY, source="desktop", cwd=str(workspace))
    sid, live = env.resume(source=source, omit_messages=True)
    server._hydrate_session_cwd(sid, KEY, env.db, None)
    assert server._ensure_session_db_row(live)
    assert env.db.get_session(KEY)["cwd"] == str(workspace)
    response = server.handle_request({"id": "cwd", "method": "session.cwd.set",
                                      "params": {"session_id": sid, "cwd": str(env.fallback)}})
    assert "error" not in response, response
    assert server._ensure_session_db_row(live)
    assert env.db.get_session(KEY)["cwd"] == str(env.fallback)


def test_new_tui_keeps_launch_directory_intent(home_runtime):
    env = home_runtime
    response = server.handle_request({"id": "new", "method": "session.create", "params": {"source": "tui"}})
    assert "error" not in response, response
    live = server._sessions[response["result"]["session_id"]]
    assert server._ensure_session_db_row(live)
    assert env.db.get_session(live["session_key"])["cwd"] == str(env.fallback)


def test_home_can_explicitly_choose_the_same_fallback(home_runtime):
    env = home_runtime
    env.db.create_session(KEY, source="desktop")
    sid, live = env.resume(omit_messages=True)
    response = server.handle_request({"id": "cwd", "method": "session.cwd.set",
                                      "params": {"session_id": sid, "cwd": str(env.fallback)}})
    assert "error" not in response, response
    assert server._ensure_session_db_row(live)
    assert env.db.get_session(KEY)["cwd"] == str(env.fallback)


def test_explicit_workspace_wins_over_null_row_hydration(home_runtime):
    env = home_runtime
    env.db.create_session(KEY, source="desktop")
    server._init_session("chosen-before-hydration", KEY, SimpleNamespace(), [], cwd=str(env.fallback),
                         source="desktop", explicit_cwd=True, session_db=env.db)
    assert env.db.get_session(KEY)["cwd"] == str(env.fallback)
