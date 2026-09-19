"""--allow-partial recovery must not certify an empty trigram projection.

``_cleanup_partial_orphans`` reconstructs the parent ``sessions`` rows *after*
``messages`` has been copied. The trigram sync trigger is gated on
``EXISTS (SELECT 1 FROM sessions WHERE id = new.session_id AND ...)``, so every
message copied while its owner was still missing is silently skipped. Once the
owners are reconstructed, ``messages_fts_trigram_src`` reports the full cohort
while the inverted index holds nothing.

Row counts, FTS integrity-check and base-index MATCH probes all pass on that
shape, so the recovery reports ``verified=True`` and the operator never learns
that CJK/substring search is gone.

Written against APIs present before and after the partial-projection change so
it can be run unchanged on a pristine base.
"""

import sqlite3

import pytest

from hermes_cli.session_recovery import recover_session_database
from hermes_state import SessionDB

CJK_NEEDLE = '"机密令"'


def test_allow_partial_orphan_recovery_keeps_trigram_coverage(tmp_path):
    source = tmp_path / "sessions-destroyed.db"
    output = tmp_path / "sessions-destroyed-recovered.db"

    db = SessionDB(db_path=source)
    if not db._fts_enabled or not db._trigram_available:
        db.close()
        pytest.skip("FTS5 / trigram tokenizer unavailable")
    try:
        for session_id, count in (("doomed-a", 8), ("doomed-b", 7)):
            db.create_session(session_id, "cli", cwd=f"/tmp/{session_id}")
            for index in range(count):
                db.append_message(
                    session_id, "user", f"irreplaceable 机密令牌数据 {session_id} {index}"
                )
    finally:
        db.close()

    # The reported shape: sessions unrecoverable, messages intact.
    conn = sqlite3.connect(str(source), isolation_level=None)
    try:
        conn.execute("DELETE FROM sessions")
    finally:
        conn.close()

    report = recover_session_database(
        source, output, work_dir=tmp_path, chunk_size=4, allow_partial=True
    )
    assert report["orphan_cleanup"]["sessions_reconstructed"] == 2
    assert report["orphan_cleanup"]["messages_retained"] == 15

    with sqlite3.connect(str(output)) as verify:
        expected = {
            row[0] for row in verify.execute("SELECT id FROM messages_fts_trigram_src")
        }
        indexed = {
            row[0]
            for row in verify.execute("SELECT id FROM messages_fts_trigram_docsize")
        }
        # Membership, not parity: the reconstructed rows have to be in the
        # inverted index, not merely visible through the content view.
        assert indexed == expected, (
            f"{len(expected) - len(indexed)} retained messages are missing from "
            "the trigram index after orphan reconstruction"
        )
        matched = {
            row[0]
            for row in verify.execute(
                "SELECT rowid FROM messages_fts_trigram "
                "WHERE messages_fts_trigram MATCH ?",
                (CJK_NEEDLE,),
            )
        }
    # The base unicode61 index cannot serve this substring, so a hit proves the
    # trigram index really carries the terms.
    assert matched == expected

    # End to end: substring search on the recovered database still answers, and
    # a database that cannot answer must at least be offered a repair.
    reopened = SessionDB(db_path=output)
    try:
        assert len(reopened.search_messages("机密令")) == 15
        assert reopened.fts_optimize_available() is False
    finally:
        reopened.close()
