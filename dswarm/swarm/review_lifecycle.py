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

from dswarm.swarm.graph_contract import (
    EV_EVIDENCE_REVIEW_DECISION,
    EV_FACT_CHALLENGED,
    EV_FACT_MERGED,
    EV_FACT_REJECTED,
    EV_FACT_REVALIDATED,
    EV_FACT_SUPERSEDED,
    EV_FACT_VERIFIED,
    EV_INTENT_QUALITY_DECISION,
    EV_REVIEWED_CONTEXT,
    EV_REVIEW_FINDING,
    EV_REVIEW_PROPOSAL,
    EV_REVIEW_PROPOSAL_DECISION,
    EV_WORKER_REPORT_PENDING_REVIEW,
    FACT_STATE_MERGED,
    FACT_STATE_REJECTED,
    FACT_STATE_SUPERSEDED,
    VALID_EVIDENCE_REVIEW_DECISIONS,
    VALID_INTENT_QUALITY_DECISIONS,
    _is_runtime_infra_fact_text,
)


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
    @staticmethod
    def _clean_refs(values: Optional[list[str]], *, limit: int = 24) -> list[str]:
        out: list[str] = []
        for raw in values or []:
            value = str(raw or "").strip()
            if not value or len(value) > 240 or value in out:
                continue
            out.append(value)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _clean_evidence_decision(value: str) -> str:
        decision = (value or "candidate").strip().lower()
        return decision if decision in VALID_EVIDENCE_REVIEW_DECISIONS else "candidate"

    @staticmethod
    def _clean_intent_decision(value: str) -> str:
        decision = (value or "defer").strip().lower()
        return decision if decision in VALID_INTENT_QUALITY_DECISIONS else "defer"

    def worker_report_pending_review(
        self, *, actor: str, worker_id: str, intent_id: str = "",
        fact_seqs: Optional[list[int]] = None, dead_end_seqs: Optional[list[int]] = None,
        artifact_ids: Optional[list[str]] = None,
        raw_artifact_ids: Optional[list[str]] = None,
        report_ref: str = "", runtime_only: bool = False,
    ) -> int:
        """Append a report quarantine marker without copying report contents."""
        clean_facts: list[int] = []
        for raw in fact_seqs or []:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in clean_facts:
                self._graph._require_fact_target(value)
                clean_facts.append(value)
        clean_dead_ends: list[int] = []
        for raw in dead_end_seqs or []:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in clean_dead_ends:
                self._graph._require_dead_end_target(value)
                clean_dead_ends.append(value)
        payload = {
            "worker_id": str(worker_id or actor).strip()[:160],
            "intent_id": str(intent_id or "").strip()[:160],
            "fact_seqs": clean_facts[:32],
            "dead_end_seqs": clean_dead_ends[:32],
            "artifact_ids": self._clean_refs(artifact_ids),
            "raw_artifact_ids": self._clean_refs(raw_artifact_ids),
            "runtime_only": bool(runtime_only),
            "status": "pending_review",
        }
        fingerprint = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return self._graph._append(
            EV_WORKER_REPORT_PENDING_REVIEW, actor, payload,
            dedupe_key=f"worker-report-pending::{payload['worker_id']}::{payload['intent_id']}::"
            f"{hashlib.sha1(fingerprint.encode()).hexdigest()[:16]}",
        )

    def decide_evidence_review(
        self, *, actor: str, decision: str, subject_id: str = "",
        fact_seq: Optional[int] = None, dead_end_seq: Optional[int] = None,
        intent_id: str = "", evidence_refs: Optional[list[str]] = None,
        reason: str = "", report_seq: Optional[int] = None, route_hash: str = "",
    ) -> int:
        """Record an evidence verdict and publish separate reviewed context.

        Genesis fact/dead-end rows are never updated.  A verified verdict is
        accepted only when the subject has a durable artifact and the reviewer
        names at least one bounded provenance reference.
        """
        clean_fact: Optional[int] = None
        clean_dead_end: Optional[int] = None
        target: Optional[dict[str, Any]] = None
        if fact_seq is not None:
            clean_fact = int(fact_seq)
            target = self._graph._require_fact_target(clean_fact)
        if dead_end_seq is not None:
            clean_dead_end = int(dead_end_seq)
            target = self._graph._require_dead_end_target(clean_dead_end)
        if clean_fact is not None and clean_dead_end is not None:
            raise ValueError("evidence review cannot target fact and dead-end together")
        clean_decision = self._clean_evidence_decision(decision)
        refs = self._clean_refs(evidence_refs)
        requested = clean_decision
        if clean_decision == "verified":
            artifact = str((target or {}).get("artifact_id") or "").strip()
            if not artifact or not refs:
                clean_decision = "candidate"
                reason = (reason or "").strip()
                reason = (reason + "; " if reason else "") + (
                    "verified requires artifact and evidence reference")
        default_subject = (
            f"fact:{clean_fact}" if clean_fact is not None else
            f"dead_end:{clean_dead_end}" if clean_dead_end is not None else ""
        )
        payload: dict[str, Any] = {
            "decision": clean_decision,
            "requested_decision": requested,
            "subject_id": str(subject_id or default_subject).strip()[:240],
            "fact_seq": clean_fact,
            "dead_end_seq": clean_dead_end,
            "intent_id": str(intent_id or "").strip()[:160],
            "evidence_refs": refs,
            "reason": str(reason or "").strip()[:1000],
            "report_seq": int(report_seq) if report_seq is not None else None,
            "route_hash": self._graph.normalize_route_hash(route_hash) if route_hash else "",
            "reviewer": str(actor or "reviewer").strip()[:160],
        }
        fp = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        seq = self._graph._append(
            EV_EVIDENCE_REVIEW_DECISION, actor, payload,
            dedupe_key=(f"evidence-review::{payload['subject_id']}::{clean_decision}::"
                        f"{hashlib.sha1(fp.encode()).hexdigest()[:16]}"),
        )
        if seq > 0 and (clean_fact is not None or clean_dead_end is not None):
            self.publish_reviewed_context(
                actor=actor,
                context_type="fact" if clean_fact is not None else "dead_end",
                subject_id=payload["subject_id"],
                fact_seqs=[clean_fact] if clean_fact is not None and clean_decision == "verified" else [],
                dead_end_seqs=[clean_dead_end] if clean_dead_end is not None and clean_decision == "verified" else [],
                reason=payload["reason"], source_review_seq=seq,
            )
        return seq

    def publish_reviewed_context(
        self, *, actor: str, context_type: str, subject_id: str = "",
        fact_seqs: Optional[list[int]] = None, dead_end_seqs: Optional[list[int]] = None,
        intent_ids: Optional[list[str]] = None, reason: str = "",
        source_review_seq: Optional[int] = None,
    ) -> int:
        clean_facts: list[int] = []
        for raw in fact_seqs or []:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                self._graph._require_fact_target(value)
                if value not in clean_facts:
                    clean_facts.append(value)
        clean_dead_ends: list[int] = []
        for raw in dead_end_seqs or []:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                self._graph._require_dead_end_target(value)
                if value not in clean_dead_ends:
                    clean_dead_ends.append(value)
        payload = {
            "context_type": str(context_type or "fact").strip()[:80],
            "subject_id": str(subject_id or "").strip()[:240],
            "fact_seqs": clean_facts[:32],
            "dead_end_seqs": clean_dead_ends[:32],
            "intent_ids": [str(x).strip()[:160] for x in (intent_ids or []) if str(x).strip()][:32],
            "reason": str(reason or "").strip()[:1000],
            "source_review_seq": int(source_review_seq) if source_review_seq is not None else None,
        }
        fp = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return self._graph._append(
            EV_REVIEWED_CONTEXT, actor, payload,
            dedupe_key=f"reviewed-context::{payload['context_type']}::{payload['subject_id']}::"
            f"{hashlib.sha1(fp.encode()).hexdigest()[:16]}",
        )

    def decide_intent_quality(
        self, *, actor: str, intent_id: str, decision: str, reason: str = "",
        replacement: Optional[dict[str, Any]] = None,
        evidence_refs: Optional[list[str]] = None, reason_cycle_id: str = "",
    ) -> int:
        """Append a Reviewer decision without mutating the proposed Intent."""
        clean_intent_id = str(intent_id or "").strip()
        if not clean_intent_id:
            raise ValueError("intent_id is required")
        clean_decision = self._clean_intent_decision(decision)
        clean_replacement = dict(replacement) if isinstance(replacement, dict) else None
        payload = {
            "intent_id": clean_intent_id[:160],
            "decision": clean_decision,
            "reason": str(reason or "").strip()[:1000],
            "replacement": clean_replacement,
            "evidence_refs": self._clean_refs(evidence_refs),
            "reason_cycle_id": str(reason_cycle_id or "").strip()[:160],
            "reviewer": str(actor or "reviewer").strip()[:160],
        }
        fingerprint = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return self._graph._append(
            EV_INTENT_QUALITY_DECISION, actor, payload,
            dedupe_key=(f"intent-quality::{payload['intent_id']}::{clean_decision}::"
                        f"{hashlib.sha1(fingerprint.encode()).hexdigest()[:16]}"),
        )

    def evidence_review_decisions(self) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for event in self._graph.events():
            if event.get("kind") != EV_EVIDENCE_REVIEW_DECISION:
                continue
            payload = dict(event.get("payload") or {})
            key = str(payload.get("subject_id") or payload.get("fact_seq") or payload.get("dead_end_seq") or "")
            if key:
                latest[key] = {**payload, "seq": int(event.get("seq") or 0),
                               "actor": str(event.get("actor") or "")}
        return list(latest.values())

    def intent_quality_decisions(self) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for event in self._graph.events():
            if event.get("kind") != EV_INTENT_QUALITY_DECISION:
                continue
            payload = dict(event.get("payload") or {})
            key = str(payload.get("intent_id") or "")
            if key:
                latest[key] = {**payload, "seq": int(event.get("seq") or 0),
                               "actor": str(event.get("actor") or "")}
        return list(latest.values())

    def reviewed_context(self) -> dict[str, Any]:
        """Fold reviewed business context; raw Worker reports stay quarantined."""
        decisions = self.evidence_review_decisions()
        by_fact: dict[int, dict[str, Any]] = {}
        by_dead_end: dict[int, dict[str, Any]] = {}
        for row in decisions:
            try:
                if row.get("fact_seq") is not None:
                    by_fact[int(row["fact_seq"])] = row
                if row.get("dead_end_seq") is not None:
                    by_dead_end[int(row["dead_end_seq"])] = row
            except (TypeError, ValueError):
                continue
        facts: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        for item in self._graph.effective_facts(active_only=True):
            seq = int(item.get("fact_seq") or 0)
            text = str(item.get("fact_text") or "")
            if not seq or _is_runtime_infra_fact_text(text):
                continue
            required = bool(item.get("review_required"))
            review = by_fact.get(seq)
            status = str(review.get("decision") or "") if review else ""
            if required and not review:
                candidates.append({"fact_seq": seq, "fact": text,
                                   "status": "pending_review", "reason": ""})
                continue
            if required and status != "verified":
                row = {"fact_seq": seq, "fact": text,
                       "status": status or "pending_review",
                       "reason": str((review or {}).get("reason") or "")}
                if status in {"candidate", "needs_retest", ""}:
                    candidates.append(row)
                elif status == "rejected":
                    rejected.append(row)
                continue
            is_verified = (status == "verified") if required else bool(item.get("verified"))
            row = {"fact_seq": seq, "fact": text,
                   "status": "verified" if is_verified else "candidate",
                   "review_seq": int((review or {}).get("seq") or 0) or None}
            (facts if is_verified else candidates).append(row)
        dead_ends: list[dict[str, Any]] = []
        for event in self._graph.events():
            if event.get("kind") != "dead_end":
                continue
            payload = dict(event.get("payload") or {})
            seq = int(event.get("seq") or 0)
            if not seq or _is_runtime_infra_fact_text(payload.get("reason", "")):
                continue
            required = bool(payload.get("review_required"))
            review = by_dead_end.get(seq)
            status = str(review.get("decision") or "") if review else ""
            if required and status != "verified":
                continue
            if not required and not any(
                    e.get("payload", {}).get("review_required")
                    for e in self._graph.events()
                    if e.get("kind") == "dead_end"):
                # Legacy direct graph events remain visible for old fixtures.
                status = "verified"
            if status == "verified":
                dead_ends.append({
                    "dead_end_seq": seq,
                    "reason": str(payload.get("reason") or ""),
                    "review_seq": int((review or {}).get("seq") or 0) or None,
                })
        return {"facts": facts, "candidates": candidates, "rejected": rejected,
                "dead_ends": dead_ends,
                "intent_decisions": self.intent_quality_decisions()}
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
