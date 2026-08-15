"""M3: canonical fact events are immutable and folded through fact_effective."""

from __future__ import annotations

import json
import sqlite3

import pytest

from dswarm.models.solve_graph import Challenge
from dswarm.swarm.shared_graph import SQLiteSharedGraph


def _challenge(challenge_id: str = "m3") -> Challenge:
    return Challenge(id=challenge_id, name="M3", category="web")


def _graph(tmp_path, challenge_id: str = "m3") -> SQLiteSharedGraph:
    return SQLiteSharedGraph.open(db_path=tmp_path / f"{challenge_id}.db", challenge=_challenge(challenge_id))


def test_new_database_installs_v2_contract_and_blocks_event_mutation(tmp_path):
    g = _graph(tmp_path)
    seq = g.add_evidence(actor="worker", source="curl", fact="candidate", verified=False)
    version = g._conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 2
    with pytest.raises(sqlite3.IntegrityError, match="events are immutable"):
        g._conn.execute("UPDATE events SET confidence=1 WHERE seq=?", (seq,))
    with pytest.raises(sqlite3.IntegrityError, match="events are immutable"):
        g._conn.execute("DELETE FROM events WHERE seq=?", (seq,))
    g.close()


def test_duplicate_verified_fact_appends_promotion_and_preserves_raw_genesis(tmp_path):
    g = _graph(tmp_path)
    seq = g.add_evidence(actor="worker", source="probe", fact="port 80 open", verified=False, confidence=0.4)
    assert g.add_evidence(
        actor="worker", source="probe", fact="port 80 open", verified=True,
        confidence=0.91, artifact_id="artifact-1", witness="curl output", verifier="gate",
    ) == -1

    raw = g.events_since(0)
    genesis = next(ev for ev in raw if ev["seq"] == seq)
    assert genesis["kind"] == "fact_added"
    assert genesis["verified"] is False
    assert genesis["confidence"] == 0.4
    promotions = [ev for ev in raw if ev["kind"] == "fact_verified"]
    assert len(promotions) == 1
    assert promotions[0]["payload"]["fact_seq"] == seq
    assert promotions[0]["artifact_id"] == "artifact-1"

    effective = g.effective_fact(seq)
    assert effective is not None
    assert effective["base_verified"] is True
    assert effective["base_confidence"] == pytest.approx(0.91)
    assert effective["verified"] is True
    assert effective["promotion_actor"] == "worker"
    assert effective["witness"] == "curl output"
    g.close()


def test_lifecycle_state_is_rebuildable_without_legacy_materialized_tables(tmp_path):
    g = _graph(tmp_path)
    first = g.add_evidence(actor="worker", source="probe", fact="candidate", verified=True, confidence=0.8)
    second = g.add_evidence(actor="worker", source="probe", fact="replacement", verified=False)
    g.challenge_fact(actor="review", fact_seq=first, reason="needs proof", verification_goal="recheck")
    g.merge_fact(actor="review", from_fact_seq=second, to_fact_seq=first, reason="duplicate")

    # M3 canonical state must live in immutable events, not in the legacy caches.
    assert g._conn.execute("SELECT COUNT(*) FROM fact_reviews").fetchone()[0] == 0
    assert g._conn.execute("SELECT COUNT(*) FROM fact_states").fetchone()[0] == 0
    assert g._conn.execute("SELECT COUNT(*) FROM fact_merges").fetchone()[0] == 0
    before = g.effective_facts()
    before_verified = g.verified_evidence()

    g._conn.execute("DELETE FROM fact_reviews")
    g._conn.execute("DELETE FROM fact_states")
    g._conn.execute("DELETE FROM fact_merges")
    g._conn.commit()

    assert g.effective_facts() == before
    assert g.verified_evidence() == before_verified
    g.close()


def test_effective_lifecycle_is_challenge_bound_and_terminal_is_sticky(tmp_path):
    g = _graph(tmp_path)
    seq = g.add_evidence(actor="worker", source="probe", fact="candidate", verified=True, confidence=0.8)
    g.reject_fact(actor="review", fact_seq=seq, reason="false positive")
    g.revalidate_fact(actor="review", fact_seq=seq, reason="later retry")

    effective = g.effective_fact(seq)
    assert effective is not None
    assert effective["state"] == "revalidated"
    assert effective["retired"] is True
    assert effective["verified"] is False
    assert effective["confidence"] == 0.0
    assert g.verified_evidence() == []
    g.close()


