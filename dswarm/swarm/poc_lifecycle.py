"""PoC registration and verification lifecycle for the shared graph.

This module is a structural seam only: the composed adapter keeps the existing
SQLite-backed behavior and append-only event semantics intact while isolating
the PoC domain from
the larger graph implementation. It deliberately depends on the host graph's
small persistence seam (``_append``, ``_conn``, ``_lock``, and activity helpers),
not on any alternate scheduling mode.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Optional

from dswarm.swarm.poc_verification import (
    VerificationFailure,
    normalize_reproduction_indicator,
    reproduction_id_for,
    sanitize_public_text,
    verification_failure_value,
)
from dswarm.solver.result_codes import is_genuine_giveup
from dswarm.swarm.shared_graph import (
    EV_POC_CLAIMED,
    EV_POC_CONCLUDED,
    EV_POC_REPRODUCTION_REGISTERED,
    EV_POC_REPRODUCTION_REJECTED,
    EV_POC_SAVED,
    EV_POC_VERIFICATION_FAILED,
    EV_POC_VERIFICATION_STARTED,
    EV_POC_VERIFIED,
    EV_REVIEW_FINDING_VERIFIED,
)


class PocLifecycle:
    """Persistence operations for saved PoCs and their verified reproductions."""

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    def save_poc(self, *, actor: str, poc_id: str, path: str,
                 entry_command: str, status: str = "available",
                 note: str = "", artifact_id: Optional[str] = None,
                 intent_id: Optional[str] = None, name: str = "") -> int:
        """Register a PoC as metadata for a shared artifact body.

        The body lives in workspace/shared CAS; this graph is the source of truth
        for inheritance state.
        """
        status = status if status in {"available", "wip", "directional", "spent", "quarantined"} else "available"
        iid = str(intent_id or "").strip() or None
        if iid and not self._graph._intent_owned_by(actor, iid):
            # A PoC belongs to the intent the worker actually owns; a stale
            # intent_id (e.g. an unaccounted worker reusing a closed intent) must
            # not be recorded on the poc row. Keep the artifact, drop the edge.
            payload_orphan = iid
            iid = None
        else:
            payload_orphan = None
        payload = {
            "poc_id": poc_id,
            "intent_id": iid,
            "name": name or Path(path).name,
            "path": path,
            "entry_command": entry_command,
            "status": status,
            "note": note,
        }
        if payload_orphan:
            payload["orphan_intent_id"] = payload_orphan
        seq = self._graph._append(EV_POC_SAVED, actor, payload,
                           artifact_id=artifact_id,
                           dedupe_key=f"poc::{poc_id}::{status}::{entry_command}::{note}")
        with self._graph._lock:
            self._graph._conn.execute(
                "INSERT INTO pocs "
                "(poc_id, challenge_id, intent_id, name, path, artifact_id, "
                " entry_command, status, note, created_seq) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(poc_id) DO UPDATE SET "
                " intent_id=excluded.intent_id, name=excluded.name, path=excluded.path, "
                " artifact_id=excluded.artifact_id, entry_command=excluded.entry_command, "
                " status=excluded.status, note=excluded.note",
                 (poc_id, self._graph.challenge.id, iid, payload["name"], path,
                  artifact_id, entry_command, status, note, seq if seq > 0 else 0),
            )
            self._graph._conn.commit()
        return seq


    def _intent_owned_by(self, actor: str, intent_id: str, *, row: "Optional[tuple]" = None) -> bool:
        """True if `actor` may attach products to `intent_id`: the intent is
        unclaimed (worker IS NULL), owned by `actor`, or `actor` is the
        coordinator/reviewer. `row` is an optional pre-fetched (worker,) row to
        avoid a second query."""
        if actor in ("coordinator", "review"):
            return True
        if row is None:
            with self._graph._lock:
                row = self._graph._conn.execute(
                    "SELECT worker FROM intents "
                    "WHERE challenge_id=? AND intent_id=? LIMIT 1",
                    (self._graph.challenge.id, intent_id),
                ).fetchone()
        if row is None:
            return False
        owner = row[0]
        return owner is None or owner == actor


    def claim_poc(self, *, worker: str, poc_id: str,
                  lease_s: float = 300.0) -> bool:
        now = time.time()
        with self._graph._lock:
            cur = self._graph._conn.execute(
                "UPDATE pocs SET worker=?, status='wip', lease_until=? "
                "WHERE poc_id=? AND challenge_id=? "
                "AND status IN ('available','directional','wip') "
                "AND (worker IS NULL OR lease_until IS NULL OR lease_until < ?)",
                (worker, now + lease_s, poc_id, self._graph.challenge.id, now),
            )
            self._graph._conn.commit()
            won = cur.rowcount == 1
        if won:
            self._graph._append(EV_POC_CLAIMED, worker, {"poc_id": poc_id})
        return won


    def conclude_poc(self, *, actor: str, poc_id: str,
                     status: str = "spent", note: str = "") -> int:
        status = status if status in {"available", "directional", "spent", "quarantined"} else "spent"
        seq = self._graph._append(EV_POC_CONCLUDED, actor,
                           {"poc_id": poc_id, "status": status, "note": note})
        fence = " AND (worker=? OR worker IS NULL)"
        with self._graph._lock:
            self._graph._conn.execute(
                "UPDATE pocs SET status=?, result_seq=? "
                "WHERE poc_id=? AND challenge_id=?" + fence,
                (status, seq if seq > 0 else None, poc_id, self._graph.challenge.id, actor),
            )
            self._graph._conn.commit()
        return seq


    def pocs(self, *, inheritable_only: bool = False) -> list[dict]:
        sql = ("SELECT poc_id, intent_id, name, path, artifact_id, entry_command, "
               "status, note, worker FROM pocs WHERE challenge_id=?")
        params: list[Any] = [self._graph.challenge.id]
        if inheritable_only:
            # A PoC is inheritable if it's available/directional, OR it was claimed
            # ('wip') but the claiming worker's lease has EXPIRED (#9). claim_poc
            # flips status→wip' to mark "in use by the current worker"; without the
            # expired-lease clause a wip PoC would vanish from the pool forever the
            # moment any worker claimed it (single-use inheritance —nothing ever
            # resets wip→available). Mirrors how _open_intents re-offers an
            # expired-lease 'claimed' intent. now() bound below.
            sql += (" AND (status IN ('available','directional') OR "
                    "(status='wip' AND (lease_until IS NULL OR lease_until < ?)))")
            params.append(time.time())
        sql += " ORDER BY created_seq"
        with self._graph._lock:
            rows = self._graph._conn.execute(sql, tuple(params)).fetchall()
        return [
            {"poc_id": r[0], "intent_id": r[1], "name": r[2], "path": r[3],
             "artifact_id": r[4], "entry_command": r[5], "status": r[6],
             "note": r[7], "worker": r[8]}
            for r in rows
        ]


    @staticmethod
    def _poc_reproduction_row(row: sqlite3.Row | tuple | None) -> Optional[dict[str, Any]]:
        if row is None:
            return None
        values = list(row)
        return {
            "reproduction_id": values[0],
            "poc_id": values[1],
            "intent_id": values[2] or "",
            "artifact_id": values[3],
            "command": values[4],
            "indicator": values[5],
            "registration_seq": int(values[6] or 0),
            "status": values[7],
            "verification_id": values[8] or "",
            "started_seq": int(values[9]) if values[9] is not None else None,
            "terminal_seq": int(values[10]) if values[10] is not None else None,
            "worker_id": values[11] or "",
            "finding_id": values[12] or "",
            "pool_identity": values[13] or "",
            "failure_reason": values[14] or "",
            "exit_code": int(values[15]) if values[15] is not None else None,
            "observed_location": values[16] or "",
            "provenance_artifact_ids": tuple(json.loads(values[17] or "[]")),
            "diagnostics": values[18] or "",
            "elapsed_ms": int(values[19]) if values[19] is not None else None,
        }


    def _select_poc_reproduction(self, poc_id: str) -> Optional[dict[str, Any]]:
        with self._graph._lock:
            row = self._graph._conn.execute(
                "SELECT reproduction_id, poc_id, intent_id, artifact_id, command, "
                "indicator, registration_seq, status, verification_id, started_seq, "
                "terminal_seq, worker_id, finding_id, pool_identity, failure_reason, "
                "exit_code, observed_location, provenance_artifact_ids, diagnostics, "
                "elapsed_ms FROM poc_reproductions WHERE challenge_id=? AND poc_id=?",
                (self._graph.challenge.id, str(poc_id)),
            ).fetchone()
        return self._graph._poc_reproduction_row(row)


    def _rebuild_poc_reproduction_projection(self) -> None:
        """Fold M9 PoC lifecycle events into the rebuildable projection table."""
        with self._graph._lock:
            self._graph._conn.execute(
                "DELETE FROM poc_reproductions WHERE challenge_id=?",
                (self._graph.challenge.id,),
            )
            rows = self._graph._conn.execute(
                "SELECT seq, kind, payload FROM events WHERE challenge_id=? "
                "AND kind IN (?,?,?,?,?,?) ORDER BY seq",
                (self._graph.challenge.id, EV_POC_REPRODUCTION_REGISTERED,
                 EV_POC_REPRODUCTION_REJECTED, EV_POC_VERIFICATION_STARTED,
                 EV_POC_VERIFIED, EV_POC_VERIFICATION_FAILED,
                 EV_REVIEW_FINDING_VERIFIED),
            ).fetchall()
            for seq, kind, raw_payload in rows:
                payload = json.loads(raw_payload or "{}")
                if kind == EV_POC_REPRODUCTION_REGISTERED:
                    self._graph._conn.execute(
                        "INSERT OR REPLACE INTO poc_reproductions "
                        "(reproduction_id, poc_id, challenge_id, intent_id, artifact_id, "
                        "command, indicator, registration_seq, status) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (payload["reproduction_id"], payload["poc_id"], self._graph.challenge.id,
                         payload.get("intent_id") or None, payload["artifact_id"],
                         payload["command"], payload["indicator"], int(seq), "registered"),
                    )
                elif kind == EV_POC_VERIFICATION_STARTED:
                    self._graph._conn.execute(
                        "UPDATE poc_reproductions SET status='started', "
                        "verification_id=?, started_seq=?, worker_id=?, finding_id=?, "
                        "pool_identity=? WHERE challenge_id=? AND reproduction_id=?",
                        (payload["verification_id"], int(seq), payload.get("worker_id") or "",
                         payload.get("finding_id") or "", payload.get("pool_identity") or "",
                         self._graph.challenge.id, payload["reproduction_id"]),
                    )
                elif kind in (EV_POC_VERIFIED, EV_POC_VERIFICATION_FAILED):
                    status = "verified" if kind == EV_POC_VERIFIED else "failed"
                    artifact_ids = json.dumps(payload.get("provenance_artifact_ids") or [])
                    self._graph._conn.execute(
                        "UPDATE poc_reproductions SET status=?, terminal_seq=?, "
                        "failure_reason=?, exit_code=?, observed_location=?, "
                        "provenance_artifact_ids=?, diagnostics=?, elapsed_ms=? "
                        "WHERE challenge_id=? AND reproduction_id=?",
                        (status, int(seq), payload.get("reason") or "",
                         payload.get("exit_code"), payload.get("observed_location") or "",
                         artifact_ids, payload.get("diagnostics") or "", payload.get("elapsed_ms"),
                         self._graph.challenge.id, payload["reproduction_id"]),
                    )
            self._graph._conn.commit()


    def register_poc_reproduction(self, *, actor: str, poc_id: str,
                                  indicator: str) -> dict[str, Any]:
        if getattr(self._graph.challenge, "mode", "ctf") != "pentest":
            raise ValueError("Verified-PoC reproduction requires pentest mode")
        normalized = normalize_reproduction_indicator(indicator)
        with self._graph._lock:
            poc = self._graph._conn.execute(
                "SELECT poc_id, intent_id, artifact_id, entry_command FROM pocs "
                "WHERE challenge_id=? AND poc_id=?",
                (self._graph.challenge.id, str(poc_id)),
            ).fetchone()
        if poc is None:
            raise ValueError("unknown PoC")
        _, intent_id, artifact_id, command = poc
        reproduction_id = reproduction_id_for(
            artifact_id=artifact_id or "", command=command or "", indicator=normalized
        )
        existing = self._graph._select_poc_reproduction(str(poc_id))
        if existing is not None:
            if existing["reproduction_id"] == reproduction_id:
                return dict(existing)
            self._graph._append(
                EV_POC_REPRODUCTION_REJECTED,
                actor,
                {
                    "poc_id": str(poc_id),
                    "existing_reproduction_id": existing["reproduction_id"],
                    "candidate_indicator_digest": hashlib.sha256(
                        normalized.encode("utf-8")
                    ).hexdigest(),
                    "reason": "conflicting_registration",
                },
                dedupe_key=(f"poc-repro-rejected::{poc_id}::"
                            f"{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"),
            )
            raise ValueError("conflicting reproduction registration")
        payload = {
            "poc_id": str(poc_id),
            "intent_id": intent_id or "",
            "artifact_id": str(artifact_id or ""),
            "command": str(command or ""),
            "indicator": normalized,
            "reproduction_id": reproduction_id,
        }
        seq = self._graph._append(
            EV_POC_REPRODUCTION_REGISTERED,
            actor,
            payload,
            dedupe_key=f"poc-repro::{poc_id}::{reproduction_id}",
        )
        with self._graph._lock:
            self._graph._conn.execute(
                "INSERT OR IGNORE INTO poc_reproductions "
                "(reproduction_id, poc_id, challenge_id, intent_id, artifact_id, command, "
                "indicator, registration_seq, status) VALUES (?,?,?,?,?,?,?,?,?)",
                (reproduction_id, str(poc_id), self._graph.challenge.id, intent_id or None,
                 artifact_id or "", command or "", normalized, seq if seq > 0 else 0,
                 "registered"),
            )
            self._graph._conn.commit()
        return dict(self._graph._select_poc_reproduction(str(poc_id)) or {})


    def get_poc_reproduction(self, poc_id: str) -> Optional[dict[str, Any]]:
        """Return a reproduction only when it still binds to its saved PoC row.

        This is the canonical resolution boundary for the Docker verifier: the
        lifecycle projection supplies the registered command/indicator while the
        saved-PoC projection supplies the immutable CAS path and user-facing name.
        """
        row = self._graph._select_poc_reproduction(str(poc_id))
        if row is None:
            return None
        with self._graph._lock:
            poc = self._graph._conn.execute(
                "SELECT artifact_id, path, name, entry_command FROM pocs "
                "WHERE challenge_id=? AND poc_id=?",
                (self._graph.challenge.id, str(poc_id)),
            ).fetchone()
        if poc is None:
            return None
        artifact_id, path, name, entry_command = poc
        if (
            str(artifact_id or "") != row["artifact_id"]
            or str(entry_command or "") != row["command"]
        ):
            return None
        resolved = dict(row)
        resolved["path"] = str(path or "")
        resolved["name"] = str(name or "")
        resolved["entry_command"] = str(entry_command or "")
        return resolved


    def poc_verification_status(self, poc_id: str) -> Optional[dict[str, Any]]:
        row = self._graph._select_poc_reproduction(str(poc_id))
        if row is None:
            return None
        return {
            "poc_id": row["poc_id"],
            "reproduction_id": row["reproduction_id"],
            "status": row["status"],
            "verification_id": row["verification_id"],
            "started_seq": row["started_seq"],
            "terminal_seq": row["terminal_seq"],
            "failure_reason": row["failure_reason"],
            "exit_code": row["exit_code"],
            "observed_location": row["observed_location"],
            "provenance_artifact_ids": tuple(row["provenance_artifact_ids"]),
            "diagnostics": row["diagnostics"],
            "elapsed_ms": row["elapsed_ms"],
        }


    def begin_poc_verification(self, *, actor: str, poc_id: str,
                               verification_id: str, reproduction_id: str,
                               worker_id: str = "", finding_id: str = "",
                               intent_id: str = "", pool_identity: str = "",
                               lease_s: float = 600.0) -> Optional[dict[str, Any]]:
        row = self._graph._select_poc_reproduction(str(poc_id))
        if row is None or row["reproduction_id"] != reproduction_id:
            return None
        if row["status"] in {"started", "verified", "failed"}:
            return None
        owner = str(worker_id or actor)
        lock_key = f"poc-verification:{reproduction_id}"
        if not self._graph.try_claim_activity(worker=owner, key=lock_key, lease_s=lease_s):
            return None
        payload = {
            "poc_id": row["poc_id"],
            "reproduction_id": reproduction_id,
            "verification_id": str(verification_id),
            "finding_id": str(finding_id or ""),
            "intent_id": str(intent_id or ""),
            "worker_id": owner,
            "pool_identity": sanitize_public_text(pool_identity, limit=160),
        }
        try:
            seq = self._graph._append(
                EV_POC_VERIFICATION_STARTED,
                actor,
                payload,
                dedupe_key=f"poc-verification-start::{reproduction_id}::{verification_id}",
            )
        except Exception:
            self._graph.release_activity(worker=owner, key=lock_key)
            raise
        with self._graph._lock:
            self._graph._conn.execute(
                "UPDATE poc_reproductions SET status='started', verification_id=?, "
                "started_seq=?, worker_id=?, finding_id=?, pool_identity=? "
                "WHERE challenge_id=? AND reproduction_id=?",
                (str(verification_id), seq if seq > 0 else 0, owner,
                 payload["finding_id"], payload["pool_identity"], self._graph.challenge.id,
                 reproduction_id),
            )
            self._graph._conn.commit()
        return dict(self._graph._select_poc_reproduction(str(poc_id)) or {})


    def append_poc_verification_terminal(
        self, *, actor: str, poc_id: str, verification_id: str,
        verified: bool, exit_code: Optional[int] = None,
        failure_reason: Optional[VerificationFailure | str] = None,
        observed_location: str = "",
        provenance_artifact_ids: Optional[list[str]] = None,
        diagnostics: str = "", elapsed_ms: Optional[int] = None,
    ) -> int:
        row = self._graph._select_poc_reproduction(str(poc_id))
        if row is None:
            raise ValueError("unknown PoC reproduction")
        if row["verification_id"] != str(verification_id):
            raise ValueError("verification does not own reproduction")
        if row["status"] in {"verified", "failed"}:
            return -1
        if row["status"] != "started":
            raise ValueError("verification must be started before terminal append")
        if not verified:
            reason = verification_failure_value(
                failure_reason or VerificationFailure.EXECUTION_ERROR
            )
        else:
            reason = ""
        artifact_ids = tuple(
            sanitize_public_text(item, limit=160)
            for item in (provenance_artifact_ids or [])
            if str(item or "").strip()
        )[:16]
        payload = {
            "poc_id": row["poc_id"],
            "reproduction_id": row["reproduction_id"],
            "verification_id": str(verification_id),
            "exit_code": int(exit_code) if exit_code is not None else None,
            "observed_location": sanitize_public_text(observed_location, limit=80),
            "provenance_artifact_ids": list(artifact_ids),
            "diagnostics": sanitize_public_text(diagnostics),
            "elapsed_ms": max(0, int(elapsed_ms)) if elapsed_ms is not None else None,
        }
        kind = EV_POC_VERIFIED if verified else EV_POC_VERIFICATION_FAILED
        if reason:
            payload["reason"] = reason
        seq = self._graph._append(
            kind,
            actor,
            payload,
            dedupe_key=f"poc-verification-terminal::{verification_id}",
        )
        with self._graph._lock:
            self._graph._conn.execute(
                "UPDATE poc_reproductions SET status=?, terminal_seq=?, failure_reason=?, "
                "exit_code=?, observed_location=?, provenance_artifact_ids=?, diagnostics=?, "
                "elapsed_ms=? WHERE challenge_id=? AND reproduction_id=?",
                ("verified" if verified else "failed", seq if seq > 0 else 0,
                 reason, payload["exit_code"], payload["observed_location"],
                 json.dumps(list(artifact_ids)), payload["diagnostics"],
                 payload["elapsed_ms"], self._graph.challenge.id, row["reproduction_id"]),
            )
            self._graph._conn.commit()
        self._graph.release_activity(
            worker=row["worker_id"] or actor,
            key=f"poc-verification:{row['reproduction_id']}",
        )
        return seq


    def mark_review_finding_verified(
        self, *, actor: str, finding_id: str, poc_id: str,
        reproduction_id: str, verification_id: str,
    ) -> int:
        row = self._graph._select_poc_reproduction(str(poc_id))
        if row is None:
            raise ValueError("unknown PoC reproduction")
        if row["reproduction_id"] != str(reproduction_id):
            raise ValueError("review finding verification does not match reproduction")
        if row["verification_id"] != str(verification_id):
            raise ValueError("review finding verification does not own reproduction")
        if row["status"] != "verified":
            raise ValueError("review finding requires durable verified PoC")
        payload = {
            "finding_id": sanitize_public_text(finding_id, limit=160),
            "poc_id": row["poc_id"],
            "reproduction_id": row["reproduction_id"],
            "verification_id": str(verification_id),
            "status": "verified",
        }
        if not payload["finding_id"]:
            raise ValueError("finding_id is required")
        return self._graph._append(
            EV_REVIEW_FINDING_VERIFIED,
            actor,
            payload,
            dedupe_key=(
                f"review-finding-verified::{payload['finding_id']}::"
                f"{row['reproduction_id']}::{verification_id}"
            ),
        )


