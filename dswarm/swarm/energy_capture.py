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
from dswarm.swarm.shared_graph import (
    IntentRouteRef,
    SQLiteSharedGraph,
    resolve_route_observation,
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
        fact_columns = ", ".join(f"fe.{name}" for name in _VIEW_COLUMNS)
        view_rows = conn.execute(
            f"SELECT {fact_columns}, "
            "json_extract(e.payload, '$.intent_id'), "
            "json_extract(e.payload, '$.orphan_intent_id') "
            "FROM fact_effective fe JOIN events e ON e.seq=fe.fact_seq "
            "WHERE fe.challenge_id=? ORDER BY fe.fact_seq ASC",
            (challenge_id,),
        ).fetchall()
        promotion_rows = conn.execute(
            "SELECT seq, ts FROM events WHERE challenge_id=? AND kind='fact_verified' "
            "AND seq<=?",
            (challenge_id, graph_after_seq),
        ).fetchall()
        lineage_rows = conn.execute(
            "SELECT ip.fact_seq, ip.intent_id, i.route_hash, i.created_seq "
            "FROM intent_products ip "
            "JOIN intents i ON i.intent_id = ip.intent_id "
            "WHERE i.challenge_id=? AND i.created_seq<=? "
            "ORDER BY ip.fact_seq, i.created_seq, i.intent_id",
            (challenge_id, graph_after_seq),
        ).fetchall()
        intent_rows = conn.execute(
            "SELECT intent_id, route_hash FROM intents "
            "WHERE challenge_id=? AND created_seq<=?",
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
        "intent_rows": intent_rows,
        "conclusion_rows": conclusion_rows,
        "conclude_audit_rows": conclude_audit_rows,
    }


def _build_snapshot(phase1: dict[str, Any]) -> GraphCycleSnapshot:
    """Phase 2: reuse M6 lineage resolution and fail closed on invalid rows."""
    graph_after_seq = int(phase1["graph_after_seq"])
    observed = len(phase1["view_rows"])
    invalid_rows = 0

    promotion_ts: dict[int, float] = {}
    for seq, ts in phase1["promotion_rows"]:
        try:
            value = float(ts)
            seq_value = int(seq)
            if not math.isfinite(value):
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            invalid_rows += 1
            continue
        promotion_ts[seq_value] = value

    intent_routes = {
        str(intent_id): route_hash
        for intent_id, route_hash in phase1["intent_rows"]
        if str(intent_id or "").strip()
    }
    products_by_fact: dict[int, list[IntentRouteRef]] = {}
    for fact_seq, intent_id, route_hash, _created_seq in phase1["lineage_rows"]:
        try:
            seq_value = int(fact_seq)
            intent_value = str(intent_id or "").strip()
            if not intent_value:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            invalid_rows += 1
            continue
        products_by_fact.setdefault(seq_value, []).append(
            IntentRouteRef(intent_id=intent_value, route_hash=str(route_hash or ""))
        )

    conclude_counts: dict[str, int] = {}
    for intent_id, n in phase1["conclude_audit_rows"]:
        if intent_id:
            try:
                conclude_counts[str(intent_id)] = int(n or 0)
            except (TypeError, ValueError, OverflowError):
                invalid_rows += 1

    observations: list[EnergyObservationSnapshot] = []
    extra_start = len(_VIEW_COLUMNS)
    for row in phase1["view_rows"]:
        cols = dict(zip(_VIEW_COLUMNS, row[:extra_start]))
        raw_payload_intent = row[extra_start]
        raw_orphan_intent = row[extra_start + 1]
        try:
            fact_seq = int(cols["fact_seq"])
            confidence = float(cols["confidence"])
            fact_ts = float(cols["fact_ts"])
            if not math.isfinite(confidence) or not math.isfinite(fact_ts):
                raise ValueError
            route_observation = resolve_route_observation(
                fact_seq=fact_seq,
                event_ts=fact_ts,
                explicit_route_hash=cols["route_hash"],
                inherited_routes=products_by_fact.get(fact_seq, ()),
                payload_intent_id=raw_payload_intent,
                payload_intent_route_hash=intent_routes.get(
                    str(raw_payload_intent or "").strip(), ""
                ),
                orphan_intent_id=raw_orphan_intent,
                orphan_intent_route_hash=intent_routes.get(
                    str(raw_orphan_intent or "").strip(), ""
                ),
                normalize_route=SQLiteSharedGraph._normalize_observed_route,
            )
            promotion_seq = cols["promotion_seq"]
            if promotion_seq is not None and int(promotion_seq) in promotion_ts:
                energy_origin_ts = promotion_ts[int(promotion_seq)]
            else:
                energy_origin_ts = fact_ts
            artifact_id = str(cols["artifact_id"] or "").strip()
            actor = str(cols["fact_actor"] or "")
            if artifact_id:
                correlation_kind = "artifact"
                correlation_basis = {
                    "kind": "artifact", "artifact_id": artifact_id
                }
            else:
                correlation_kind = "fallback"
                correlation_basis = {
                    "kind": "fallback", "fact_seq": fact_seq, "actor": actor
                }
            basis_json = json.dumps(
                correlation_basis, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"),
            )
            basis_hash = hashlib.blake2b(
                basis_json.encode("utf-8"), digest_size=16
            ).hexdigest()
            eligible = route_observation.eligible_for_energy
            if eligible:
                exclusion_reason = ""
            elif route_observation.lineage in {
                "explicit_conflict", "inherited_conflict"
            }:
                exclusion_reason = "lineage_unresolved"
            else:
                exclusion_reason = "missing_route_hash"
            observations.append(EnergyObservationSnapshot(
                fact_seq=fact_seq,
                fact_origin_ts=fact_ts,
                energy_origin_ts=energy_origin_ts,
                route_hash=route_observation.effective_route_hash,
                lineage=route_observation.lineage,
                lineage_reason=route_observation.reason,
                inherited_intent_ids=tuple(
                    ref.intent_id for ref in route_observation.inherited_routes
                ),
                state=str(cols["state"] or "candidate"),
                retired=bool(cols["retired"]),
                verified=bool(cols["verified"]),
                base_verified=bool(cols["base_verified"]),
                confidence=confidence,
                witness=str(cols["witness"] or ""),
                artifact_id=artifact_id,
                source=str(cols["source"] or ""),
                actor=actor,
                correlation_kind=correlation_kind,
                correlation_basis_hash=basis_hash,
                eligible_for_energy=eligible,
                exclusion_reason=exclusion_reason,
            ))
        except (TypeError, ValueError, OverflowError, IndexError):
            invalid_rows += 1

    dead_ends: list[DeadEndObservationSnapshot] = []
    for row in phase1["conclusion_rows"]:
        try:
            intent_id = str(row[0] or "")
            route_hash = SQLiteSharedGraph._normalize_observed_route(row[1])
            result_seq = int(row[2])
            concluded_ts = float(row[4])
            if not math.isfinite(concluded_ts):
                raise ValueError
            try:
                payload = json.loads(str(row[5] or "{}"))
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid conclusion payload") from exc
            if not isinstance(payload, dict):
                raise ValueError("invalid conclusion payload")
            result = str(payload.get("result") or "")
            genuine = bool(is_genuine_giveup(result))
            eligible = genuine and bool(route_hash)
            if not eligible:
                reason = (
                    "missing_route_hash" if not route_hash
                    else "not_genuine_giveup"
                )
            else:
                reason = ""
            audit = conclude_counts.get(intent_id, 0)
            ignored = max(0, audit - 1) if audit else 0
            dead_ends.append(DeadEndObservationSnapshot(
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
            ))
        except (TypeError, ValueError, OverflowError, IndexError):
            invalid_rows += 1

    return GraphCycleSnapshot(
        graph_after_seq=graph_after_seq,
        observations=tuple(observations),
        dead_ends=tuple(dead_ends),
        complete=invalid_rows == 0,
        exclusion_reason="" if invalid_rows == 0 else "snapshot_invalid_rows",
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
