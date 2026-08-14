"""Shared-graph event to blackboard delta bridging."""

from __future__ import annotations

from typing import Any

from dswarm.swarm.shared_graph import _is_runtime_infra_fact_text


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
