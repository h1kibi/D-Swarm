"""Read-only, secret-free observability projection for one D-Swarm Run.

The projection deliberately sits beside the event/ledger substrate instead of
modifying it.  It gives the command deck one stable shape for answering three
operator questions:

* what was configured and frozen at launch;
* what the runtime is effectively using right now;
* how healthy and expensive each role has been.

No credential material or raw provider exception text crosses this boundary.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Mapping
from urllib.parse import urlsplit

from dswarm.core.events import Event, EventType

ROLE_KEYS = ("reason", "worker", "review", "observer", "titler")
_SAFE_BINDING_KEYS = {
    "id", "name", "label", "engine", "provider", "provider_ref", "model",
    "base_url", "wire_api", "auth_mode",
    "effort", "timeout", "credential_source", "credential_account",
}
_SECRET_PARTS = (
    "key", "token", "secret", "password", "authorization", "private",
)


def _safe_number(value: Any, default: float = 0.0) -> float:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _safe_int(value: Any, default: int = 0) -> int:
    return max(0, int(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _safe_text(value: Any, limit: int = 240) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _public_origin(value: Any) -> str:
    """Return only an endpoint origin; never forward path, query, or userinfo."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = urlsplit(value.strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme.lower()}://{host}{port}"
    except ValueError:
        return ""


def public_binding(value: Any) -> dict[str, Any]:
    """Allowlist the already-redacted launch binding once more at the API edge."""
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key)
        lowered = key.lower().replace("-", "_")
        if lowered not in _SAFE_BINDING_KEYS or any(part in lowered for part in _SECRET_PARTS):
            continue
        if lowered == "base_url":
            origin = _public_origin(raw_value)
            if origin:
                out[key] = origin
        elif isinstance(raw_value, (str, int, float, bool)) and raw_value not in ("", None):
            out[key] = raw_value
    return out


def _role_for_usage(*, operation_kind: Any = "", profile_id: Any = "", solver_id: Any = "") -> str:
    op = _safe_text(operation_kind, 80).lower().replace("-", "_")
    profile = _safe_text(profile_id, 80).lower().replace("-", "_")
    solver = _safe_text(solver_id, 80).lower().replace("-", "_")
    if (
        op in {"review", "revalidation", "poc_verifier"}
        or "review" in op
        or solver in {"review", "reviewer"}
        or "review" in profile
    ):
        return "review"
    if (
        op in {"observer", "btw", "ask_progress"}
        or "observer" in op
        or solver in {"observer", "observer_fallback"}
        or "observer" in profile
    ):
        return "observer"
    if (
        op in {"titler", "title"}
        or "titler" in op
        or solver in {"titler", "title"}
        or "titler" in profile
    ):
        return "titler"
    if solver in {"reason", "planner"} or profile in {"reason", "planner"} or op in {"reason", "planner", "recovery"}:
        return "reason"
    return "worker"


def _role_for_error(event: Event) -> str:
    payload = event.payload or {}
    phase = payload.get("phase") or payload.get("stage") or ""
    worker = payload.get("worker_id") or payload.get("worker") or event.solver_id or ""
    return _role_for_usage(operation_kind=phase, solver_id=worker)


def _empty_usage() -> dict[str, Any]:
    return {
        "calls": 0, "tokens": 0, "input_tokens": 0, "output_tokens": 0,
        "usd": 0.0, "unknown_calls": 0, "estimated_calls": 0,
        "last_call_at": None,
    }


def _add_usage(target: dict[str, Any], record: Any, timestamp: float | None) -> None:
    target["calls"] += 1
    input_tokens = getattr(record, "input_tokens", None)
    output_tokens = getattr(record, "output_tokens", None)
    if input_tokens is None or output_tokens is None or getattr(record, "usage_status", "") == "unknown":
        target["unknown_calls"] += 1
    if getattr(record, "usage_status", "") == "estimated":
        target["estimated_calls"] += 1
    target["input_tokens"] += _safe_int(input_tokens)
    target["output_tokens"] += _safe_int(output_tokens)
    target["tokens"] += _safe_int(input_tokens) + _safe_int(output_tokens)
    target["usd"] += _safe_number(getattr(record, "usd", None))
    if timestamp is not None:
        target["last_call_at"] = max(float(timestamp), float(target["last_call_at"] or 0.0))


