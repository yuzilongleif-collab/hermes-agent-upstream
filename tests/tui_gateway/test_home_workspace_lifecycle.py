"""Workspace provenance across branch/compression and the compute-host carrier."""
from types import SimpleNamespace

import pytest

from tests.tui_gateway.test_home_workspace_provenance import KEY
from tui_gateway import server

pytest_plugins = ["tests.tui_gateway.test_home_workspace_provenance"]


@pytest.mark.parametrize("chosen", [False, True], ids=["home", "project"])
def test_branch_keeps_workspace_across_child_cold_resume(home_runtime, chosen):
    env = home_runtime
    cwd = str(env.fallback) if chosen else None
    env.db.create_session(KEY, source="desktop", cwd=cwd)
    env.db.append_message(KEY, "user", "synthetic branch input")
    sid, live = env.resume(source="tui", omit_messages=True)
    live["agent"] = SimpleNamespace(close=lambda: None)
    live["agent_ready"].set()
    response = server.handle_request({"id": "branch", "method": "session.branch",
                                      "params": {"session_id": sid, "name": "synthetic branch"}})
    assert "error" not in response, response
    child_key = response["result"]["stored_session_id"]
    child_sid = response["result"]["session_id"]
    child = server._sessions[child_sid]
    assert server._ensure_session_db_row(child)
    assert env.db.get_session(child_key)["cwd"] == cwd
    server._sessions.pop(child_sid)
    resumed = server.handle_request({"id": "child", "method": "session.resume",
                                     "params": {"session_id": child_key, "omit_messages": True}})
    assert "error" not in resumed, resumed
    child = server._sessions[resumed["result"]["session_id"]]
    assert server._ensure_session_db_row(child)
    assert env.db.get_session(child_key)["cwd"] == cwd


@pytest.mark.parametrize("chosen", [False, True], ids=["home", "project"])
def test_compression_child_keeps_workspace_on_resume(home_runtime, chosen):
    env = home_runtime
    cwd = str(env.fallback) if chosen else None
    env.db.create_session(KEY, source="desktop", cwd=cwd)
    child_key = "20261029_120001_ba12dc"
    env.db.publish_compression_child(
        parent_session_id=KEY, child_session_id=child_key, source="desktop",
        messages=[{"role": "user", "content": "synthetic compressed input"}],
        require_compression_lease=False,
    )
    _, live = env.resume(omit_messages=True)
    assert live["session_key"] == child_key
    assert server._ensure_session_db_row(live)
    assert env.db.get_session(child_key)["cwd"] == cwd


def test_compute_host_carrier_keeps_unbound_workspace(home_runtime):
    import io

    from tui_gateway.compute_host import ComputeHost

    env = home_runtime
    env.db.create_session(KEY, source="desktop")
    sid, live = env.resume(omit_messages=True)
    frame = server._compute_host_turn_frame("turn", sid, live, "synthetic input")
    child = dict(live, workspace_unbound=False)
    server._sessions[sid] = child
    host = ComputeHost(stdout=io.StringIO(), heartbeat_secs=0)
    try:
        restored = host._ensure_server_session(server, frame)
        assert server._persisted_session_cwd(restored) is None
        assert restored["source"] == live["source"]
    finally:
        host.close()