def test_summary_is_canonical_first_write_wins_with_legacy_fallback(tmp_path):
    g = _graph(tmp_path)
    seq = g.add_evidence(actor="worker", source="probe", fact="admin at /manage")
    assert g.record_fact_summary(fact_seq=seq, summary="管理端点") is True
    assert g.record_fact_summary(fact_seq=seq, summary="管理端点") is True
    assert g.record_fact_summary(fact_seq=seq, summary="另一个摘要") is False
    assert g.effective_fact(seq)["summary"] == "管理端点"
    summaries = g.events_since(0, kinds=("fact_summarized",))
    assert len(summaries) == 1
    g.close()

    # A legacy genesis summary remains readable after an explicit v2 migration.
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute("""CREATE TABLE events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, challenge_id TEXT NOT NULL,
        actor TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, artifact_id TEXT,
        verified INTEGER NOT NULL DEFAULT 0, confidence REAL NOT NULL DEFAULT 1.0,
        dedupe_key TEXT UNIQUE)""")
    conn.execute(
        "INSERT INTO events(ts,challenge_id,actor,kind,payload,verified,confidence,dedupe_key) VALUES(?,?,?,?,?,?,?,?)",
        (1.0, "legacy", "w", "fact_added", json.dumps({"fact": "old", "source": "x", "summary": "旧摘要"}), 0, 0.4, "f"),
    )
    conn.commit(); conn.close()
    old = SQLiteSharedGraph.open(db_path=legacy, challenge=_challenge("legacy"))
    backup = old.migrate_to_v2(backup_path=tmp_path / "legacy.pre-v2.db")
    assert backup.exists()
    assert old.effective_fact(1)["summary"] == "旧摘要"
    old.close()


def test_database_transition_guards_reject_bad_references_and_duplicates(tmp_path):
    g = _graph(tmp_path)
    seq = g.add_evidence(actor="worker", source="probe", fact="candidate", verified=False)

    def insert(kind: str, payload: dict, challenge_id: str = "m3"):
        g._conn.execute(
            "INSERT INTO events(ts,challenge_id,actor,kind,payload,verified,confidence,dedupe_key) VALUES(?,?,?,?,?,?,?,?)",
            (2.0, challenge_id, "direct", kind, json.dumps(payload), 0, 1.0, None),
        )

    for payload in ({}, {"fact_seq": "1"}, {"fact_seq": 999999}):
        with pytest.raises(sqlite3.IntegrityError):
            insert("fact_verified", payload)
        g._conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        insert("fact_verified", {"fact_seq": seq}, challenge_id="other")
    g._conn.rollback()

    insert("fact_verified", {"fact_seq": seq, "confidence": 0.9})
    g._conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        insert("fact_verified", {"fact_seq": seq, "confidence": 0.8})
    g._conn.rollback()

    other = g.add_evidence(actor="worker", source="probe", fact="other", verified=False)
    with pytest.raises(sqlite3.IntegrityError):
        insert("fact_merged", {"from_fact_seq": seq, "to_fact_seq": seq})
    g._conn.rollback()
    insert("fact_merged", {"from_fact_seq": seq, "to_fact_seq": other})
    g._conn.commit()
    g.close()


def test_future_database_version_is_rejected_by_all_open_paths(tmp_path):
    db = tmp_path / "future.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA user_version=99")
    conn.commit(); conn.close()
    with pytest.raises(sqlite3.DatabaseError, match="newer than supported"):
        SQLiteSharedGraph(db, _challenge())
    with pytest.raises(sqlite3.DatabaseError, match="newer than supported"):
        SQLiteSharedGraph.open(db_path=db, challenge=_challenge())
    with pytest.raises(sqlite3.DatabaseError, match="newer than supported"):
        SQLiteSharedGraph.open_readonly(db_path=db, challenge=_challenge())


def test_lifecycle_rejects_missing_fact_before_any_side_effect(tmp_path):
    graph = _graph(tmp_path)
    before = len(graph.events())
    with pytest.raises(ValueError, match="same-challenge fact_added"):
        graph.challenge_fact(
            actor="reviewer", fact_seq=999, reason="missing",
            verification_goal="verify it",
        )
    assert len(graph.events()) == before
    with graph._lock:
        assert graph._conn.execute("SELECT COUNT(*) FROM fact_reviews").fetchone()[0] == 0
        assert graph._conn.execute("SELECT COUNT(*) FROM intents").fetchone()[0] == 0
    graph.close()


def test_merge_requires_two_existing_same_challenge_facts(tmp_path):
    graph = _graph(tmp_path)
    seq = graph.add_evidence(
        actor="worker", source="probe", fact="one", verified=False, confidence=0.4,
    )
    with pytest.raises(ValueError, match="same-challenge fact_added"):
        graph.merge_fact(actor="reviewer", from_fact_seq=seq, to_fact_seq=999)
    assert graph.effective_fact(seq)["retired"] is False
    with graph._lock:
        assert graph._conn.execute("SELECT COUNT(*) FROM fact_merges").fetchone()[0] == 0
    graph.close()


