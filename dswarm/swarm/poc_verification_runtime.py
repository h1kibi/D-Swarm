"""Durable orchestration for M9 Verified-PoC verification."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping
import uuid

from dswarm.solver.poc_verifier import (
    ContainerPocVerifier,
    ResolvedPocRegistration,
    VerifierExecutionResult,
)
from dswarm.swarm.poc_verification import (
    VerificationFailure,
    sanitize_public_text,
    verification_failure_value,
)


@dataclass(frozen=True)
class VerificationOutcome:
    status: str
    poc_id: str
    reproduction_id: str
    verification_id: str
    source_finding_id: str = ""
    intent_id: str = ""
    worker_id: str = ""
    verified: bool = False
    exit_code: int | None = None
    observed_location: str = ""
    provenance_artifact_ids: tuple[str, ...] = ()
    failure_reason: str = ""
    diagnostics: str = ""
    elapsed_ms: int | None = None
    started_seq: int | None = None
    terminal_seq: int | None = None
    review_finding_verified_seq: int | None = None


async def run_poc_verification(
    intent_metadata: Mapping[str, Any] | Any,
    *,
    graph: Any,
    verifier: Any | None = None,
    runtime_lease_factory: Callable[[str, str], Awaitable[Any]],
    usage_context: Mapping[str, Any] | Any,
) -> VerificationOutcome:
    """Run one registered PoC verifier lifecycle with graph-first durability.

    The executable authority is resolved only from the shared graph's canonical
    reproduction boundary.  This function emits no success delta until the
    terminal graph append succeeds.
    """

    verifier = verifier or ContainerPocVerifier()
    poc_id = _field(intent_metadata, "poc_id")
    reproduction_id = _field(intent_metadata, "reproduction_id")
    source_finding_id = _field(intent_metadata, "source_finding_id")
    intent_id = _field(intent_metadata, "intent_id")
    worker_id = _field(usage_context, "worker_id") or _field(intent_metadata, "worker_id")
    if not worker_id:
        worker_id = f"poc-verifier-{uuid.uuid4().hex[:12]}"
    verification_id = _field(intent_metadata, "verification_id") or f"poc-verification-{uuid.uuid4().hex}"
    timeout = _positive_timeout(_field(usage_context, "timeout", default=120.0))
    operation_kind = _field(usage_context, "operation_kind", default="review") or "review"
    workspace_root = _field(usage_context, "workspace_root")
    emit_delta = _field(usage_context, "emit_delta", default=None)

    if not poc_id or not reproduction_id or not workspace_root:
        return _outcome(
            VerificationFailure.MISSING_REPRODUCTION.value,
            poc_id=poc_id,
            reproduction_id=reproduction_id,
            verification_id=verification_id,
            source_finding_id=source_finding_id,
            intent_id=intent_id,
            worker_id=worker_id,
            diagnostics="missing verifier metadata",
        )

    try:
        registration = ResolvedPocRegistration.from_graph(
            graph,
            poc_id=poc_id,
            reproduction_id=reproduction_id,
            workspace_root=Path(workspace_root),
        )
    except (TypeError, ValueError):
        return _outcome(
            VerificationFailure.MISSING_REPRODUCTION.value,
            poc_id=poc_id,
            reproduction_id=reproduction_id,
            verification_id=verification_id,
            source_finding_id=source_finding_id,
            intent_id=intent_id,
            worker_id=worker_id,
            diagnostics="missing graph reproduction registration",
        )

    # Fast duplicate guard before taking a Docker capacity permit.  The graph's
    # begin call remains authoritative for races after this read.
    try:
        current = graph.poc_verification_status(poc_id)
    except Exception:
        current = None
    if isinstance(current, Mapping) and current.get("status") in {"started", "verified", "failed"}:
        return _outcome(
            VerificationFailure.LEASE_UNAVAILABLE.value,
            poc_id=poc_id,
            reproduction_id=reproduction_id,
            verification_id=verification_id,
            source_finding_id=source_finding_id,
            intent_id=intent_id,
            worker_id=worker_id,
            diagnostics="reproduction verification already claimed or terminal",
        )

    try:
        lease = await runtime_lease_factory(worker_id, operation_kind)
    except asyncio.CancelledError:
        raise
    except Exception:
        return _outcome(
            VerificationFailure.LEASE_UNAVAILABLE.value,
            poc_id=poc_id,
            reproduction_id=reproduction_id,
            verification_id=verification_id,
            source_finding_id=source_finding_id,
            intent_id=intent_id,
            worker_id=worker_id,
            diagnostics="runtime lease unavailable",
        )

    pool_identity = _pool_identity(lease)
    started_row = None
    try:
        started_row = graph.begin_poc_verification(
            actor=worker_id,
            poc_id=poc_id,
            verification_id=verification_id,
            reproduction_id=reproduction_id,
            worker_id=worker_id,
            finding_id=source_finding_id,
            intent_id=intent_id,
            pool_identity=pool_identity,
            lease_s=max(timeout + 60.0, 120.0),
        )
    except asyncio.CancelledError:
        await _release_if_needed(lease)
        raise
    except Exception:
        await _release_if_needed(lease)
        raise
    if not started_row:
        await _release_if_needed(lease)
        return _outcome(
            VerificationFailure.LEASE_UNAVAILABLE.value,
            poc_id=poc_id,
            reproduction_id=reproduction_id,
            verification_id=verification_id,
            source_finding_id=source_finding_id,
            intent_id=intent_id,
            worker_id=worker_id,
            diagnostics="reproduction verification already claimed or terminal",
        )
    started_seq = _safe_int(started_row.get("started_seq")) if isinstance(started_row, Mapping) else None

    try:
        result = await verifier.verify(registration, lease, timeout=timeout)
    except asyncio.CancelledError:
        _append_cancelled_terminal(
            graph=graph,
            actor=worker_id,
            poc_id=poc_id,
            verification_id=verification_id,
        )
        raise

    if not isinstance(result, VerifierExecutionResult):
        result = VerifierExecutionResult(
            status=VerificationFailure.PROVENANCE_UNAVAILABLE.value,
            diagnostics="verifier returned no normalized provenance",
        )

    verified = bool(result.verified)
    failure_reason = "" if verified else verification_failure_value(result.status)
    terminal_seq = graph.append_poc_verification_terminal(
        actor=worker_id,
        poc_id=poc_id,
        verification_id=verification_id,
        verified=verified,
        exit_code=result.exit_code,
        failure_reason=failure_reason or None,
        observed_location=result.observed_location,
        provenance_artifact_ids=list(result.provenance_artifact_ids),
        diagnostics=result.diagnostics,
        elapsed_ms=result.elapsed_ms,
    )
    if int(terminal_seq or 0) <= 0:
        return _outcome(
            VerificationFailure.LEASE_UNAVAILABLE.value,
            poc_id=poc_id,
            reproduction_id=reproduction_id,
            verification_id=verification_id,
            source_finding_id=source_finding_id,
            intent_id=intent_id,
            worker_id=worker_id,
            diagnostics="terminal verification already recorded",
            started_seq=started_seq,
        )

    review_seq: int | None = None
    if verified and source_finding_id:
        marker = getattr(graph, "mark_review_finding_verified", None)
        if callable(marker):
            try:
                review_seq = marker(
                    actor=worker_id,
                    finding_id=source_finding_id,
                    poc_id=poc_id,
                    reproduction_id=reproduction_id,
                    verification_id=verification_id,
                )
            except Exception:
                review_seq = None

    outcome = _outcome(
        "verified" if verified else failure_reason,
        poc_id=poc_id,
        reproduction_id=reproduction_id,
        verification_id=verification_id,
        source_finding_id=source_finding_id,
        intent_id=intent_id,
        worker_id=worker_id,
        verified=verified,
        exit_code=result.exit_code,
        observed_location=result.observed_location,
        provenance_artifact_ids=tuple(result.provenance_artifact_ids),
        failure_reason=failure_reason,
        diagnostics=result.diagnostics,
        elapsed_ms=result.elapsed_ms,
        started_seq=started_seq,
        terminal_seq=int(terminal_seq),
        review_finding_verified_seq=review_seq,
    )
    if callable(emit_delta):
        await _emit_terminal_delta(emit_delta, outcome)
    return outcome


def _field(container: Mapping[str, Any] | Any, key: str, default: Any = "") -> Any:
    if isinstance(container, Mapping):
        return container.get(key, default)
    return getattr(container, key, default)


def _positive_timeout(value: Any) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        timeout = 120.0
    if timeout <= 0:
        return 120.0
    return min(timeout, 600.0)


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pool_identity(lease: Any) -> str:
    pool_id = sanitize_public_text(getattr(lease, "pool_id", ""), limit=80)
    instance = sanitize_public_text(getattr(lease, "pool_instance_id", ""), limit=80)
    generation = _safe_int(getattr(lease, "generation", None))
    parts = [part for part in (pool_id, instance) if part]
    if generation is not None:
        parts.append(f"gen-{generation}")
    return "/".join(parts)


async def _release_if_needed(lease: Any) -> None:
    release = getattr(lease, "release", None)
    if callable(release):
        await release()


def _append_cancelled_terminal(*, graph: Any, actor: str, poc_id: str, verification_id: str) -> None:
    try:
        graph.append_poc_verification_terminal(
            actor=actor,
            poc_id=poc_id,
            verification_id=verification_id,
            verified=False,
            failure_reason=VerificationFailure.CANCELLED,
            diagnostics="cancelled",
        )
    except Exception:
        pass


def _outcome(
    status: str,
    *,
    poc_id: str,
    reproduction_id: str,
    verification_id: str,
    source_finding_id: str = "",
    intent_id: str = "",
    worker_id: str = "",
    verified: bool = False,
    exit_code: int | None = None,
    observed_location: str = "",
    provenance_artifact_ids: tuple[str, ...] = (),
    failure_reason: str = "",
    diagnostics: str = "",
    elapsed_ms: int | None = None,
    started_seq: int | None = None,
    terminal_seq: int | None = None,
    review_finding_verified_seq: int | None = None,
) -> VerificationOutcome:
    return VerificationOutcome(
        status=str(status or ""),
        poc_id=str(poc_id or ""),
        reproduction_id=str(reproduction_id or ""),
        verification_id=str(verification_id or ""),
        source_finding_id=str(source_finding_id or ""),
        intent_id=str(intent_id or ""),
        worker_id=str(worker_id or ""),
        verified=bool(verified),
        exit_code=exit_code,
        observed_location=sanitize_public_text(observed_location, limit=80),
        provenance_artifact_ids=tuple(
            sanitize_public_text(item, limit=160)
            for item in provenance_artifact_ids
            if str(item or "").strip()
        ),
        failure_reason=str(failure_reason or ""),
        diagnostics=sanitize_public_text(diagnostics),
        elapsed_ms=elapsed_ms,
        started_seq=started_seq,
        terminal_seq=terminal_seq,
        review_finding_verified_seq=review_finding_verified_seq,
    )


async def _emit_terminal_delta(emit_delta: Callable[..., Awaitable[None]], outcome: VerificationOutcome) -> None:
    fields: dict[str, Any] = {
        "poc_id": outcome.poc_id,
        "reproduction_id": outcome.reproduction_id,
        "verification_id": outcome.verification_id,
        "status": "verified" if outcome.verified else "failed",
        "terminal_seq": outcome.terminal_seq,
        "source_finding_id": outcome.source_finding_id,
        "intent_id": outcome.intent_id,
        "worker_id": outcome.worker_id,
        "exit_code": outcome.exit_code,
        "elapsed_ms": outcome.elapsed_ms,
    }
    if outcome.verified:
        fields["observed_location"] = outcome.observed_location
        fields["provenance_artifact_ids"] = list(outcome.provenance_artifact_ids)
        await emit_delta("poc_verified", **fields)
    else:
        fields["reason"] = outcome.failure_reason or outcome.status
        fields["diagnostics"] = outcome.diagnostics
        await emit_delta("poc_verification_failed", **fields)


__all__ = ["VerificationOutcome", "run_poc_verification"]