def _error_projection(event: Event) -> dict[str, Any]:
    payload = event.payload or {}
    return {
        "kind": event.event_type.value,
        "role": _role_for_error(event),
        "category": _safe_text(payload.get("category"), 80) or "runtime_failure",
        "severity": _safe_text(payload.get("severity"), 24) or "warning",
        "retryable": bool(payload.get("retryable", False)),
        "provider": _safe_text(payload.get("provider"), 120),
        "message": _safe_text(payload.get("user_message"), 280),
        "suggested_action": _safe_text(payload.get("suggested_action"), 280),
        "ts": float(event.ts or 0.0),
    }


def project_run_observability(run: Any, *, now: float | None = None) -> dict[str, Any]:
    """Build a deterministic, secret-free projection from a live Run handle."""
    now = float(time.time() if now is None else now)
    try:
        events = list(run.store.read_events(run.run_id)) if getattr(run, "store", None) is not None else []
    except Exception:
        events = []

    event_ts: dict[str, float] = {}
    started_at: float | None = None
    finished_at: float | None = None
    errors: list[dict[str, Any]] = []
    degraded: list[dict[str, Any]] = []
    recovery: dict[str, list[dict[str, Any]]] = defaultdict(list)
    # ``configured``/``frozen`` remain the launch snapshot.  Only explicit
    # append-only recovery events may move ``effective`` to a fallback binding.
    # Never derive the effective provider from mutable current settings.
    effective_bindings: dict[str, dict[str, Any]] = {}
    effective_source_state: dict[str, str] = {}
    # Host-owned Reason health (docs/17 §5.4/§5.5). Built only from append-only
    # reason_health_changed / reason_blocked / reason_recovered deltas, so a
    # legacy Run with none of them reports `unknown` rather than a fabricated
    # healthy state.
    reason_health: dict[str, Any] = {}
    for event in events:
        ts = float(getattr(event, "ts", 0.0) or 0.0)
        if event.event_type is EventType.RUN_STARTED and started_at is None:
            started_at = ts
        elif event.event_type is EventType.RUN_FINISHED:
            finished_at = ts
        usage_id = (event.payload or {}).get("usage_id")
        if usage_id:
            event_ts[str(usage_id)] = ts
        if event.event_type in {EventType.PROVIDER_ERROR, EventType.PROVIDER_BATCH_ALERT}:
            errors.append(_error_projection(event))
        payload = event.payload or {}
        kind = str(payload.get("kind") or "")
        recovery_kind = kind or str(getattr(event.event_type, "value", ""))
        if recovery_kind in {
            "provider_recovery_directive", "provider_recovery_activated",
            "provider_recovery_blocked", "binding_effective_changed",
        }:
            role = _safe_text(payload.get("role"), 40) or "worker"
            from_binding = payload.get("from_binding") or payload.get("from")
            to_binding = payload.get("to_binding") or payload.get("to")
            entry = {
                "kind": recovery_kind,
                "seq": _safe_int(getattr(event, "seq", 0)),
                "attempt": _safe_int(payload.get("attempt")),
                "window_s": _safe_int(payload.get("window_s"), 60),
                "cooldown_s": _safe_int(payload.get("cooldown_s"), 60),
                "cooldown_until": _safe_number(payload.get("cooldown_until")),
                "from_binding": public_binding(from_binding),
                "to_binding": public_binding(to_binding),
                "probe": dict(payload.get("probe") or {}) if isinstance(payload.get("probe"), Mapping) else {},
                "blocked": bool(payload.get("blocked")),
                "blocked_reason": _safe_text(payload.get("blocked_reason"), 160),
                "reason": _safe_text(payload.get("reason"), 240),
                "category": _safe_text(payload.get("category"), 80),
                "active_workers": _safe_int(payload.get("active_workers")),
                "affected_workers": _safe_int(payload.get("affected_workers")),
                "ts": ts,
            }
            recovery[role].append(entry)
            if recovery_kind in {"provider_recovery_activated", "binding_effective_changed"}:
                if isinstance(to_binding, Mapping):
                    effective_bindings[role] = public_binding(to_binding)
                effective_source_state[role] = "fallback"
        if event.event_type is EventType.BLACKBOARD_DELTA and kind == "runtime_degraded":
            payload = event.payload or {}
            degraded.append({
                "role": _role_for_error(event),
                "engine": _safe_text(payload.get("engine"), 100),
                "reason": _safe_text(payload.get("reason"), 240),
                "ts": ts,
            })
        if event.event_type is EventType.BLACKBOARD_DELTA and kind == "reason_health_changed":
            reason_health = {
                "state": _safe_text(payload.get("health_state"), 40) or "unknown",
                "failures": _safe_int(payload.get("failures")),
                "max_failures": _safe_int(payload.get("max_failures")),
                "request_failures": _safe_int(payload.get("request_failures")),
                "request_limit": _safe_int(payload.get("request_limit"), 3),
                "retry_window_s": _safe_int(payload.get("retry_window_s"), 60),
                "operator_attention": bool(payload.get("operator_attention")),
                "immediate": bool(payload.get("immediate")),
                "fallback_active": bool(payload.get("fallback_active")),
                "blocked_reason": _safe_text(payload.get("blocked_reason"), 120),
                "ts": ts,
            }
        elif event.event_type is EventType.BLACKBOARD_DELTA and kind == "reason_blocked":
            # A Reviewer intent-quality block carries an intent id and is not a
            # run-level Reason block; only the role-level block changes health.
            blocked_intent = str(payload.get("intent_id") or "").strip()
            blocked_stage = str(payload.get("stage") or "").strip().lower()
            if not blocked_intent or blocked_stage == "reason":
                base = dict(reason_health or {})
                if base.get("state") != "waiting_for_operator":
                    base.update({
                        "state": "reason_blocked",
                        "operator_attention": True,
                        "blocked_reason": _safe_text(payload.get("reason"), 120) or "reason_blocked",
                        "ts": ts,
                    })
                    reason_health = base
        elif event.event_type is EventType.BLACKBOARD_DELTA and kind == "reason_recovered":
            base = dict(reason_health or {})
            base.update({
                "state": "healthy",
                "failures": 0,
                "operator_attention": False,
                "blocked_reason": "",
                "ts": ts,
            })
            reason_health = base

    if started_at is None:
        started_at = float(getattr(run, "updated_at", 0.0) or 0.0) or None
    if finished_at is None and bool(getattr(run, "finished", False)):
        finished_at = float(getattr(run, "updated_at", 0.0) or 0.0) or None
    elapsed_ms = 0
    if started_at is not None:
        elapsed_ms = max(0, round(((finished_at if finished_at is not None else now) - started_at) * 1000))

    usage_by_role = {role: _empty_usage() for role in ROLE_KEYS}
    records = getattr(getattr(run, "ledger", None), "records", {}) or {}
    for record in records.values():
        role = _role_for_usage(
            operation_kind=getattr(record, "operation_kind", ""),
            profile_id=getattr(record, "profile_id", ""),
            solver_id=getattr(record, "solver_id", ""),
        )
        _add_usage(usage_by_role[role], record, event_ts.get(str(getattr(record, "usage_id", ""))))

    snapshot = getattr(run, "binding_snapshot", None)
    snapshot = snapshot if isinstance(snapshot, Mapping) else {}
    snapshot_roles = snapshot.get("roles") if isinstance(snapshot.get("roles"), Mapping) else {}
    role_errors: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in errors + degraded:
        role_errors[str(item.get("role") or "worker")].append(item)
    role_sources = snapshot.get("role_sources") if isinstance(snapshot.get("role_sources"), Mapping) else {}
    effective_sources = snapshot.get("effective_sources") if isinstance(snapshot.get("effective_sources"), Mapping) else {}
    fallback_candidates = snapshot.get("fallback_candidates") if isinstance(snapshot.get("fallback_candidates"), Mapping) else {}
    fallback_state = snapshot.get("fallback") if isinstance(snapshot.get("fallback"), Mapping) else {}
    role_rows: list[dict[str, Any]] = []
    reason_state = str(reason_health.get("state") or "")
    reason_operator = bool(reason_health.get("operator_attention"))
    for role in ROLE_KEYS:
        binding = public_binding(snapshot_roles.get(role))
        role_issue = role_errors.get(role, [])
        role_recovery = recovery.get(role, [])
        blocked = next((item for item in reversed(role_recovery) if item.get("blocked")), None)
        activated = next((item for item in reversed(role_recovery) if item.get("kind") == "provider_recovery_activated"), None)
        fatal = any(str(item.get("severity")) == "fatal" for item in role_issue)
        if blocked or fatal:
            status = "blocked"
        elif activated:
            status = "fallback_active"
        elif role_issue:
            status = "degraded"
        elif usage_by_role[role]["calls"]:
            status = "active"
        elif binding:
            status = "ready"
        else:
            status = "unconfigured"
        # Reason's own health is host-owned and must not be masked by a stale
        # "active"/"ready" verdict from usage accounting (docs/17 §5.5).
        if role == "reason" and reason_state:
            if reason_state in {"circuit_open", "reason_blocked", "waiting_for_operator"}:
                status = "blocked"
            elif reason_state in {"degraded", "retrying"}:
                status = "degraded"
        source = str(effective_source_state.get(role) or effective_sources.get(role) or role_sources.get(role) or "")
        effective = effective_bindings.get(role) or binding
        role_rows.append({
            "role": role, "configured": dict(binding), "frozen": dict(binding), "effective": dict(effective),
            "configured_source": str(role_sources.get(role) or "settings_default"),
            "effective_source": source or ("frozen_snapshot" if binding else "unavailable"),
            "fallback_candidate": public_binding(fallback_candidates.get(role)) if isinstance(fallback_candidates.get(role), Mapping) else str(fallback_candidates.get(role) or ""),
            "fallback": dict(fallback_state.get(role) or {}) if isinstance(fallback_state.get(role), Mapping) else {},
            "recovery": role_recovery[-8:], "status": status,
            "operator_attention": bool(blocked) or (role == "reason" and reason_operator),
            "usage": usage_by_role[role], "issues": role_issue[-8:],
        })

    total = _empty_usage()
    for usage in usage_by_role.values():
        for key in ("calls", "tokens", "input_tokens", "output_tokens", "unknown_calls", "estimated_calls"):
            total[key] += int(usage[key])
        total["usd"] += float(usage["usd"])
        if usage["last_call_at"] is not None:
            total["last_call_at"] = max(float(total["last_call_at"] or 0.0), float(usage["last_call_at"]))

    if any(row["status"] == "blocked" for row in role_rows):
        overall = "blocked"
    elif errors or degraded or reason_state in {"degraded", "retrying"}:
        overall = "degraded"
    elif role_rows and any(row["status"] in {"active", "ready", "fallback_active"} for row in role_rows):
        overall = "healthy"
    else:
        overall = "unknown"

    # docs/17 §5.5: Run Health shows the failure count, the window, and when the
    # next retry is due. Only public codes cross this boundary — never provider
    # exception text or credentials.
    reason_public: dict[str, Any] = {
        "state": reason_state or "unknown",
        "failures": _safe_int(reason_health.get("failures")),
        "max_failures": _safe_int(reason_health.get("max_failures")),
        "request_failures": _safe_int(reason_health.get("request_failures")),
        "request_limit": _safe_int(reason_health.get("request_limit"), 3),
        "retry_window_s": _safe_int(reason_health.get("retry_window_s"), 60),
        "operator_attention": reason_operator,
        "immediate": bool(reason_health.get("immediate")),
        "fallback_active": bool(reason_health.get("fallback_active")),
        "blocked_reason": _safe_text(reason_health.get("blocked_reason"), 120),
        "ts": _safe_number(reason_health.get("ts")),
    }
    if reason_state in {"degraded", "retrying"} and reason_public["ts"]:
        reason_public["retry_not_before"] = (
            reason_public["ts"] + reason_public["retry_window_s"])

    return {
        "run_id": str(run.run_id),
        "generated_at": now,
        "lifecycle": {
            "status": str(run.status()),
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_ms": elapsed_ms,
            "updated_at": float(getattr(run, "updated_at", 0.0) or 0.0) or None,
        },
        "knowledge": {
            "inherited": [
                {
                    "fact_id": _safe_text(str(item.get("fact_id") or ""), 200) or f"fact:{_safe_text(str(item.get('source_run_id') or ''), 120)}:{_safe_text(str(item.get('fact_seq') or ''), 40)}",
                    "fact": _safe_text(item.get("fact") or item.get("fact_text"), 2000),
                    "provenance_status": "verified" if item.get("provenance_status") == "verified" else "context",
                    "source_run_id": _safe_text(str(item.get("source_run_id") or ""), 120),
                    "fact_seq": _safe_int(item.get("fact_seq"), 0),
                    "artifact_id": _safe_text(str(item.get("artifact_id") or ""), 200),
                }
                for item in (getattr(run, "inherited_verified_facts", []) or [])
                if isinstance(item, Mapping) and _safe_text(item.get("fact") or item.get("fact_text"), 2000)
            ],
            "source": "target_verified_facts",
            "read_only": True,
        },
        "binding": {
            "version": snapshot.get("version"),
            "frozen": bool(snapshot),
            "source": "role_binding_overlay" if snapshot else "unavailable",
            "roles": role_rows,
            "review_policy": public_binding(snapshot.get("review_policy")),
        },
        "usage": total,
        "health": {
            "status": overall,
            "issues": errors[-16:],
            "degraded": degraded[-16:],
            "reason": reason_public,
        },
    }


__all__ = ["ROLE_KEYS", "project_run_observability", "public_binding"]
