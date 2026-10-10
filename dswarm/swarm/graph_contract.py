"""Pure SharedGraph contracts and route-lineage helpers.

The SQLite implementation remains in ``shared_graph.py``.  This module keeps
its event vocabulary, lifecycle constants, and side-effect-free route policy
independent from the persistence class so callers can reuse them without
loading the large SQLite implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Iterable, Optional, Protocol, runtime_checkable
from dswarm.models.solve_graph import SolveGraph
from dswarm.swarm.board import StructuredFinding
from dswarm.swarm.poc_verification import VerificationFailure

from dswarm.core.normalization import (
    normalize_lane_host as _clean_lane_host,
    normalize_lane_risk as _clean_lane_risk,
)


# Append-only event vocabulary.
EV_FACT_ADDED = "fact_added"
EV_HYP_PROPOSED = "hyp_proposed"
EV_HYP_REFUTED = "hyp_refuted"
EV_DEAD_END = "dead_end"
EV_INTENT_PROPOSED = "intent_proposed"
EV_INTENT_CLAIMED = "intent_claimed"
EV_INTENT_CONCLUDED = "intent_concluded"
EV_FLAG_FOUND = "flag_found"
EV_FLAG_UNVERIFIED = "flag_unverified"
EV_FLAG_INVALIDATED = "flag_invalidated"
EV_POC_SAVED = "poc_saved"
EV_POC_CLAIMED = "poc_claimed"
EV_POC_CONCLUDED = "poc_concluded"
EV_POC_REPRODUCTION_REGISTERED = "poc_reproduction_registered"
EV_POC_REPRODUCTION_REJECTED = "poc_reproduction_rejected"
EV_POC_VERIFICATION_STARTED = "poc_verification_started"
EV_POC_VERIFIED = "poc_verified"
EV_POC_VERIFICATION_FAILED = "poc_verification_failed"
EV_REVIEW_FINDING_VERIFIED = "review_finding_verified"
EV_REVIEW_FINDING = "review_finding"
EV_FACT_CHALLENGED = "fact_challenged"
EV_FACT_REVALIDATED = "fact_revalidated"
EV_FACT_VERIFIED = "fact_verified"
EV_FACT_SUMMARIZED = "fact_summarized"
EV_ROUTE_SUPPRESSED = "route_suppressed"
EV_ROUTE_REOPENED = "route_reopened"
EV_BRANCH_SPLIT = "branch_split"
EV_BRANCH_RESOLVED = "branch_resolved"
EV_COORDINATOR_DIRECTIVE = "coordinator_directive"
EV_REVIEW_PROPOSAL = "review_proposal"
EV_REVIEW_PROPOSAL_DECISION = "review_proposal_decision"
EV_LANE_LOCKED = "lane_locked"
EV_LANE_RELEASED = "lane_released"
EV_INTENT_LANE_DEFERRED = "intent_lane_deferred"
EV_FACT_REJECTED = "fact_rejected"
EV_FACT_MERGED = "fact_merged"
EV_FACT_SUPERSEDED = "fact_superseded"
EV_FACT_PINNED = "fact_pinned"
EV_INTENT_STATE_CHANGED = "intent_state_changed"
EV_OPERATOR_DIRECTIVE = "operator_directive"
EV_OPERATOR_DIRECTIVE_STATUS = "operator_directive_status"
EV_HITL_CLASSIFIED = "hitl_classified"
EV_RESOURCE_LOCKED = "resource_locked"
EV_RESOURCE_RELEASED = "resource_released"
EV_CLEANUP_ACTION_REGISTERED = "cleanup_action_registered"
EV_CLEANUP_EXECUTED = "cleanup_executed"
EV_CLEANUP_FAILED = "cleanup_failed"
EV_GRAPH_COMPACTED = "graph_compacted"

# Reviewer/recovery/provider events are separate transitions.  They never mutate
# the genesis Worker report or replace the hard provenance gate.
EV_WORKER_REPORT_PENDING_REVIEW = "worker_report_pending_review"
EV_EVIDENCE_REVIEW_DECISION = "evidence_review_decision"
EV_REVIEWED_CONTEXT = "reviewed_context"
EV_INTENT_QUALITY_DECISION = "intent_quality_decision"
EV_INTENT_INTERRUPTED = "intent_interrupted"
EV_INTENT_RECOVERY_REQUIRED = "intent_recovery_required"
EV_PROVIDER_RECOVERY_ACTIVATED = "provider_recovery_activated"
EV_BINDING_EFFECTIVE_CHANGED = "binding_effective_changed"


EVIDENCE_REVIEW_VERIFIED = "verified"
EVIDENCE_REVIEW_CANDIDATE = "candidate"
EVIDENCE_REVIEW_REJECTED = "rejected"
EVIDENCE_REVIEW_NEEDS_RETEST = "needs_retest"
EVIDENCE_REVIEW_RUNTIME_ONLY = "runtime_only"
VALID_EVIDENCE_REVIEW_DECISIONS = (
    EVIDENCE_REVIEW_VERIFIED,
    EVIDENCE_REVIEW_CANDIDATE,
    EVIDENCE_REVIEW_REJECTED,
    EVIDENCE_REVIEW_NEEDS_RETEST,
    EVIDENCE_REVIEW_RUNTIME_ONLY,
)

INTENT_QUALITY_APPROVED = "approved"
INTENT_QUALITY_REVISE = "revise"
INTENT_QUALITY_DUPLICATE = "duplicate"
INTENT_QUALITY_BLOCKED = "blocked"
INTENT_QUALITY_DEFER = "defer"
VALID_INTENT_QUALITY_DECISIONS = (
    INTENT_QUALITY_APPROVED,
    INTENT_QUALITY_REVISE,
    INTENT_QUALITY_DUPLICATE,
    INTENT_QUALITY_BLOCKED,
    INTENT_QUALITY_DEFER,
)


# Effective fact lifecycle states.
FACT_STATE_UNRESOLVED = "unresolved"
FACT_STATE_CHALLENGED = "challenged"
FACT_STATE_REVALIDATED = "revalidated"
FACT_STATE_REJECTED = "rejected"
FACT_STATE_MERGED = "merged"
FACT_STATE_SUPERSEDED = "superseded"
_FACT_TERMINAL_STATES = {
    FACT_STATE_REJECTED,
    FACT_STATE_MERGED,
    FACT_STATE_SUPERSEDED,
}
_FACT_STATES = {
    FACT_STATE_UNRESOLVED,
    FACT_STATE_CHALLENGED,
    FACT_STATE_REVALIDATED,
    FACT_STATE_REJECTED,
    FACT_STATE_MERGED,
    FACT_STATE_SUPERSEDED,
}


# Intent dispatch_state is orthogonal to open/claimed/done status.
INTENT_DISPATCH_ACTIVE = "active"
INTENT_DISPATCH_RESUME = "resume"
INTENT_DISPATCH_AWAITING_VALIDATION = "awaiting_validation"
INTENT_DISPATCH_RETIRED = "retired"
INTENT_DISPATCH_CLOSED = "closed"
_INTENT_DISPATCH_STATES = {
    INTENT_DISPATCH_ACTIVE,
    INTENT_DISPATCH_RESUME,
    INTENT_DISPATCH_AWAITING_VALIDATION,
    INTENT_DISPATCH_RETIRED,
    INTENT_DISPATCH_CLOSED,
}


_SERVICE_DEFAULT_PORTS = {
    "smb": 445,
    "microsoft-ds": 445,
    "http": 80,
    "https": 443,
    "rdp": 3389,
    "winrm": 5985,
    "winrm-http": 5985,
    "winrm-https": 5986,
    "ssh": 22,
    "redis": 6379,
    "mysql": 3306,
    "mssql": 1433,
    "ldap": 389,
    "ldaps": 636,
    "kerberos": 88,
    "postgres": 5432,
}


def _is_runtime_infra_fact_text(text: object) -> bool:
    """Identify worker/runtime failures that are not challenge evidence."""
    value = str(text or "").strip().lower()
    if not value:
        return False
    if "worker cli/runtime failed before producing solver output" in value:
        return True
    if "profile_incompatible offline eval cannot use custom endpoint profile" in value:
        return True
    if "unknown provider" in value and "dswarm-worker" in value and "--list-models" in value:
        return True
    if "endpoint probe failed:" in value and (
        "curl:" in value or "requested url returned error" in value
    ):
        return True
    return False


def canonicalize_lane(
    host: str = "",
    port: str | int | None = None,
    service: str = "",
    risk_class: str = "destructive",
) -> tuple[str, float, str]:
    """Return a stable lane key for dangerous or exclusive work."""
    risk = _clean_lane_risk(risk_class)
    clean_host, host_conf, host_reason = _clean_lane_host(host)
    if not clean_host:
        return "", 0.0, host_reason

    clean_service = re.sub(
        r"[^a-z0-9_-]+", "-", (service or "").strip().lower()
    ).strip("-")
    clean_port = ""
    if port not in (None, ""):
        try:
            candidate = int(str(port).strip())
            if 0 < candidate <= 65535:
                clean_port = str(candidate)
        except (TypeError, ValueError):
            clean_port = ""
    if not clean_port and clean_service in _SERVICE_DEFAULT_PORTS:
        clean_port = str(_SERVICE_DEFAULT_PORTS[clean_service])

    if not clean_port:
        if risk == "listener_port":
            return "", min(host_conf, 0.40), "listener_port_unknown"
        if risk in {"destructive", "exclusive_shell", "relay_service"}:
            clean_port = "*"
        else:
            return "", min(host_conf, 0.50), "port_unknown_fail_open"

    reason = host_reason if host_reason else ""
    confidence = min(1.0, host_conf if clean_port != "*" else min(host_conf, 0.65))
    return f"{risk}:tcp:{clean_port}@{clean_host}", confidence, reason


@dataclass(frozen=True)
class IntentRouteRef:
    intent_id: str
    route_hash: str


@dataclass(frozen=True)
class RouteObservation:
    fact_seq: int
    event_ts: float
    explicit_route_hash: str = ""
    inherited_routes: tuple[IntentRouteRef, ...] = ()
    effective_route_hash: str = ""
    lineage: str = "unattributed"
    reason: str = ""
    attempted_orphan_intent_id: str = ""
    attempted_orphan_route_hash: str = ""
    eligible_for_energy: bool = False


def resolve_route_observation(
    *,
    fact_seq: int,
    event_ts: float,
    explicit_route_hash: Any,
    inherited_routes: Iterable[IntentRouteRef] = (),
    payload_intent_id: Any = "",
    payload_intent_route_hash: Any = "",
    orphan_intent_id: Any = "",
    orphan_intent_route_hash: Any = "",
    normalize_route: Callable[[Any], str],
) -> RouteObservation:
    """Resolve route lineage without performing IO or mutating graph state."""
    explicit = normalize_route(explicit_route_hash)
    inherited = [
        IntentRouteRef(ref.intent_id, normalize_route(ref.route_hash))
        for ref in inherited_routes
    ]
    inherited_reason = "intent_product"
    payload_id = str(payload_intent_id or "").strip()
    if not inherited and payload_id:
        inherited.append(
            IntentRouteRef(payload_id, normalize_route(payload_intent_route_hash))
        )
        inherited_reason = "payload_intent_inherit"

    orphan_id = str(orphan_intent_id or "").strip()
    orphan_route = normalize_route(orphan_intent_route_hash) if orphan_id else ""
    inherited_values = {ref.route_hash for ref in inherited if ref.route_hash}

    effective = ""
    lineage = "unattributed"
    reason = "orphan_intent_reference" if orphan_id else "no_route_attribution"
    eligible = False
    if explicit:
        effective = explicit
        if inherited_values and explicit not in inherited_values:
            lineage = "explicit_conflict"
            reason = "explicit_inherited_conflict"
            eligible = False
        else:
            lineage = "explicit"
            reason = "explicit_matches_inherited" if inherited_values else "explicit_route"
            eligible = True
    elif len(inherited_values) > 1:
        lineage = "inherited_conflict"
        reason = "inherited_route_conflict"
    elif len(inherited_values) == 1:
        effective = next(iter(inherited_values))
        lineage = "inherited"
        reason = inherited_reason
        eligible = True
    elif inherited:
        reason = inherited_reason

    return RouteObservation(
        fact_seq=int(fact_seq),
        event_ts=float(event_ts),
        explicit_route_hash=explicit,
        inherited_routes=tuple(inherited),
        effective_route_hash=effective,
        lineage=lineage,
        reason=reason,
        attempted_orphan_intent_id=orphan_id,
        attempted_orphan_route_hash=orphan_route,
        eligible_for_energy=eligible,
    )


@runtime_checkable
class SharedGraph(Protocol):
    """Backend-swappable shared graph. Local = SQLite file; (future) cross-
    container = HTTP. Callers depend only on this surface."""

    def add_evidence(self, *, actor: str, source: str, fact: str,
                     artifact_id: Optional[str] = None, verified: bool = False,
                     confidence: float = 1.0, witness: Optional[str] = None,
                     verifier: str = "", route_hash: str = "",
                     intent_id: Optional[str] = None,
                     finding: Optional[StructuredFinding] = None,
                     review_required: bool = False) -> int: ...
    def route_lineage_for_fact(self, fact_seq: int) -> RouteObservation: ...

    def add_dead_end(self, *, actor: str, reason: str, review_required: bool = False,
                     intent_id: Optional[str] = None,
                     artifact_id: Optional[str] = None) -> int: ...

    def flag_found(self, *, actor: str, flag: str,
                   artifact_id: Optional[str] = None,
                   intent_id: Optional[str] = None) -> int: ...

    def flag_unverified(self, *, actor: str, flag: str, reason: str = "",
                        artifact_id: Optional[str] = None,
                        intent_id: Optional[str] = None,
                        context: str = "") -> int: ...

    def propose_intent(self, *, actor: str, intent_id: str, goal: str,
                       payload: Optional[dict] = None,
                       from_fact_seqs: Optional[list[int]] = None) -> int: ...

    def claim_intent(self, *, worker: str, intent_id: str,
                     lease_s: float = 300.0) -> bool: ...

    def conclude_intent(self, *, actor: str, intent_id: str,
                        result: str = "",
                        to_fact_seq: Optional[int] = None,
                        result_detail: str = "") -> int: ...

    def save_poc(self, *, actor: str, poc_id: str, path: str,
                 entry_command: str, status: str = "available",
                 note: str = "", artifact_id: Optional[str] = None,
                 intent_id: Optional[str] = None, name: str = "") -> int: ...

    def claim_poc(self, *, worker: str, poc_id: str,
                  lease_s: float = 300.0) -> bool: ...

    def conclude_poc(self, *, actor: str, poc_id: str,
                     status: str = "spent", note: str = "") -> int: ...

    def register_poc_reproduction(self, *, actor: str, poc_id: str,
                                  indicator: str) -> dict[str, Any]: ...

    def get_poc_reproduction(self, poc_id: str) -> Optional[dict[str, Any]]: ...

    def register_cleanup_action(
        self, *, actor: str, action_type: str, target: str,
        intent_id: str = "", poc_id: str = "", owner_key: str = "",
        idempotency_key: str = "",
    ) -> dict[str, Any]: ...

    def cleanup_actions(self, *, include_terminal: bool = True) -> list[dict[str, Any]]: ...

    def cleanup_action_executed(self, *, actor: str, action_id: str,
                                result: str = "") -> int: ...

    def cleanup_action_failed(self, *, actor: str, action_id: str,
                              reason: str = "") -> int: ...

    def poc_verification_status(self, poc_id: str) -> Optional[dict[str, Any]]: ...

    def begin_poc_verification(self, *, actor: str, poc_id: str,
                               verification_id: str, reproduction_id: str,
                               worker_id: str = "", finding_id: str = "",
                               intent_id: str = "", pool_identity: str = "",
                               lease_s: float = 600.0) -> Optional[dict[str, Any]]: ...

    def append_poc_verification_terminal(
        self, *, actor: str, poc_id: str, verification_id: str,
        verified: bool, exit_code: Optional[int] = None,
        failure_reason: Optional[VerificationFailure | str] = None,
        observed_location: str = "",
        provenance_artifact_ids: Optional[list[str]] = None,
        diagnostics: str = "", elapsed_ms: Optional[int] = None,
    ) -> int: ...

    def mark_review_finding_verified(
        self, *, actor: str, finding_id: str, poc_id: str,
        reproduction_id: str, verification_id: str,
    ) -> int: ...

    def supersede_open_intents(self, *, actor: str, match: str,
                               reason: str = "") -> list[str]: ...

    def add_review_finding(self, *, actor: str, kind: str, severity: str,
                           summary: str, evidence_seqs: Optional[list[int]] = None,
                           intent_ids: Optional[list[str]] = None,
                           route_hash: str = "", branch_id: str = "",
                           recommended_actions: Optional[list[str]] = None,
                           poc_id: str = "",
                           poc_ids: Optional[list[str]] = None) -> int: ...

    def add_review_proposal(self, *, actor: str, marker: str, payload: dict,
                            tier: str = "tier1") -> int: ...

    def decide_review_proposal(self, *, actor: str, proposal_seq: int,
                               decision: str, reason: str = "",
                               applied_seq: Optional[int] = None) -> int: ...

    def challenge_fact(self, *, actor: str, fact_seq: int, reason: str,
                       verification_goal: str) -> dict: ...

    def revalidate_fact(self, *, actor: str, fact_seq: int, reason: str = "") -> int: ...

    def reject_fact(self, *, actor: str, fact_seq: int, reason: str = "") -> int: ...

    def merge_fact(self, *, actor: str, from_fact_seq: int, to_fact_seq: int,
                   reason: str = "") -> int: ...

    def supersede_fact(self, *, actor: str, fact_seq: int, reason: str = "",
                       by_fact_seq: Optional[int] = None) -> int: ...

    def review_fact(self, *, actor: str, fact_seq: int, action: str,
                    reason: str = "", verification_goal: str = "",
                    to_fact_seq: Optional[int] = None) -> dict: ...

    def active_candidates(self) -> list[dict]: ...

    def verified_evidence(self) -> list[dict]: ...

    def suppress_route(self, *, actor: str, route_hash: str, label: str = "",
                       reason: str = "", until: str = "new_evidence",
                       matching_intents: Optional[list[str]] = None) -> dict: ...

    def reopen_route(self, *, actor: str, route_hash: str, reason: str = "",
                     intent_goal: str = "") -> dict: ...

    def split_branch(self, *, actor: str, title: str,
                     branches: list[dict[str, Any]]) -> dict: ...

    def resolve_branch(self, *, actor: str, branch_id: str, reason: str = "",
                       status: str = "resolved") -> dict: ...

    def add_coordinator_directive(self, *, actor: str, action: str,
                                  directive: str, priority: str = "normal",
                                  route_hash: str = "") -> int: ...

    def add_operator_directive(self, *, actor: str = "operator", action: str,
                               text: str, scope: str = "global",
                               standing: bool = False,
                               preempt_policy: str = "soft_rebind",
                               priority: Optional[int] = None) -> dict: ...

    def update_directive_status(self, *, directive_id: str, status: str,
                                actor: str = "coordinator",
                                generated_fact_seq: Optional[int] = None,
                                generated_intent_id: Optional[str] = None,
                                bound_worker: Optional[str] = None,
                                conflicts: Optional[list[str]] = None) -> int: ...

    def operator_directives(self, *, active_only: bool = True) -> list[dict]: ...

    def add_hitl_request(self, *, worker: str, need: str, need_kind: str,
                         classification_confidence: float = 1.0,
                         status: str = "classified",
                         directive_id: Optional[str] = None,
                         resource_lock_id: Optional[str] = None,
                         auto_action_seq: Optional[int] = None) -> dict: ...

    def lock_lane(self, *, actor: str, lane_key: str, risk_class: str,
                  owner_worker: str, owner_intent: str,
                  lease_s: float = 900.0) -> dict: ...

    def release_lane(self, *, actor: str, lane_key: str,
                     by_worker: str = "") -> dict: ...

    def defer_intent_for_lane(self, *, actor: str, intent_id: str,
                              lane_key: str, against_locked_seq: int = 0) -> int: ...

    def active_lanes(self) -> list[dict]: ...

    def request_resource_lock(self, *, actor: str, resource_key: str,
                              scope: str = "activity", risk_class: str = "",
                              owner_worker: str = "", owner_intent: str = "",
                              conflict_policy: str = "exclusive",
                              lease_s: float = 600.0, cooldown_s: float = 0.0) -> dict: ...

    def release_resource_lock(self, *, actor: str, resource_key: str = "",
                              lock_id: str = "", by_worker: str = "") -> dict: ...

    def active_resource_locks(self) -> list[dict]: ...

    def check_resource_conflicts(self, *, resource_key: str = "", lane_key: str = "",
                                 by_worker: str = "") -> dict: ...

    def is_lane_held_by_other(self, lane_key: str, by_worker: str) -> bool: ...

    def in_lane_cooldown(self, lane_key: str, worker: str) -> bool: ...
    def worker_report_pending_review(
        self, *, actor: str, worker_id: str, intent_id: str = "",
        fact_seqs: Optional[list[int]] = None,
        dead_end_seqs: Optional[list[int]] = None,
        artifact_ids: Optional[list[str]] = None,
        raw_artifact_ids: Optional[list[str]] = None,
        report_ref: str = "", runtime_only: bool = False,
    ) -> int: ...

    def decide_evidence_review(
        self, *, actor: str, decision: str, subject_id: str = "",
        fact_seq: Optional[int] = None, dead_end_seq: Optional[int] = None,
        intent_id: str = "", evidence_refs: Optional[list[str]] = None,
        reason: str = "", report_seq: Optional[int] = None,
        route_hash: str = "",
    ) -> int: ...

    def publish_reviewed_context(
        self, *, actor: str, context_type: str, subject_id: str = "",
        fact_seqs: Optional[list[int]] = None, intent_ids: Optional[list[str]] = None,
        reason: str = "", source_review_seq: Optional[int] = None,
    ) -> int: ...

    def decide_intent_quality(
        self, *, actor: str, intent_id: str, decision: str, reason: str = "",
        replacement: Optional[dict[str, Any]] = None,
        evidence_refs: Optional[list[str]] = None, reason_cycle_id: str = "",
    ) -> int: ...

    def mark_intent_awaiting_validation(
        self, *, actor: str, intent_id: str, reason: str = "",
        recovery_attempt: int = 0, worker_id: str = "",
    ) -> int: ...

    def reopen_interrupted_intent(
        self, *, actor: str, intent_id: str, recovery_intent_id: str,
        worker_id: str = "", reason: str = "",
    ) -> int: ...

    def awaiting_validation_intents(self, *, limit: int = 32) -> list[dict[str, Any]]: ...

    def expired_claim_intents(self, *, now: Optional[float] = None,
                              limit: int = 32) -> list[dict[str, Any]]: ...

    def park_expired_claims(self, *, actor: str, reason: str = "claim_lease_expired",
                            limit: int = 32) -> list[dict[str, Any]]: ...

    def reviewed_context(self) -> dict[str, Any]: ...
    def evidence_review_decisions(self) -> list[dict[str, Any]]: ...
    def intent_quality_decisions(self) -> list[dict[str, Any]]: ...

    def release_claims_for_finalize(self, *, reason: str) -> dict: ...

    def compact_graph(self, *, actor: str = "coordinator",
                      trigger: str = "no_progress_time", summary: str = "") -> dict: ...

    def compact_epochs(self) -> list[dict]: ...

    def revive_resume_intents(self, *, actor: str = "coordinator") -> list[str]: ...

    def prior_intent_count(self) -> int: ...

    def to_review_summary(self) -> str: ...

    def suppressed_routes(self) -> list[dict]: ...

    def challenged_facts(self) -> list[dict]: ...

    def branches(self) -> list[dict]: ...

    def coordinator_directives(self) -> list[dict]: ...

    def snapshot(self) -> SolveGraph: ...

    def invalidated_flags(self) -> set[str]: ...

    def unverified_flags(self) -> list[dict]: ...

    def events(self) -> list[dict]: ...
    def events_since(self, after_seq: int, kinds: Optional[list[str]] = None) -> list[dict]: ...
    async def subscribe_events(self, after_seq: int = 0,
                               kinds: Optional[list[str]] = None,
                               poll_interval: float = 0.5) -> Any: ...

    def to_summary(self, max_evidence: int = 16,
                   max_dead_ends: Optional[int] = None) -> str: ...

    def to_reason_summary(self, standing_guidance: Optional[list[str]] = None) -> str: ...

    def to_board_markdown(self) -> str: ...

    def open_goal_texts(self) -> list[str]: ...

    def dispatchable_goal_texts(self) -> list[str]: ...

    def dispatchable_intents(self, *, now: Optional[float] = None) -> list[dict]: ...

    def annotate_intent_lane(self, *, intent_id: str, lane_key: str,
                             risk_class: str = "") -> bool: ...

    def open_route_hashes(self) -> list[str]: ...

    def barren_concluded_goal_texts(self) -> list[str]: ...

    def pin_facts(self, *, actor: str, fact_seqs: list[int],
                  reason: str = "") -> list[int]: ...

    def pinned_fact_seqs(self) -> list[int]: ...

    def fact_pin_context(self, limit: int = 240) -> str: ...

    def try_claim_activity(self, *, worker: str, key: str,
                           lease_s: float = 600.0) -> bool: ...

    def release_activity(self, *, worker: str, key: str) -> None: ...

    def active_activities(self) -> list[dict]: ...

    def canonical_credentials(self) -> list[dict]: ...

__all__ = [
    "SharedGraph",
    "EV_FACT_ADDED", "EV_HYP_PROPOSED", "EV_HYP_REFUTED", "EV_DEAD_END",
    "EV_INTENT_PROPOSED", "EV_INTENT_CLAIMED", "EV_INTENT_CONCLUDED",
    "EV_FLAG_FOUND", "EV_FLAG_UNVERIFIED", "EV_FLAG_INVALIDATED",
    "EV_POC_SAVED", "EV_POC_CLAIMED", "EV_POC_CONCLUDED",
    "EV_POC_REPRODUCTION_REGISTERED", "EV_POC_REPRODUCTION_REJECTED",
    "EV_POC_VERIFICATION_STARTED", "EV_POC_VERIFIED", "EV_POC_VERIFICATION_FAILED",
    "EV_REVIEW_FINDING_VERIFIED", "EV_REVIEW_FINDING", "EV_FACT_CHALLENGED",
    "EV_FACT_REVALIDATED", "EV_FACT_VERIFIED", "EV_FACT_SUMMARIZED",
    "EV_ROUTE_SUPPRESSED", "EV_ROUTE_REOPENED", "EV_BRANCH_SPLIT", "EV_BRANCH_RESOLVED",
    "EV_COORDINATOR_DIRECTIVE", "EV_REVIEW_PROPOSAL", "EV_REVIEW_PROPOSAL_DECISION",
    "EV_LANE_LOCKED", "EV_LANE_RELEASED", "EV_INTENT_LANE_DEFERRED",
    "EV_FACT_REJECTED", "EV_FACT_MERGED", "EV_FACT_SUPERSEDED", "EV_FACT_PINNED",
    "EV_INTENT_STATE_CHANGED", "EV_OPERATOR_DIRECTIVE", "EV_OPERATOR_DIRECTIVE_STATUS",
    "EV_HITL_CLASSIFIED", "EV_RESOURCE_LOCKED", "EV_RESOURCE_RELEASED",
    "EV_CLEANUP_ACTION_REGISTERED", "EV_CLEANUP_EXECUTED", "EV_CLEANUP_FAILED",
    "EV_GRAPH_COMPACTED", "EV_WORKER_REPORT_PENDING_REVIEW",
    "EV_EVIDENCE_REVIEW_DECISION", "EV_REVIEWED_CONTEXT",
    "EV_INTENT_QUALITY_DECISION", "EV_INTENT_INTERRUPTED",
    "EV_INTENT_RECOVERY_REQUIRED", "EV_PROVIDER_RECOVERY_ACTIVATED",
    "EV_BINDING_EFFECTIVE_CHANGED",
    "EVIDENCE_REVIEW_VERIFIED", "EVIDENCE_REVIEW_CANDIDATE",
    "EVIDENCE_REVIEW_REJECTED", "EVIDENCE_REVIEW_NEEDS_RETEST",
    "EVIDENCE_REVIEW_RUNTIME_ONLY", "VALID_EVIDENCE_REVIEW_DECISIONS",
    "INTENT_QUALITY_APPROVED", "INTENT_QUALITY_REVISE", "INTENT_QUALITY_DUPLICATE",
    "INTENT_QUALITY_BLOCKED", "INTENT_QUALITY_DEFER", "VALID_INTENT_QUALITY_DECISIONS",
    "FACT_STATE_UNRESOLVED", "FACT_STATE_CHALLENGED", "FACT_STATE_REVALIDATED",
    "FACT_STATE_REJECTED", "FACT_STATE_MERGED", "FACT_STATE_SUPERSEDED",
    "INTENT_DISPATCH_ACTIVE", "INTENT_DISPATCH_RESUME", "INTENT_DISPATCH_AWAITING_VALIDATION",
    "INTENT_DISPATCH_RETIRED", "INTENT_DISPATCH_CLOSED", "canonicalize_lane", "IntentRouteRef", "RouteObservation",
    "resolve_route_observation",
]
