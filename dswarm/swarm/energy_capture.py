"""M7 bounded read-only graph snapshot capture (docs/10 Contract v9.2).

Phase 1 = ONE short SQLite read transaction that reads raw rows only —
no hashing, no dataclass construction, no serialization (static assertion in
tests). Phase 2 runs after COMMIT.

Causal membership uses seq exclusively: ``graph_after_seq`` is the
single-transaction ``MAX(seq)``; under journal_mode=DELETE a writer cannot
commit while the read transaction is open, so every visible row is
<= graph_after_seq by construction. Timestamps are decay-only.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from typing import Any, Optional

from dswarm.solver.result_codes import is_genuine_giveup
from dswarm.swarm.energy import (
    DeadEndObservationSnapshot,
    EnergyObservationSnapshot,
    GraphCycleSnapshot,
)

DEFAULT_CAPTURE_DEADLINE_S = 5.0

# fact_effective column order (fact_events.FACT_EFFECTIVE_SELECT).
_VIEW_COLUMNS = (
    "fact_seq", "challenge_id", "fact_text", "fact_source", "fact_actor",
    "fact_ts", "base_verified", "base_confidence", "route_hash", "finding_kind",
    "finding_target", "finding_data", "promotion_seq", "promotion_actor",
    "promotion_artifact_id", "artifact_id", "witness", "verifier", "source",
    "summary_seq", "summary", "state", "retired", "verified", "confidence",
)

_INTENT_CONCLUDED = "intent_concluded"


def _phase1_read(conn: sqlite3.Connection, challenge_id: str) -> dict[str, Any]:
    """Phase 1: one short transaction, raw rows only."""
    conn.execute("BEGIN")
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events WHERE challenge_id=?",
            (challenge_id,),
        ).fetchone()
        graph_after_seq = int(row[0] or 0)
        view_rows = conn.execute(
            f"SELECT {', '.join(_VIEW_COLUMNS)} FROM fact_effective "
            "WHERE challenge_id=? ORDER BY fact_seq ASC",
            (challenge_id,),
        ).fetchall()
        promotion_rows = conn.execute(
            "SELECT seq, ts FROM events WHERE challenge_id=? AND kind='fact_verified' "
            "AND seq<=?",
            (challenge_id, graph_after_seq),
        ).fetchall()
        lineage_rows = conn.execute(
            "SELECT ip.fact_seq, ip.intent_id FROM intent_products ip "
            "JOIN intents i ON i.intent_id = ip.intent_id "
            "WHERE i.challenge_id=? AND i.created_seq<=?",
            (challenge_id, graph_after_seq),
        ).fetchall()
        conclusion_rows = conn.execute(
            "SELECT i.intent_id, i.route_hash, i.result_seq, i.result_detail, "
            "e.ts, e.payload FROM intents i "
            "JOIN events e ON e.seq = i.result_seq AND e.challenge_id = i.challenge_id "
            "WHERE i.challenge_id=? AND i.result_seq IS NOT NULL AND i.result_seq<=?",
            (challenge_id, graph_after_seq),
        ).fetchall()
        conclude_audit_rows = conn.execute(
            "SELECT json_extract(payload, '$.intent_id') AS intent_id, COUNT(*) AS n "
            "FROM events WHERE challenge_id=? AND kind=? AND json_valid(payload) "
            "GROUP BY 1",
            (challenge_id, _INTENT_CONCLUDED),
        ).fetchall()
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {
        "graph_after_seq": graph_after_seq,
        "view_rows": view_rows,
        "promotion_rows": promotion_rows,
        "lineage_rows": lineage_rows,
        "conclusion_rows": conclusion_rows,
        "conclude_audit_rows": conclude_audit_rows,
    }


def _build_snapshot(phase1: dict[str, Any]) -> GraphCycleSnapshot:
    """Phase 2 (outside the transaction): fold raw rows into validated
    dataclasses."""
    graph_after_seq = phase1["graph_after_seq"]
    observed = len(phase1["view_rows"])

    promotion_ts: dict[int, float] = {}
    for seq, ts in phase1["promotion_rows"]:
        try:
            value = float(ts)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            promotion_ts[int(seq)] = value

    lineage_map: dict[int, list[str]] = {}
    for fact_seq, intent_id in phase1["lineage_rows"]:
        lineage_map.setdefault(int(fact_seq), []).append(str(intent_id))
    for ids in lineage_map.values():
        ids.sort()

    conclude_counts: dict[str, int] = {}
    for intent_id, n in phase1["conclude_audit_rows"]:
        if intent_id:
            conclude_counts[str(intent_id)] = int(n or 0)

    observations: list[EnergyObservationSnapshot] = []
    for row in phase1["view_rows"]:
        cols = dict(zip(_VIEW_COLUMNS, row))
        try:
            confidence = float(cols["confidence"])
            if not math.isfinite(confidence):
                continue  # non_finite_confidence: excluded, not captured
        except (TypeError, ValueError):
            continue
        route_hash = str(cols["route_hash"] or "").strip()
        if not route_hash:
            continue  # missing_route_hash: excluded, not captured
        fact_ts = float(cols["fact_ts"])
        if not math.isfinite(fact_ts):
            continue
        promotion_seq = cols["promotion_seq"]
        if promotion_seq is not None and int(promotion_seq) in promotion_ts:
            energy_origin_ts = promotion_ts[int(promotion_seq)]
        else:
            energy_origin_ts = fact_ts
        artifact_id = str(cols["artifact_id"] or "").strip()
        basis = artifact_id or route_hash
        kind = "artifact" if artifact_id else "fallback"
        basis_hash = hashlib.blake2b(
            basis.encode("utf-8"), digest_size=16).hexdigest()
        intent_ids = lineage_map.get(int(cols["fact_seq"]), [])
        lineage = ",".join(intent_ids) if intent_ids else "unattributed"
        lineage_reason = "product_edge" if intent_ids else "no_producer"
        try:
            obs = EnergyObservationSnapshot(
                fact_seq=int(cols["fact_seq"]),
                fact_origin_ts=fact_ts,
                energy_origin_ts=energy_origin_ts,
                route_hash=route_hash,
                lineage=lineage,
                lineage_reason=lineage_reason,
                inherited_intent_ids=tuple(intent_ids),
                state=str(cols["state"] or "candidate"),
                retired=bool(cols["retired"]),
                verified=bool(cols["verified"]),
                base_verified=bool(cols["base_verified"]),
                confidence=confidence,
                witness=str(cols["witness"] or ""),
                artifact_id=artifact_id,
                source=str(cols["source"] or ""),
                actor=str(cols["fact_actor"] or ""),
                correlation_kind=kind,
                correlation_basis_hash=basis_hash,
                eligible_for_energy=True,
                exclusion_reason="",
            )
        except ValueError:
            continue  # invalid row: dropped, not captured
        observations.append(obs)

    dead_ends: list[DeadEndObservationSnapshot] = []
    for row in phase1["conclusion_rows"]:
        intent_id = str(row[0] or "")
        route_hash = str(row[1] or "").strip()
        result_seq = int(row[2])
        concluded_ts = float(row[4])
        if not math.isfinite(concluded_ts):
            continue
        try:
            payload = json.loads(str(row[5] or "{}"))
        except (TypeError, ValueError):
            payload = {}
        result = str(payload.get("result") or "")
        genuine = bool(is_genuine_giveup(result))
        eligible = genuine and bool(route_hash)
        if not eligible:
            reason = "missing_route_hash" if not route_hash else "not_genuine_giveup"
        else:
            reason = ""
        audit = conclude_counts.get(intent_id, 0)
        ignored = max(0, audit - 1) if audit else 0
        try:
            dead = DeadEndObservationSnapshot(
                intent_id=intent_id,
                route_hash=route_hash,
                result_seq=result_seq,
                concluded_ts=concluded_ts,
                result=result,
                genuine_giveup=genuine,
                eligible_for_energy=eligible,
                exclusion_reason=reason,
                conclusion_event_count=audit,
                ignored_stale_conclusion_count=ignored,
            )
        except ValueError:
            continue
        dead_ends.append(dead)

    return GraphCycleSnapshot(
        graph_after_seq=graph_after_seq,
        observations=tuple(observations),
        dead_ends=tuple(dead_ends),
        complete=True,
        exclusion_reason="",
        observed_fact_count=observed,
        captured_fact_count=len(observations),
        stored_fact_count=len(observations),
    )


def _snapshot_unavailable() -> GraphCycleSnapshot:
    return GraphCycleSnapshot(
        graph_after_seq=0,
        observations=(),
        dead_ends=(),
        complete=False,
        exclusion_reason="snapshot_unavailable",
        observed_fact_count=0,
        captured_fact_count=0,
        stored_fact_count=0,
    )


def capture_energy_cycle_snapshot(
    db_path: str,
    challenge_id: str,
    *,
    deadline_s: float = DEFAULT_CAPTURE_DEADLINE_S,
) -> GraphCycleSnapshot:
    """Bounded, dedicated read-only capture. Any failure degrades to an
    incomplete snapshot; it never raises to the scheduler."""
    deadline = time.monotonic() + max(0.0, float(deadline_s))
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=250")
        conn.set_progress_handler(
            lambda: 1 if time.monotonic() > deadline else 0, 100)
        phase1 = _phase1_read(conn, str(challenge_id))
        return _build_snapshot(phase1)
    except Exception:
        return _snapshot_unavailable()
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
