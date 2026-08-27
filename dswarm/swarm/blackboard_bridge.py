"""Shared-graph event to blackboard delta bridging."""

from __future__ import annotations

import hashlib
from typing import Any

from dswarm.swarm.shared_graph import _is_runtime_infra_fact_text
from dswarm.swarm.poc_verification import sanitize_public_text


class BlackboardBridgeMixin:
    @staticmethod
    def _split_ids(value: Any) -> list[str]:
        return [x.strip() for x in str(value or "").split(",") if x.strip()]

    def _graph_event_to_bb(self, ev: dict) -> list[tuple[str, dict]]:
        seq = int(ev.get("seq") or 0)
        kind = str(ev.get("kind") or "")
        actor = str(ev.get("actor") or "")
        p = dict(ev.get("payload") or {})
        if kind == "fact_added":
            if _is_runtime_infra_fact_text(p.get("fact", "")):
                return []
            return [("fact_added", {
                "fact": p.get("fact", ""),
                "source": p.get("source", ""),
                "source_solver": p.get("source_solver") or actor,
                "verified": bool(ev.get("verified")),
                "confidence": ev.get("confidence", 1.0),
                "verifier": p.get("verifier", ""),
                "witness": p.get("witness", ""),
                "artifact_id": ev.get("artifact_id"),
                "fact_seq": seq,
                "route_hash": p.get("route_hash", ""),
                "intent_id": p.get("intent_id", ""),
            })]
        if kind == "dead_end":
            return [("dead_end", {
                "reason": p.get("reason", ""),
                "dead_end_seq": seq,
            })]
        if kind == "intent_proposed":
            fields = dict(p)
            fields["intent_id"] = p.get("intent_id", "")
            fields["goal"] = p.get("goal", "")
            fields["intent_seq"] = seq
            return [("intent_proposed", fields)]
        if kind == "intent_claimed":
            return [("intent_claimed", {
                "intent_id": p.get("intent_id", ""),
                "worker": actor,
                "intent_seq": seq,
            })]
        if kind == "intent_concluded":
            out = []
            for iid in self._split_ids(p.get("intent_id")):
                out.append(("intent_concluded", {
                    "intent_id": iid,
                    "worker": actor,
                    "result": p.get("result", ""),
                    "result_detail": p.get("result_detail", ""),
                    "to_fact_seq": p.get("to_fact_seq"),
                    "intent_seq": seq,
                }))
            return out
        if kind == "intent_state_changed":
            out = []
            for iid in self._split_ids(p.get("intent_id")):
                fields = dict(p)
                fields["intent_id"] = iid
                fields["intent_seq"] = seq
                out.append(("intent_state_changed", fields))
            return out
        if kind == "flag_found":
            fields = dict(p)
            fields["flag_seq"] = seq
            return [("flag_found", fields)]
        if kind == "flag_unverified":
            fields = dict(p)
            fields["seq"] = seq
            fields["claim_seq"] = seq
            fields["source_actor"] = actor
            fields["artifact_id"] = ev.get("artifact_id") or fields.get("artifact_id") or ""
            return [("flag_unverified", fields)]
        if kind in {"poc_saved", "poc_claimed", "poc_concluded"}:
            fields = dict(p)
            fields["seq"] = seq
            return [(kind, fields)]
        if kind == "poc_reproduction_registered":
            indicator = str(p.get("indicator") or "")
            return [("poc_reproduction_registered", {
                "seq": seq,
                "poc_id": str(p.get("poc_id") or ""),
                "reproduction_id": str(p.get("reproduction_id") or ""),
                "status": "registered",
                "indicator_digest": hashlib.sha256(
                    indicator.encode("utf-8")
                ).hexdigest() if indicator else "",
                "indicator_length": len(indicator),
            })]
        if kind == "poc_reproduction_rejected":
            return [("poc_reproduction_rejected", {
                "seq": seq,
                "poc_id": str(p.get("poc_id") or ""),
                "status": "rejected",
                "reason": sanitize_public_text(p.get("reason") or "", limit=120),
                "candidate_indicator_digest": sanitize_public_text(
                    p.get("candidate_indicator_digest") or "", limit=80
                ),
            })]
        if kind == "poc_verification_started":
            return [("poc_verification_started", {
                "seq": seq,
                "poc_id": str(p.get("poc_id") or ""),
                "reproduction_id": str(p.get("reproduction_id") or ""),
                "verification_id": str(p.get("verification_id") or ""),
                "finding_id": sanitize_public_text(p.get("finding_id") or "", limit=160),
                "intent_id": sanitize_public_text(p.get("intent_id") or "", limit=160),
                "worker_id": sanitize_public_text(p.get("worker_id") or "", limit=160),
                "pool_identity": sanitize_public_text(p.get("pool_identity") or "", limit=160),
                "status": "started",
            })]
        if kind == "poc_verified":
            return [("poc_verified", {
                "seq": seq,
                "poc_id": str(p.get("poc_id") or ""),
                "reproduction_id": str(p.get("reproduction_id") or ""),
                "verification_id": str(p.get("verification_id") or ""),
                "status": "verified",
                "exit_code": p.get("exit_code"),
                "observed_location": sanitize_public_text(p.get("observed_location") or "", limit=80),
                "provenance_artifact_ids": [
                    sanitize_public_text(item, limit=160)
                    for item in (p.get("provenance_artifact_ids") or [])
                    if str(item or "").strip()
                ][:16],
                "elapsed_ms": p.get("elapsed_ms"),
            })]
        if kind == "poc_verification_failed":
            return [("poc_verification_failed", {
                "seq": seq,
                "poc_id": str(p.get("poc_id") or ""),
                "reproduction_id": str(p.get("reproduction_id") or ""),
                "verification_id": str(p.get("verification_id") or ""),
                "status": "failed",
                "reason": sanitize_public_text(p.get("reason") or "", limit=80),
                "exit_code": p.get("exit_code"),
                "diagnostics": sanitize_public_text(p.get("diagnostics") or ""),
                "elapsed_ms": p.get("elapsed_ms"),
            })]
        if kind == "review_finding_verified":
            return [("review_finding_verified", {
                "seq": seq,
                "finding_id": sanitize_public_text(p.get("finding_id") or "", limit=160),
                "poc_id": str(p.get("poc_id") or ""),
                "reproduction_id": str(p.get("reproduction_id") or ""),
                "verification_id": str(p.get("verification_id") or ""),
                "status": "verified",
            })]
        if kind == "review_finding":
            fields = dict(p)
            fields["seq"] = seq
            if "kind" in fields:
                fields["finding_kind"] = fields.pop("kind")
            return [("review_finding", fields)]
        if kind in {"route_suppressed", "route_reopened", "branch_split",
                    "branch_resolved", "coordinator_directive"}:
            fields = dict(p)
            fields["seq"] = seq
            return [(kind, fields)]
        return []
