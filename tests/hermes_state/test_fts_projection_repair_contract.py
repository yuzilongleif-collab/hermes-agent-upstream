"""Semantic contract for FTS external-content projection repair.

These exercise the *search* contract of the partial-projection repair path,
not count parity: a docsize count can agree with its source cohort while the
inverted index holds the wrong terms. Every assertion here is membership
(explicit id sets) or a MATCH probe that only the trigram index can serve.

The file is written against APIs that exist both before and after the
partial-projection change (``optimize_fts_storage``,
``fts_optimize_available``, ``_repair_optimize_bookkeeping``,
``search_messages``) so it can be run unchanged on a pristine base to tell a
baseline defect apart from a regression.
"""

import pytest

import hermes_state_common
from hermes_state import SessionDB

# Mid-token substring and CJK needles: the default unicode61 base index cannot
# match either, so a hit proves the trigram inverted index really carries them.
SUBSTRING_NEEDLE = '"querulou"'
CJK_NEEDLE = '"机密令"'
REDACT_TOKEN = "zzredactedtokenzz"


def _ids(conn, sql, params=()):
    return {row[0] for row in conn.execute(sql, params).fetchall()}


def _match_rowids(conn, table, expr):
    return {
        row[0]
        for row in conn.execute(
            f"SELECT rowid FROM {table} WHERE {table} MATCH ?", (expr,)
        ).fetchall()
    }


def _mixed_cohort_db(tmp_path, name):
    """A DB whose trigram cohort is a strict subset of ``messages``.

    ``tool`` rows, ``cron`` sessions and ``subagent`` sessions are excluded
    from the trigram index by design, so this shape is what a parity check
    has to tolerate without reporting phantom work.
    """
    db = SessionDB(db_path=tmp_path / name)
    if not db._fts_enabled or not db._trigram_available:
        db.close()
        pytest.skip("FTS5 / trigram tokenizer unavailable")
    db.create_session(session_id="interactive", source="cli")
    db.create_session(session_id="nightly", source="cron")
    db.create_session(session_id="child", source="subagent")

    ids = {}
    ids["substring"] = db.append_message(
        "interactive", role="user", content="alpha zetaquerulous beta"
    )
    ids["cjk"] = db.append_message(
        "interactive", role="assistant", content="机密令牌数据 payload"
    )
    ids["redact"] = db.append_message(
        "interactive", role="user", content=f"{REDACT_TOKEN} please drop me"
    )
    for i in range(6):
        db.append_message("interactive", role="user", content=f"filler row {i}")
    ids["tool"] = db.append_message(
        "interactive", role="tool", tool_name="bash", content="机密令牌数据 tool output"
    )
    ids["cron"] = db.append_message(
        "nightly", role="user", content="机密令牌数据 cron transcript"
    )
    ids["subagent"] = db.append_message(
        "child", role="user", content="机密令牌数据 delegate transcript"
    )
    # The excluded cohort is non-empty: a parity check that ignored the view
    # and compared trigram docsize to COUNT(*) messages would misfire here.
    assert db._conn.execute(
        "SELECT COUNT(*) FROM messages_fts_trigram_src"
    ).fetchone()[0] < db._conn.execute(
        "SELECT COUNT(*) FROM messages"
    ).fetchone()[0]
    return db, ids


def _stamp_complete(db):
    db.set_meta("fts_storage_version", str(hermes_state_common.FTS_STORAGE_VERSION))


def _truncate_trigram_to_one_row(db, keep_id):
    """Field shape: an inverted index truncated to a few post-switch rows
    while the external content view still reports every source row."""
    db._conn.execute(
        "INSERT INTO messages_fts_trigram(messages_fts_trigram) VALUES('delete-all')"
    )
    db._conn.execute(
        "INSERT INTO messages_fts_trigram(rowid, content, tool_name) "
        "SELECT id, content, tool_name FROM messages WHERE id = ?",
        (keep_id,),
    )
    db._conn.commit()


def _assert_projection_membership(db, ids):
    """Both projections hold exactly their source cohort — by id, not count."""
    conn = db._conn
    assert _ids(conn, "SELECT id FROM messages_fts_docsize") == _ids(
        conn, "SELECT id FROM messages"
    )
    trigram_ids = _ids(conn, "SELECT id FROM messages_fts_trigram_docsize")
    assert trigram_ids == _ids(conn, "SELECT id FROM messages_fts_trigram_src")
    # The filtered cohort stays filtered after a repair replays the backfill.
    for excluded in ("tool", "cron", "subagent"):
        if excluded in ids:
            assert ids[excluded] not in trigram_ids, excluded


