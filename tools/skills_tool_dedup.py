"""skill_view repeat-view dedup registry: per-task cache of (resolved skill name, file_path) ->
digest of the skill_view result served. A repeat view whose result is byte-identical returns a short
stub — the earlier tool result already carries the content verbatim. Content digests need no file
timestamps, so a same-size rewrite in one coarse mtime tick (or on a host whose clock disagrees with
the filesystem's) is never mistaken for "unchanged". A transcript rewrite releases entries unless
the same result is still present after the rewrite.
"""

import hashlib
import json
import threading

_skill_view_tracker: dict[str, dict[tuple, str]] = {}
_skill_view_tracker_lock = threading.Lock()
_SKILL_VIEW_DEDUP_CAP = 200

_SKILL_VIEW_DEDUP_MESSAGE = (
    "Skill content unchanged since it was loaded earlier in this "
    "conversation — refer to the earlier skill_view result; it is still "
    "current and complete.")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def dedup_skill_view(task_id, name, file_path, payload: dict, result: str) -> str | None:
    """The "unchanged" stub when this task was already served exactly ``result`` for this skill
    file; otherwise record ``result``'s digest and return None (serve the full result)."""
    # Never dedup setup-needed views: readiness depends on config/env state that
    # changes without the file changing; the model must see the refreshed status.
    if (not task_id or payload.get("setup_needed")
            or payload.get("readiness_status") == "setup_needed"):
        return None
    resolved = str(payload.get("name") or name)
    key = (resolved, file_path or "")
    digest = _digest(result)
    with _skill_view_tracker_lock:
        cache = _skill_view_tracker.setdefault(str(task_id), {})
        if cache.get(key) == digest:
            return json.dumps({
                "success": True, "status": "unchanged", "name": resolved,
                "file": file_path or "SKILL.md", "dedup": True, "content_returned": False,
                "message": _SKILL_VIEW_DEDUP_MESSAGE}, ensure_ascii=False)
        cache.pop(key, None)  # re-insert at the end: FIFO eviction keeps the recently served
        cache[key] = digest
        while len(cache) > _SKILL_VIEW_DEDUP_CAP:  # FIFO eviction
            del cache[next(iter(cache))]
    return None


def _retained_skill_digests(messages: list) -> set[str]:
    """Exact results of skill_view calls still present after the rewrite, not stored archives.

    An older revision of the same linked file does not witness the latest result. And a
    tool row before the newest native checkpoint may remain locally but is off the wire.
    """
    from agent.context_compressor import _newest_checkpoint_carrier
    from agent.context_compressor_tail import skill_view_names_by_call_id

    rows = messages[_newest_checkpoint_carrier(messages, "codex_reasoning_items") + 1:]
    calls = skill_view_names_by_call_id(rows)
    # The handler serializes a JSON object: raw newlines in its string values are escaped.
    # append_toolguard_guidance appends outside that object. A forged delimiter or a
    # truncated JSON prefix cannot witness the original result: the whole prefix must hash-match.
    return {_digest(row["content"].partition("\n\n[Tool loop warning: ")[0]) for row in rows
            if row.get("role") == "tool" and row.get("tool_call_id") and row["tool_call_id"] in calls
            and isinstance(row.get("content"), str)}


def reset_skill_view_dedup(task_id: str | None = None, *, surviving_messages: list | None = None) -> None:
    """Release stubs on loss; keep only exact already-served results in the rewritten transcript.
    No transcript (including native compaction) always releases the task, never rehydrates it."""
    with _skill_view_tracker_lock:
        if task_id is None:
            _skill_view_tracker.clear()
            return
        if not surviving_messages or not _skill_view_tracker.get(str(task_id)):
            _skill_view_tracker.pop(str(task_id), None)
            return
    # Never scan a transcript under the global registry lock; fail closed on malformed plugin rows.
    retained = set()
    try:
        retained = _retained_skill_digests(surviving_messages)
    finally:
        with _skill_view_tracker_lock:
            cache = _skill_view_tracker.pop(str(task_id), {})
            if kept := {key: entry for key, entry in cache.items() if entry in retained}:
                _skill_view_tracker[str(task_id)] = kept