def test_append_only_reports_guard_violation_instead_of_masking_as_dedupe(tmp_path):
    graph = _graph(tmp_path)
    with pytest.raises(sqlite3.IntegrityError, match="fact transition target"):
        graph._append(
            "fact_rejected", "reviewer", {"fact_seq": 999},
            dedupe_key="invalid-transition",
        )
    graph.close()


def _legacy_graph(tmp_path, challenge_id: str = "legacy-m3") -> SQLiteSharedGraph:
    path = tmp_path / f"{challenge_id}.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE legacy_marker(value INTEGER)")
    conn.commit()
    conn.close()
    graph = SQLiteSharedGraph.open(db_path=path, challenge=_challenge(challenge_id))
    assert graph._conn.execute("PRAGMA user_version").fetchone()[0] == 0
    return graph


def test_legacy_open_does_not_implicitly_upgrade_and_snapshot_is_restorable(tmp_path):
    graph = _legacy_graph(tmp_path, "legacy-open")
    seq = graph.add_evidence(actor="worker", source="probe", fact="legacy fact")
    assert graph._conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert graph._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name='fact_effective'"
    ).fetchone() is None

    backup_path = tmp_path / "legacy-open.pre-v2.db"
    assert graph.migrate_to_v2(backup_path=backup_path) == backup_path
    assert graph._conn.execute("PRAGMA user_version").fetchone()[0] == 2
    graph.close()

    restored = sqlite3.connect(backup_path)
    try:
        assert restored.execute("PRAGMA user_version").fetchone()[0] == 0
        assert restored.execute("SELECT kind FROM events WHERE seq=?", (seq,)).fetchone()[0] == "fact_added"
        assert restored.execute(
            "SELECT 1 FROM sqlite_master WHERE type='view' AND name='fact_effective'"
        ).fetchone() is None
    finally:
        restored.close()


def test_migration_preflight_reports_seq_and_does_not_bump_version(tmp_path):
    graph = _legacy_graph(tmp_path, "legacy-bad")
    with graph._lock:
        graph._conn.execute(
            "INSERT INTO events(ts,challenge_id,actor,kind,payload,verified,confidence,dedupe_key) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (1.0, graph.challenge.id, "review", "fact_rejected", "{bad", 0, 1.0, "bad"),
        )
        bad_seq = graph._conn.execute("SELECT MAX(seq) FROM events").fetchone()[0]
        graph._conn.commit()
    backup = tmp_path / "legacy-bad.pre-v2.db"
    with pytest.raises(sqlite3.DatabaseError, match=rf"events.seq={bad_seq}"):
        graph.migrate_to_v2(backup_path=backup)
    assert backup.exists()
    assert graph._conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert graph._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name='fact_effective'"
    ).fetchone() is None
    graph.close()


def test_migration_ddl_failure_rolls_back_contract_and_version(tmp_path):
    graph = _legacy_graph(tmp_path, "legacy-ddl")
    with graph._lock:
        # The index name collision occurs after CREATE VIEW, proving the DDL batch
        # must roll back as one transaction rather than leave a partial contract.
        graph._conn.execute("CREATE TABLE ux_events_fact_verified_once(value INTEGER)")
        graph._conn.commit()
    with pytest.raises(sqlite3.DatabaseError):
        graph.migrate_to_v2(backup_path=tmp_path / "legacy-ddl.pre-v2.db")
    assert graph._conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert graph._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name='fact_effective'"
    ).fetchone() is None
    assert graph._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='events_immutable_update'"
    ).fetchone() is None
    graph.close()


def test_migration_preflight_rejects_duplicate_historical_promotion(tmp_path):
    graph = _legacy_graph(tmp_path, "legacy-duplicate")
    fact_seq = graph.add_evidence(actor="worker", source="probe", fact="candidate", verified=False)
    with graph._lock:
        for dedupe in ("p1", "p2"):
            graph._conn.execute(
                "INSERT INTO events(ts,challenge_id,actor,kind,payload,verified,confidence,dedupe_key) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (1.0, graph.challenge.id, "review", "fact_verified",
                 json.dumps({"fact_seq": fact_seq, "confidence": 0.9}), 0, 1.0, dedupe),
            )
        duplicate_seq = graph._conn.execute("SELECT MAX(seq) FROM events").fetchone()[0]
        graph._conn.commit()
    with pytest.raises(sqlite3.DatabaseError, match=rf"events.seq={duplicate_seq}"):
        graph.migrate_to_v2(backup_path=tmp_path / "legacy-duplicate.pre-v2.db")
    assert graph._conn.execute("PRAGMA user_version").fetchone()[0] == 0
    graph.close()