class TestFTSProjectionRepairContract:
    def test_healthy_mixed_cohort_is_never_treated_as_incomplete(self, tmp_path):
        """A complete index whose trigram cohort is legitimately smaller than
        ``messages`` must not be offered for optimize, and the bookkeeping
        repair must not truncate it.

        This is the regression guard for the parity check itself: a repair
        that resets the index to empty on a false positive would destroy a
        healthy production index and force a full rebuild on every open.
        """
        db, ids = _mixed_cohort_db(tmp_path, "healthy.db")
        try:
            _stamp_complete(db)
            _assert_projection_membership(db, ids)
            assert db.fts_optimize_available() is False

            before_base = _ids(db._conn, "SELECT id FROM messages_fts_docsize")
            before_trigram = _ids(
                db._conn, "SELECT id FROM messages_fts_trigram_docsize"
            )

            db._repair_optimize_bookkeeping()
            assert db.get_meta("fts_rebuild_high_water") is None
            assert db.get_meta("fts_storage_version") == str(
                hermes_state_common.FTS_STORAGE_VERSION
            )
            assert _ids(db._conn, "SELECT id FROM messages_fts_docsize") == before_base
            assert (
                _ids(db._conn, "SELECT id FROM messages_fts_trigram_docsize")
                == before_trigram
            )

            result = db.optimize_fts_storage(vacuum=False)
            assert result["ok"] is True
            assert db.fts_optimize_available() is False
            _assert_projection_membership(db, ids)
            assert _match_rowids(db._conn, "messages_fts_trigram", CJK_NEEDLE) == {
                ids["cjk"]
            }
        finally:
            db.close()

    def test_truncated_trigram_projection_is_detected_and_fully_healed(self, tmp_path):
        """Detection plus a real search-coverage heal, then idempotence.

        Count parity is not the claim: after the repair the trigram index has
        to answer the mid-token and CJK substring probes it could not answer
        while truncated, and the excluded cohort has to stay excluded.
        """
        db, ids = _mixed_cohort_db(tmp_path, "truncated.db")
        try:
            _stamp_complete(db)
            _truncate_trigram_to_one_row(db, ids["substring"])
            # Base index untouched and non-empty: EXISTS-style empty detection
            # cannot see this shape.
            assert db._conn.execute(
                "SELECT COUNT(*) FROM messages_fts_docsize"
            ).fetchone()[0] == db._conn.execute(
                "SELECT COUNT(*) FROM messages"
            ).fetchone()[0]
            assert _match_rowids(db._conn, "messages_fts_trigram", CJK_NEEDLE) == set()
            assert db.get_meta("fts_rebuild_high_water") is None

            assert db.fts_optimize_available() is True

            result = db.optimize_fts_storage(vacuum=False)
            assert result["ok"] is True
            _assert_projection_membership(db, ids)
            # Coverage the truncated index had lost is genuinely back.
            assert _match_rowids(db._conn, "messages_fts_trigram", CJK_NEEDLE) == {
                ids["cjk"]
            }
            assert _match_rowids(
                db._conn, "messages_fts_trigram", SUBSTRING_NEEDLE
            ) == {ids["substring"]}
            # The base index cannot serve these — the hits came from trigram.
            assert _match_rowids(db._conn, "messages_fts", CJK_NEEDLE) == set()
            assert [r["id"] for r in db.search_messages("机密令")] == [ids["cjk"]]

            # Second pass is a no-op, not another truncate-and-rebuild cycle.
            assert db.fts_optimize_available() is False
            db._repair_optimize_bookkeeping()
            assert db.get_meta("fts_rebuild_high_water") is None
            assert db.optimize_fts_storage(vacuum=False)["ok"] is True
            _assert_projection_membership(db, ids)
            assert _match_rowids(db._conn, "messages_fts_trigram", CJK_NEEDLE) == {
                ids["cjk"]
            }
        finally:
            db.close()

    def test_writes_during_the_seeded_gap_do_not_survive_the_repair(self, tmp_path):
        """Redaction and deletion inside the backfill claim must win.

        The repair seeds ``(progress, high_water]`` markers, which switch the
        sync triggers off for every existing id. A row redacted or deleted in
        that window is invisible to the triggers, so the replayed backfill is
        the only thing that can carry the new truth into the index.
        """
        db, ids = _mixed_cohort_db(tmp_path, "gap.db")
        try:
            _stamp_complete(db)
            _truncate_trigram_to_one_row(db, ids["substring"])
            assert ids["redact"] in _match_rowids(
                db._conn, "messages_fts", REDACT_TOKEN
            )

            db._repair_optimize_bookkeeping()
            high_water = db.get_meta("fts_rebuild_high_water")
            assert high_water is not None and int(high_water) >= ids["redact"]

            db._conn.execute(
                "UPDATE messages SET content = 'harmless replacement' WHERE id = ?",
                (ids["redact"],),
            )
            db._conn.execute("DELETE FROM messages WHERE id = ?", (ids["cron"],))
            db._conn.commit()

            assert db.optimize_fts_storage(vacuum=False)["ok"] is True

            # Redacted term is gone from every index and from the search API.
            assert _match_rowids(db._conn, "messages_fts", REDACT_TOKEN) == set()
            assert (
                _match_rowids(db._conn, "messages_fts_trigram", f'"{REDACT_TOKEN}"')
                == set()
            )
            assert db.search_messages(REDACT_TOKEN) == []
            # The replacement text and untouched rows are searchable.
            assert ids["redact"] in _match_rowids(
                db._conn, "messages_fts", "harmless"
            )
            assert _match_rowids(db._conn, "messages_fts_trigram", CJK_NEEDLE) == {
                ids["cjk"]
            }
            # The row deleted inside the gap leaves no orphan projection entry.
            assert ids["cron"] not in _ids(
                db._conn, "SELECT id FROM messages_fts_docsize"
            )
            assert ids["cron"] not in _ids(
                db._conn, "SELECT id FROM messages_fts_trigram_docsize"
            )
            del ids["cron"]
            _assert_projection_membership(db, ids)

            # A write after the repair settles is indexed by the live triggers.
            fresh = db.append_message(
                "interactive", role="user", content="机密令牌数据 after repair"
            )
            assert fresh in _match_rowids(
                db._conn, "messages_fts_trigram", CJK_NEEDLE
            )
        finally:
            db.close()
