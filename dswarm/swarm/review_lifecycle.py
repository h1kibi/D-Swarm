"""Review findings, proposals, and fact lifecycle operations.

This module isolates the review/revalidation domain from the larger shared graph
implementation. Like PocLifecycle, it delegates to the host graph's persistence
layer while keeping review semantics cohesive.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Optional

# Event and state constants inline to avoid circular import
EV_FACT_CHALLENGED = "fact_challenged"
EV_FACT_REJECTED = "fact_rejected"
EV_FACT_MERGED = "fact_merged"
EV_FACT_SUPERSEDED = "fact_superseded"
EV_FACT_REVALIDATED = "fact_revalidated"
EV_FACT_VERIFIED = "fact_verified"
EV_REVIEW_FINDING = "review_finding"
EV_REVIEW_PROPOSAL = "review_proposal"
EV_REVIEW_PROPOSAL_DECISION = "review_proposal_decision"
FACT_STATE_REJECTED = "rejected"
FACT_STATE_MERGED = "merged"
FACT_STATE_SUPERSEDED = "superseded"


class ReviewLifecycle:
    """Review findings, proposals, and fact challenge/revalidation operations."""

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    @staticmethod
    def _safe_review_severity(value: str) -> str:
        v = (value or "info").strip().lower()
        return v if v in {"info", "warn", "blocker"} else "warn"

    def add_review_finding(
        self, *, actor: str, kind: str, severity: str,
        summary: str, evidence_seqs: Optional[list[int]] = None,
        intent_ids: Optional[list[str]] = None,
        route_hash: str = "", branch_id: str = "",
        recommended_actions: Optional[list[str]] = None,
        poc_id: str = "",
        poc_ids: Optional[list[str]] = None,
    ) -> int:
        """Register a review finding with optional PoC linkage."""
        route = self._graph.normalize_route_hash(route_hash) if route_hash else ""
        fid_seed = f"{kind}:{summary}:{route}:{time.time()}"
        payload = {
            "finding_id": f"rvw-{hashlib.sha1(fid_seed.encode()).hexdigest()[:10]}",
            "kind": (kind or "no_action").strip() or "no_action",
            "severity": self._safe_review_severity(severity),
            "summary": (summary or "").strip()[:1000],
            "evidence_seqs": [int(x) for x in (evidence_seqs or []) if isinstance(x, int)],
            "intent_ids": [str(x) for x in (intent_ids or []) if x],
            "route_hash": route,
            "branch_id": (branch_id or "").strip(),
            "recommended_actions": [str(x) for x in (recommended_actions or []) if x],
        }
        explicit_pocs = []
        for raw in ([poc_id] if poc_id else []) + list(poc_ids or []):
            clean = str(raw or "").strip()
            if clean and clean not in explicit_pocs:
                explicit_pocs.append(clean)
        if explicit_pocs:
            payload["poc_ids"] = explicit_pocs
            if len(explicit_pocs) == 1:
                payload["poc_id"] = explicit_pocs[0]
        return self._graph._append(
            EV_REVIEW_FINDING, actor, payload,
            dedupe_key=f"review::{payload['kind']}::{payload['summary']}::{route}",
        )

    @staticmethod
    def _review_proposal_tier(marker: str) -> str:
        m = (marker or "").strip().upper()
        if m in {"ROUTE_SUPPRESS", "COORDINATOR_DIRECTIVE", "LANE_LOCK", "LANE_UNLOCK"}:
            return "tier2"
        return "tier1"

    def add_review_proposal(
        self, *, actor: str, marker: str, payload: dict, tier: str = "tier1"
    ) -> int:
        """Register a review proposal (e.g., route suppression, lane lock)."""
        marker = (marker or "").strip().upper()
        clean_payload = dict(payload or {})
        route_hash = str(clean_payload.get("route_hash") or "").strip()
        if route_hash:
            clean_payload["route_hash"] = self._graph.normalize_route_hash(route_hash)
        lane_key = str(clean_payload.get("lane_key") or "").strip()
        if lane_key:
            clean_payload["lane_key"] = self._graph.normalize_lane_key(lane_key)
        confidence = clean_payload.get("confidence", 1.0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 1.0
        clean_payload["confidence"] = max(0.0, min(1.0, confidence))
        clean_tier = tier if tier in {"tier1", "tier2"} else self._review_proposal_tier(marker)
        payload_out = {
            "marker": marker,
            "tier": clean_tier,
            "payload": clean_payload,
            "status": "pending",
        }
        fp = json.dumps(clean_payload, sort_keys=True, ensure_ascii=False, default=str)
        return self._graph._append(
            EV_REVIEW_PROPOSAL, actor, payload_out,
            dedupe_key=f"review-proposal::{marker}::{hashlib.sha1(fp.encode()).hexdigest()}",
        )

    def decide_review_proposal(
        self, *, actor: str, proposal_seq: int,
        decision: str, reason: str = "",
        applied_seq: Optional[int] = None,
    ) -> int:
        """Record a decision (accepted/deferred/rejected) on a review proposal."""
        clean_decision = (decision or "deferred").strip().lower()
        if clean_decision not in {"accepted", "deferred", "rejected"}:
            clean_decision = "deferred"
        payload = {
            "proposal_seq": int(proposal_seq),
            "decision": clean_decision,
            "reason": (reason or "").strip()[:1000],
        }
        if applied_seq is not None:
            payload["applied_seq"] = int(applied_seq)
        return self._graph._append(
            EV_REVIEW_PROPOSAL_DECISION, actor, payload,
            dedupe_key=f"review-proposal-decision::{proposal_seq}::{clean_decision}",
        )

    def challenge_fact(
        self, *, actor: str, fact_seq: int, reason: str, verification_goal: str
    ) -> dict:
        """Challenge a fact, creating a verifier intent to re-check it."""
        fact_seq = int(fact_seq)
        self._graph._require_fact_target(fact_seq)
        goal = (verification_goal or f"Verify fact #{fact_seq}: {reason}").strip()
        h = hashlib.sha1(f"{fact_seq}:{goal}".encode("utf-8", "ignore")).hexdigest()[:8]
        intent_id = f"I-verify-{fact_seq}-{h}"
        payload = {
            "fact_seq": fact_seq,
            "status": "challenged",
            "reason": (reason or "").strip()[:1000],
            "challenged_by": actor,
            "verification_intent_id": intent_id,
        }
        seq = self._graph._append(
            EV_FACT_CHALLENGED, actor, payload,
            dedupe_key=f"fact-challenged::{fact_seq}::{payload['reason']}",
        )
        if seq <= 0:
            return {
                "fact_seq": fact_seq,
                "verification_intent_id": intent_id,
                "seq": seq,
                "reason": payload["reason"],
            }
        self._graph.propose_intent(
            actor=actor, intent_id=intent_id, goal=goal,
            payload={
                "worker_class": "verifier",
                "depends_on": [str(fact_seq)],
                "rationale": f"Review challenged fact #{fact_seq}: {reason}",
            },
            from_fact_seqs=[fact_seq],
        )
        return {
            "fact_seq": fact_seq,
            "verification_intent_id": intent_id,
            "seq": seq,
            "reason": payload["reason"],
        }

    def revalidate_fact(self, *, actor: str, fact_seq: int, reason: str = "") -> int:
        """Mark a fact as revalidated after review."""
        fact_seq = int(fact_seq)
        self._graph._require_fact_target(fact_seq)
        payload = {
            "fact_seq": fact_seq,
            "status": "revalidated",
            "reason": (reason or "").strip()[:1000],
            "revalidated_by": actor,
        }
        return self._graph._append(
            EV_FACT_REVALIDATED, actor, payload,
            dedupe_key=f"fact-revalidated::{fact_seq}::{payload['reason']}",
        )

    def reject_fact(self, *, actor: str, fact_seq: int, reason: str = "") -> int:
        """Mark a fact REJECTED — review proved it false."""
        fact_seq = int(fact_seq)
        self._graph._require_fact_target(fact_seq)
        payload = {
            "fact_seq": fact_seq,
            "status": FACT_STATE_REJECTED,
            "reason": (reason or "").strip()[:1000],
            "rejected_by": actor,
        }
        return self._graph._append(
            EV_FACT_REJECTED, actor, payload,
            dedupe_key=f"fact-rejected::{fact_seq}::{payload['reason']}",
        )

    def merge_fact(
        self, *, actor: str, from_fact_seq: int, to_fact_seq: int, reason: str = ""
    ) -> int:
        """Fold `from_fact_seq` into `to_fact_seq` — they describe the same finding."""
        from_seq, to_seq = int(from_fact_seq), int(to_fact_seq)
        if from_seq == to_seq:
            return -1
        self._graph._require_fact_target(from_seq)
        self._graph._require_fact_target(to_seq)
        payload = {
            "from_fact_seq": from_seq,
            "to_fact_seq": to_seq,
            "status": FACT_STATE_MERGED,
            "reason": (reason or "").strip()[:1000],
            "merged_by": actor,
        }
        return self._graph._append(
            EV_FACT_MERGED, actor, payload,
            dedupe_key=f"fact-merged::{from_seq}::{to_seq}",
        )

    def supersede_fact(
        self, *, actor: str, fact_seq: int, reason: str = "",
        by_fact_seq: Optional[int] = None,
    ) -> int:
        """Mark a fact SUPERSEDED — a newer fact replaces it."""
        fact_seq = int(fact_seq)
        self._graph._require_fact_target(fact_seq)
        if by_fact_seq is not None:
            self._graph._require_fact_target(int(by_fact_seq))
        payload = {
            "fact_seq": fact_seq,
            "status": FACT_STATE_SUPERSEDED,
            "reason": (reason or "").strip()[:1000],
            "superseded_by": actor,
        }
        if by_fact_seq is not None:
            payload["by_fact_seq"] = int(by_fact_seq)
        return self._graph._append(
            EV_FACT_SUPERSEDED, actor, payload,
            dedupe_key=f"fact-superseded::{fact_seq}::{payload['reason']}",
        )

    def verify_fact(self, *, actor: str, fact_seq: int, reason: str = "") -> int:
        """Promote a fact to 'verified' status after review."""
        fact_seq = int(fact_seq)
        self._graph._require_fact_target(fact_seq)
        payload = {
            "fact_seq": fact_seq,
            "status": "verified",
            "reason": (reason or "").strip()[:1000],
            "verified_by": actor,
        }
        return self._graph._append(
            EV_FACT_VERIFIED, actor, payload,
            dedupe_key=f"fact-verified::{fact_seq}::{payload['reason']}",
        )

    def review_fact(
        self, *, actor: str, fact_seq: int, action: str,
        reason: str = "", verification_goal: str = "",
        to_fact_seq: Optional[int] = None,
    ) -> dict:
        """Unified fact review dispatcher (challenge/revalidate/reject/merge/supersede)."""
        act = (action or "").strip().lower()
        if act in ("challenge", "challenged"):
            res = self.challenge_fact(
                actor=actor, fact_seq=fact_seq, reason=reason,
                verification_goal=verification_goal,
            )
            return {
                "action": "challenge",
                "fact_seq": int(fact_seq),
                "seq": int(res.get("seq") or 0),
            }
        if act in ("revalidate", "revalidated"):
            seq = self.revalidate_fact(actor=actor, fact_seq=fact_seq, reason=reason)
            return {"action": "revalidate", "fact_seq": int(fact_seq), "seq": seq}
        if act in ("reject", "rejected"):
            seq = self.reject_fact(actor=actor, fact_seq=fact_seq, reason=reason)
            return {"action": "reject", "fact_seq": int(fact_seq), "seq": seq}
        if act in ("merge", "merged"):
            seq = self.merge_fact(
                actor=actor, from_fact_seq=fact_seq,
                to_fact_seq=int(to_fact_seq or 0), reason=reason,
            )
            return {"action": "merge", "fact_seq": int(fact_seq), "seq": seq}
        if act in ("supersede", "superseded"):
            seq = self.supersede_fact(
                actor=actor, fact_seq=fact_seq, reason=reason, by_fact_seq=to_fact_seq,
            )
            return {"action": "supersede", "fact_seq": int(fact_seq), "seq": seq}
        return {"action": act, "fact_seq": int(fact_seq), "seq": -1}
