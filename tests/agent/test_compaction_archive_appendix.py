"""The archive inventory is durable, not part of the model's working summary (#156164)."""
import json
from types import SimpleNamespace

from agent.context_compressor import ContextCompressor
from agent.model_metadata import estimate_messages_tokens_rough
from agent.turn_context import build_api_messages
from agent.usage_anchor import anchored_context_tokens, capture_usage_anchor
from persistence.conversation.messages import NewMessage
from persistence.conversation.sessions import NewSession
from persistence_assembly import open_persistence, profile_scope
from run_agent import AIAgent
from session_rules.media import archive_refs_in_text
from session_rules.turns import _SUMMARY_END_MARKER
from tests.fixtures.storage_read import conversation
from tools.file_tools import read_file_tool
from tools.tool_result_storage import archive_list_for_summary, pruned_archive_refs, referenced_archive_refs


def _wire(agent, row):
    wire, _ = build_api_messages(agent, [row], current_turn_user_idx=0, ext_prefetch_cache=None,
                                 plugin_user_context=None, moa_config=None, active_system_prompt="")
    return wire[-1]["content"]


def test_cold_handoff_pages_exact_archive_list_and_next_compaction_preserves_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    refs = {f"cache/pruned/result-{i:04}.txt" for i in range(4000)}
    body = ContextCompressor._with_summary_prefix("Keep the semantic result.") + archive_list_for_summary(refs, [])
    ask = "Do not shorten my next message: cache/pruned/user-note.txt"
    text = body + "\n\n" + _SUMMARY_END_MARKER + "\n\n" + ask
    with open_persistence(profile_scope(home=tmp_path)) as store:
        store.sessions.create(NewSession("example", "cli"))
        store.messages.append_batch("example", [NewMessage("user", text)])
    agent = AIAgent(api_key="test", model="test-model", provider="custom", base_url="http://localhost:1234/v1",
                    quiet_mode=True, skip_context_files=True, skip_memory=True)
    agent._current_turn_timestamp = 1.0
    with open_persistence(profile_scope(home=tmp_path)) as store:
        row, = conversation(store, "example")
    assert row["content"] == text
    wire = _wire(agent, row)
    assert "result-0000.txt" not in wire and wire.endswith(ask)
    assert estimate_messages_tokens_rough([row]) == estimate_messages_tokens_rough([{**row, "content": wire}])
    assert "Keep the semantic result." in wire
    assert refs <= referenced_archive_refs(tmp_path)
    index_ref, = (r for r in archive_refs_in_text(wire) if r.startswith("cache/spillover/"))
    index = tmp_path / index_ref
    assert index_ref in referenced_archive_refs(tmp_path)
    assert index.read_text(encoding="utf-8").splitlines() == [(tmp_path / ref).as_posix() for ref in sorted(refs)]
    page = json.loads(read_file_tool(str(index), offset=4000, limit=1))
    assert "result-3999.txt" in page["content"] and "result-3998.txt" not in page["content"]
    comp = agent.context_compressor
    comp._scan_window_handoffs([row], 0, 1, [row])
    assert "result-0000.txt" not in comp._previous_summary
    assert "Keep the semantic result." in comp._previous_summary
    newer = {"role": "tool", "content": "cache/pruned/new.txt"}
    scan = SimpleNamespace(tail_start=2, summary_indices={0})
    out = comp._assemble_with_archive_list([row, newer], 1, 2, scan, comp._with_summary_prefix("Updated."))
    assert refs | {"cache/pruned/new.txt"} <= pruned_archive_refs(out)
    assert "result-0000.txt" not in _wire(agent, out[-1])
    anchor = capture_usage_anchor(500, 20, [row])
    assert anchored_context_tokens([row], anchor) == 520
    index.write_text("wrong index\n")
    assert _wire(agent, row) == text
    assert anchored_context_tokens([row], anchor) is None


def test_index_write_failure_leaves_every_path_inline(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_ENV", "local")
    # A non-directory exercises the real writer's failure path without relying on chmod.
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    (cache / "spillover").write_text("not a directory")
    refs = {f"cache/pruned/result-{i}.txt" for i in range(200)}
    footer = archive_list_for_summary(refs, [])
    assert archive_refs_in_text(footer) == refs
    assert "cache/spillover/" not in footer
