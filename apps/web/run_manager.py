"""RunManager — the web/TUI-facing handle to live solve runs.

The frontends are dumb subscribers (§3): they never call the solver core
directly. They ask the RunManager to start a run, then subscribe to that run's
EventBus and POST HITL commands which land in the run's HITL queue. This keeps
the event schema as the only contract between core and UI.

A "run" here is one challenge being solved (solo or by a swarm). Each gets its
own EventBus + SessionStore (durable replay) + an asyncio.Queue for inbound
human commands.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import secrets
import shutil
import tarfile
import time
from urllib.parse import urlsplit, urlunsplit
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from apps.web.provider_errors import ProviderErrorAggregator, classify_provider_error
from apps.web.run_meta import FolderStore, RunMetaStore
from apps.web.targets import TargetStore
from apps.web.worker_config import WorkerConfigStore, resolve_worker_backend
from dswarm.core.cost import CostController
from dswarm.core.event_bus import EventBus
from dswarm.core.events import Event, EventType, hitl_response_payload
from dswarm.core.runtime_env import is_web_container
from dswarm.core.session_store import SessionStore
from dswarm.core.storage import safe_run_storage_key
from dswarm.core.usage_journal import UsageContext, UsageJournal, UsageRecord, UsageWriter
from dswarm.core.usage_ledger import SpawnGuard, UsageLedger
from dswarm.swarm.budget import ProfileBudgetGate
from dswarm.solver.credential_accounts import (
    account_store_root,
    ensure_pi_account_from_env,
)
from dswarm.solver.container_pool import ContainerPoolManager
from dswarm.solver.runtime_policy import (
    RuntimePolicy,
    RuntimePolicyError,
    RuntimeSnapshot,
    build_runtime_policy,
)
from dswarm.solver.runtime_snapshot import RuntimeSnapshotBuilder, RuntimeSnapshotStore
from dswarm.solver.runtime_cleanup import RuntimeCleanupInspector, RuntimeCleanupResult
from dswarm.solver.worker_profiles import normalize_profile_roster, profile_uses_endpoint
from dswarm.swarm.poc_verification import sanitize_public_text

LOG = logging.getLogger(__name__)


class TargetDispatchMismatchError(ValueError):
    """A historical Run cannot be resumed under its current Target card."""

    code = "target_dispatch_mismatch"
    safe_detail = "historical dispatch binding does not match the current Target; create a new Target Run"

    def __init__(self, diagnostics: Sequence[str] = ()) -> None:
        self.diagnostics = tuple(str(item) for item in diagnostics if str(item))
        super().__init__(self.code)



_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|auth(?:orization)?|"
    r"bearer|secret|password)\s*['\"]?\s*[:=]\s*['\"]?([^\s,;}'\"]+)"
)
_SECRET_TOKEN_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{20,}|"
    r"gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,})(?![A-Za-z0-9_-])"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_URL_RE = re.compile(r"(?i)\bhttps?://[^\s<>\"']+")
_ABSOLUTE_PATH_RE = re.compile(r"(?:(?:[A-Za-z]:[\\/])|(?:^|\s)/)[^\s,;]+")


def _safe_runtime_detail(value: Any, *, limit: int = 500) -> str:
    """Bound and redact worker/provider diagnostics before public emission.

    Worker exceptions frequently include SDK request URLs, authorization headers,
    or host paths.  The event bus is also an SSE/public persistence boundary, so
    truncating the exception is not sufficient: remove those fields first while
    retaining useful non-secret context such as provider error codes and account
    identifiers.
    """
    text = sanitize_public_text(value, limit=max(int(limit) * 2, 500))
    text = _SECRET_ASSIGNMENT_RE.sub(r"\1=<redacted>", text)
    text = _SECRET_TOKEN_RE.sub("<redacted>", text)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    text = _URL_RE.sub("<url>", text)
    text = _ABSOLUTE_PATH_RE.sub("<path>", text)
    return sanitize_public_text(text, limit=limit)


def _selected_roster(engines: Any, worker_profiles: list[dict]) -> list[dict]:
    """Mirror drivers._selected_profiles: normalize the roster to profile dicts."""
    names = normalize_profile_roster(engines or [], worker_profiles)
    by_name = {
        str(p.get("name") or p.get("id")): p
        for p in worker_profiles
        if isinstance(p, dict)
    }
    return [by_name[n] for n in names if n in by_name]


def _binding_scalar(value: Any) -> Any:
    """Keep binding snapshots JSON-safe and free of credential material."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _safe_transport_url(value: Any) -> str:
    """Keep only a routable, non-secret provider endpoint in a Run snapshot.

    Query strings/fragments and URL userinfo are excluded because operators
    occasionally paste signed or key-bearing URLs into custom relay settings.
    The path is retained (for example ``/v1``) because it is part of routing,
    not credential material.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        # Accessing .port validates malformed/out-of-range ports. Rebuild the
        # authority from hostname/port instead of reusing parsed.netloc: the
        # latter includes URL userinfo and could persist a pasted credential.
        port = parsed.port
        hostname = parsed.hostname
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        safe_netloc = hostname if port is None else f"{hostname}:{port}"
        path = parsed.path.rstrip("/") or "/"
        return urlunsplit((parsed.scheme, safe_netloc, path, "", "")).rstrip("/")
    except ValueError:
        return ""


def _public_binding(value: Any, key: str = "") -> Any:
    """Redact a provider/profile binding without losing operator-visible shape."""
    lowered = key.lower().replace("-", "_")
    secret_words = ("api_key", "apikey", "access_token", "refresh_token", "secret", "password", "passwd", "authorization", "private_key")
    # Bare/qualified token keys are credential material too; do not mistake
    # descriptive fields such as ``token_budget`` for a secret.
    if (
        lowered == "token"
        or lowered.endswith("_token")
        or any(word in lowered for word in secret_words)
    ):
        return None
    if lowered in {"base_url", "endpoint", "url"}:
        safe_url = _safe_transport_url(value)
        return safe_url or None
    if isinstance(value, Mapping):
        out = {}
        for raw_key, raw_value in value.items():
            clean_value = _public_binding(raw_value, str(raw_key))
            if clean_value is not None:
                out[str(raw_key)] = clean_value
        return out
    if isinstance(value, (list, tuple)):
        return [_public_binding(item, key) for item in value]
    return _binding_scalar(value)


def _binding_fields(profile: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project one configured profile to the immutable, non-secret Run DTO."""
    if not isinstance(profile, Mapping):
        return {}
    keys = (
        "id", "name", "label", "engine", "transport", "runtime", "image",
        "provider_ref", "provider", "model", "base_url", "wire_api",
        "auth_mode", "auth_header", "auth_prefix", "effort",
        "credential_account",
    )
    out: dict[str, Any] = {}
    for key in keys:
        if profile.get(key) in (None, ""):
            continue
        value = _safe_transport_url(profile[key]) if key == "base_url" else _binding_scalar(profile[key])
        if value not in (None, ""):
            out[key] = value
    return out


def _role_binding(raw: Any, *, fallback: Mapping[str, Any] | None = None) -> dict[str, Any]:
    value = dict(raw) if isinstance(raw, Mapping) else {}
    if fallback:
        merged = dict(fallback)
        merged.update(value)
        value = merged
    allowed = (
        "provider", "provider_ref", "model", "base_url", "wire_api",
        "auth_mode", "auth_header", "auth_prefix", "effort", "timeout",
        "credential_source", "credential_account",
    )
    out: dict[str, Any] = {}
    for key in allowed:
        if key not in value or value[key] in (None, ""):
            continue
        clean = (
            _safe_transport_url(value[key])
            if key == "base_url"
            else _binding_scalar(value[key])
        )
        if clean not in (None, ""):
            out[key] = clean
    return out


def _provider_transport_descriptor(provider_ref: Any, providers: Any) -> dict[str, Any]:
    """Copy only non-secret transport metadata into a Run binding snapshot.

    A provider id alone is not enough to recover a Run after the settings entry
    is edited or deleted.  The endpoint/protocol are safe display metadata; the
    credential remains exclusively in the provider secret store and is resolved
    only when a new worker token is minted.
    """
    ref = str(provider_ref or "").strip()
    if not ref or not isinstance(providers, (list, tuple)):
        return {}
    provider = next((item for item in providers
                     if isinstance(item, Mapping) and str(item.get("id") or "").strip() == ref), None)
    if provider is None:
        return {}
    allowed = ("base_url", "wire_api", "auth_mode", "auth_header", "auth_prefix")
    out: dict[str, Any] = {}
    for key in allowed:
        if provider.get(key) in (None, ""):
            continue
        clean = _safe_transport_url(provider[key]) if key == "base_url" else _binding_scalar(provider[key])
        if clean not in (None, ""):
            out[key] = clean
    return out


def _freeze_role_transport(binding: dict[str, Any], providers: Any) -> dict[str, Any]:
    """Enrich a redacted role binding without ever resolving or copying a key."""
    if not isinstance(binding, dict):
        return {}
    ref = binding.get("provider_ref")
    transport = _provider_transport_descriptor(ref, providers)
    # Explicit role/profile values win, while registry metadata fills omitted
    # fields.  This makes restart deterministic without changing legacy inputs.
    return {**transport, **binding}
def _runtime_credential_mode(profile: Mapping[str, Any]) -> str:
    """Return the non-secret credential transport mode for a runtime profile."""
    if profile.get("base_url"):
        return "custom"
    if profile.get("api_key_ref"):
        return "direct"
    return "gateway"


def _runtime_worker_binding_fields(profile: Mapping[str, Any]) -> dict[str, Any]:
    binding = _binding_fields(profile)
    binding["runtime_credential_mode"] = _runtime_credential_mode(profile)
    return binding


def _runtime_provider_descriptors(
    profiles: Sequence[Mapping[str, Any]], providers: Any,
) -> list[dict[str, Any]]:
    refs = dict.fromkeys(
        str(profile.get("provider_ref") or "").strip()
        for profile in profiles
        if str(profile.get("provider_ref") or "").strip()
    )
    return [
        {"id": provider_id, **_provider_transport_descriptor(provider_id, providers)}
        for provider_id in refs
    ]


def build_binding_snapshot(body: Mapping[str, Any] | None, worker_config: Any) -> dict[str, Any]:
    """Freeze effective role/profile references for one Run without secrets.

    New launches use an explicit ``llm_mode`` plus ``llm_overrides`` contract.
    The old ``llm_profiles`` request shape remains readable for imported/older
    clients, but Titler is never a per-Run override and Observer is settings-only.
    """
    request = dict(body or {})
    challenge = request.get("challenge") if isinstance(request.get("challenge"), Mapping) else {}
    category = str(challenge.get("category") or "")
    try:
        config = worker_config.resolve(category) if category else worker_config.get()
    except Exception:
        config = worker_config.get() if worker_config is not None else {}
    config = config if isinstance(config, Mapping) else {}
    profiles = [p for p in (config.get("worker_profiles") or []) if isinstance(p, Mapping)]
    requested = request.get("engines")
    if not isinstance(requested, (list, tuple)):
        requested = [request.get("worker_profile_id")] if request.get("worker_profile_id") else []
    selected = _selected_roster(requested, [dict(p) for p in profiles])
    selected_names = {str(p.get("name") or p.get("id")) for p in selected}
    selected_profiles = [p for p in profiles if str(p.get("name") or p.get("id")) in selected_names]
    if not selected_profiles:
        selected_profiles = [p for p in profiles if p.get("enabled", True)][:1]
    worker = selected_profiles[0] if selected_profiles else {}

    configured_llm = config.get("llm_profiles") if isinstance(config.get("llm_profiles"), Mapping) else {}
    configured_llm = dict(configured_llm)
    mode_raw = request.get("llm_mode")
    explicit_contract = mode_raw is not None or isinstance(request.get("llm_overrides"), Mapping)
    mode = str(mode_raw or "settings_default").strip().lower()
    if mode not in {"settings_default", "per_run_override"}:
        mode = "settings_default"
    overrides = request.get("llm_overrides") if isinstance(request.get("llm_overrides"), Mapping) else {}
    # Compatibility: old callers sent all roles in llm_profiles. Treat those
    # values as overrides only when the new explicit mode is absent.
    if not explicit_contract:
        legacy = request.get("llm_profiles") if isinstance(request.get("llm_profiles"), Mapping) else {}
        overrides = dict(legacy)
        mode = "per_run_override" if overrides else "settings_default"
    else:
        overrides = dict(overrides)

    def _profile(key: str, *aliases: str) -> Mapping[str, Any]:
        for candidate in (key, *aliases):
            value = configured_llm.get(candidate)
            if isinstance(value, Mapping):
                return value
        return {}

    def _override(key: str, *aliases: str) -> Mapping[str, Any] | None:
        if mode != "per_run_override":
            return None
        for candidate in (key, *aliases):
            value = overrides.get(candidate)
            if isinstance(value, Mapping):
                return value
        return None

    def _effective(key: str, *aliases: str) -> tuple[dict[str, Any], str]:
        default = _profile(key, *aliases)
        override = _override(key, *aliases)
        if override is not None:
            return dict(override), "run_override"
        return dict(default), "settings_default"

    reason, reason_source = _effective("reason", "planner")
    worker_llm, worker_source = _effective("worker")
    # ``reviewer`` is the proposal spelling; ``review`` remains the persisted
    # spelling used by existing settings and historical Runs.
    review_llm, review_source = _effective("review", "reviewer")
    # Observer and Titler are settings-only and can never be Run overrides.
    observer_llm = dict(_profile("observer"))

    titler_llm = dict(_profile("titler"))
    observer_source = "settings_default"
    titler_source = "settings_default"

    review_policy = request.get("review_policy") if isinstance(request.get("review_policy"), Mapping) else config.get("review_policy") or {}
    review_ref = str(review_policy.get("engine") or "").strip() if isinstance(review_policy, Mapping) else ""
    review_profile = next(
        (
            p for p in profiles
            if review_ref and review_ref in {
                str(p.get("id") or ""), str(p.get("name") or ""), str(p.get("label") or "")
            }
        ),
        None,
    )
    if not review_profile and selected_profiles:
        review_profile = selected_profiles[0]
    worker_binding = _freeze_role_transport(
        _role_binding(worker_llm, fallback=_binding_fields(worker)), config.get("llm_providers"))
    review_binding = _freeze_role_transport(
        _role_binding(review_llm, fallback=_binding_fields(review_profile)), config.get("llm_providers"))
    reason_binding = _freeze_role_transport(
        _role_binding(reason), config.get("llm_providers"))
    observer_binding = _freeze_role_transport(
        _role_binding(observer_llm), config.get("llm_providers"))
    titler_binding = _freeze_role_transport(
        _role_binding(titler_llm), config.get("llm_providers"))
    snapshot: dict[str, Any] = {
        "version": 2 if explicit_contract else 1,
        "worker_profile": _binding_fields(worker),
        "worker_profiles": [_binding_fields(p) for p in selected_profiles],
        "runtime_worker_profiles": [
            _runtime_worker_binding_fields(p) for p in profiles
        ],
        "runtime_providers": _runtime_provider_descriptors(
            profiles, config.get("llm_providers")
        ),
        "roles": {
            "reason": reason_binding,
            "worker": worker_binding,
            "review": review_binding,
            "observer": observer_binding,
            "titler": titler_binding,
        },
        "review_policy": _public_binding(dict(review_policy)) if isinstance(review_policy, Mapping) else {},
    }
    # Keep settings-default transport bindings separate from configured/effective
    # role bindings. Observer's fallback candidate is the explicit lightweight
    # Observer binding, never Titler or Reason.
    default_worker = _binding_fields(worker)
    default_reason = _role_binding(_profile("reason", "planner"))
    default_review = _role_binding(_profile("review"), fallback=_binding_fields(review_profile))
    default_observer_fallback = _role_binding(_profile("observer_fallback"))
    defaults = {
        "reason": _freeze_role_transport(default_reason, config.get("llm_providers")),
        "worker": _freeze_role_transport(
            _role_binding(_profile("worker"), fallback=default_worker),
            config.get("llm_providers"),
        ),
        "review": _freeze_role_transport(default_review, config.get("llm_providers")),
        "observer": _freeze_role_transport(default_observer_fallback, config.get("llm_providers")),
    }
    snapshot.update({
        "llm_mode": mode,
        "role_sources": {
            "reason": reason_source,
            "worker": worker_source,
            "review": review_source,
            "observer": observer_source,
            "titler": titler_source,
        },
        "effective_sources": {
            "reason": reason_source,
            "worker": worker_source,
            "review": review_source,
            "observer": observer_source,
            "titler": titler_source,
        },
        "fallback_candidates": {
            role: dict(binding) for role, binding in defaults.items()
        },
        "fallback": defaults,
    })

    return snapshot
def merge_resolve_dispatch(
    saved: dict[str, Any] | None,
    body: dict[str, Any] | None,
    historical_challenge: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge a continuation request without losing an operator direction.

    Direction uses presence semantics: an omitted field preserves the saved
    operator choice, an explicit empty string resets to ``auto``, and a value
    replaces the saved choice. Other challenge fields retain the historical
    challenge before applying explicit request overrides.
    """
    saved_body = dict(saved or {})
    request_body = dict(body or {})
    saved_ch = saved_body.get("challenge")
    saved_ch = dict(saved_ch) if isinstance(saved_ch, dict) else {}
    historical = dict(historical_challenge or {})
    request_ch = request_body.get("challenge")
    request_ch = dict(request_ch) if isinstance(request_ch, dict) else {}
    challenge = {**saved_ch, **historical, **request_ch}
    # ``direction`` has three-state presence semantics rather than ordinary
    # historical merge semantics: an explicit request wins, otherwise retain
    # the saved operator choice, and only use the historical value when no
    # saved choice exists.  This prevents replay metadata from silently
    # changing the operator's selected route during /resolve.
    if "direction" in request_ch:
        challenge["direction"] = request_ch["direction"]
    elif "direction" in saved_ch:
        challenge["direction"] = saved_ch["direction"]
    elif "direction" in historical:
        challenge["direction"] = historical["direction"]
    else:
        challenge.pop("direction", None)
    merged = {**saved_body, **request_body, "challenge": challenge}
    # A Run is an execution snapshot, not a live view of global settings. Once
    # the first launch captured bindings, /resolve may update the operator
    # prompt/direction but must reuse the original worker/provider/runtime seats.
    if isinstance(saved_body.get("binding_snapshot"), dict):
        for key in ("engines", "worker_profile_id", "llm_profiles", "llm_mode",
                    "llm_overrides", "review_policy", "worker_backend",
                    "runtime_profiles"):
            if key in saved_body:
                merged[key] = saved_body[key]
    return merged


@dataclass
class Run:
    run_id: str
    bus: EventBus
    cost: CostController
    store: SessionStore
    usage_journal: UsageJournal | None = None
    ledger: UsageLedger | None = None
    spawn_guard: SpawnGuard | None = None
    budget_gate: ProfileBudgetGate = field(default_factory=ProfileBudgetGate)
    runtime_policy: RuntimePolicy | None = None
    runtime_snapshot: RuntimeSnapshot | None = None
    pool_manager: ContainerPoolManager | None = None
    hitl: "asyncio.Queue[dict[str, Any]]" = field(default_factory=asyncio.Queue)
    # operator worker commands (spawn/kill a specific engine) the scheduler drains
    worker_cmds: "asyncio.Queue[dict[str, Any]]" = field(default_factory=asyncio.Queue)
    task: Optional[asyncio.Task] = None
    # post-solve standby: a short-lived worker spun up to serve a HITL command when
    # the main run is no longer live (finished, or the server restarted). Serialized
    # — one at a time per run.
    standby_task: Optional[asyncio.Task] = None
    finished: bool = False
    flag: Optional[str] = None
    # multi-flag: every distinct flag the run collected (dedup, discovery order).
    # `flag` stays the first for back-compat. expected_flags drives the rail/UI
    # "collected N/total" + the solved-vs-collecting distinction.
    flags: list[str] = field(default_factory=list)
    expected_flags: int = 1
    # multi-flag MODE bit (collect vs single). Relayed on the synthetic RUN_FINISHED
    # so a reconnecting deck knows a collect run shouldn't read "solved" on flag #1.
    multi_flag: bool = False
    # ---- lightweight metadata for the thread rail (conversation-first deck) ----
    # The deck lists runs in a ChatGPT-style sidebar; it needs a name/category/
    # outcome per run without replaying the whole event stream. We sniff these off
    # the bus as a sink (the run stays a dumb event source — no extra contract).
    name: str = ""
    # True when `name` is a derived slug / auto title (replaceable by a better
    # rule/LLM title); False once the operator names it themselves.
    name_is_slug: bool = False
    category: str = ""
    started: bool = False
    solved: bool = False
    # Terminal runtime failures remain visible in the fleet rail as `failed`;
    # ordinary non-solved completion remains `finished`.
    failure_reason: Optional[str] = None
    paused: bool = False
    # a worker raised its hand (HITL_REQUEST: NEED_INPUT / target crashed / instance
    # expired / missing credential). True until the operator answers (HITL_RESPONSE)
    # or the run finishes. Surfaced on the summary so a poll of /api/runs catches it
    # — independent of `paused` (the swarm may keep running with one hand up).
    awaiting_help: bool = False
    help_text: str = ""
    reason_blocked: bool = False
    reason_blocked_reason: str = ""
    degraded: bool = False
    degraded_reason: str = ""
    fallback_active: bool = False
    fallback_reason: str = ""
    created_seq: int = 0
    updated_seq: int = 0  # bumped on every event — exposed as activity metadata
    updated_at: float = 0.0  # epoch seconds of the latest event, for rail "x ago"
    # operator-set rail metadata (persisted in RunMetaStore, injected by manager)
    pinned: bool = False
    pinned_at: Optional[float] = None
    archived: bool = False
    custom_name: Optional[str] = None
    # rail folder (None = top-level) + operator drag-order within its section
    folder_id: Optional[str] = None
    sort_order: Optional[int] = None
    # M2: signature of the last HITL command (target, action, text, url) — an
    # identical back-to-back resend is dropped instead of re-queued/re-emitted.
    _last_hitl_sig: Optional[tuple] = None
    # P4: scheduler state — True while the run waits for a concurrency slot
    # (started but not dispatched); queue_position is the 1-based FIFO position.
    # cancelled marks an operator-cancelled run (queued-cancel, or a live run
    # stopped via the cancel endpoint) — distinct from a plain finished run.
    queued: bool = False
    queue_position: Optional[int] = None
    cancelled: bool = False
    blackboard_token: str = ""
    # Safe, durable copy of the dispatch options used to create this run. A
    # finished run can be continued after a server restart without silently
    # falling back to a different global worker roster/backend. Secrets are
    # redacted before this is stored (see remember_dispatch).
    dispatch_body: dict[str, Any] = field(default_factory=dict)
    # Immutable, non-secret role/profile snapshot captured on first launch.
    binding_snapshot: dict[str, Any] = field(default_factory=dict)
    # Operator-visible integrity diagnostics for legacy/imported relationships.
    # These are derived metadata, never evidence and never an event-log rewrite.
    dispatch_diagnostics: list[str] = field(default_factory=list)
    # Target-centric metadata. These are relationship/context snapshots only;
    # execution truth remains in the run event log and provenance gate.
    target_id: Optional[str] = None
    target_name: str = ""
    target_category: str = ""
    direction_id: Optional[str] = None
    direction: Optional[str] = None
    # Explicit, read-only Target knowledge copied at Run creation. Each item
    # retains source provenance and is context only, never accepted evidence.
    inherited_verified_facts: list[dict[str, Any]] = field(default_factory=list)

    def merge_flags(self, flags: Any) -> None:
        """Accumulate flags from an event payload (dedup, keep order); keep the
        flag/flags[0] invariant. Accepts a list or a single string."""
        if isinstance(flags, str):
            flags = [flags]
        for f in (flags or []):
            if f and f not in self.flags:
                self.flags.append(f)
        if self.flags and not self.flag:
            self.flag = self.flags[0]

    def status(self) -> str:
        """Return the single lifecycle/health status exposed to the deck.

        Terminal outcomes always win over transient Reason/provider health. A
        live Run then exposes the narrowest operator action: Reason blocked,
        settings fallback active, generic degradation, pause, or running.
        """
        if self.cancelled:
            return "cancelled"
        if not self.started:
            return "failed" if self.finished and self.failure_reason == "runtime_failure" else "draft"
        if self.finished:
            if self.solved:
                return "solved"
            if self.failure_reason == "runtime_failure":
                return "failed"
            return "finished"
        if self.queued:
            return "paused" if self.paused else "queued"
        if self.reason_blocked:
            return "reason_blocked"
        if self.fallback_active:
            return "fallback_active"
        if self.degraded:
            return "degraded"
        return "paused" if self.paused else "running"

    def summary(self) -> dict[str, Any]:
        """The shape the deck's thread rail consumes (one row per run).

        ``binding_snapshot`` is persisted as a non-secret launch snapshot, but
        older sessions and test/import paths may still contain an accidental
        credential-shaped key. Apply the same public allowlist used by the
        observability endpoint at this final DTO boundary. This is defense in
        depth: a future API route must not leak a raw snapshot merely by
        calling ``run.summary()``.
        """
        binding = _public_binding_snapshot(self.binding_snapshot)
        return {
            "run_id": self.run_id,
            # custom_name (operator rename) wins; else the auto/challenge name.
            # Empty when neither is set — the rail renders its own placeholder, we
            # do NOT leak the bare run id as a display name.
            "name": self.custom_name or self.name,
            "category": self.category or "",
            "target_id": self.target_id,
            "target_name": self.target_name or None,
            "target_category": self.target_category or None,
            "direction_id": self.direction_id,
            "direction": self.direction,
            "inherited_verified_facts": list(self.inherited_verified_facts),
            "inherited_verified_fact_count": len(self.inherited_verified_facts),
            "binding_snapshot": binding or None,
            "dispatch_diagnostics": list(self.dispatch_diagnostics),
            "started": self.started,
            "finished": self.finished,
            "solved": self.solved,
            "failure_reason": self.failure_reason,
            "reason_blocked": self.reason_blocked,
            "reason_blocked_reason": self.reason_blocked_reason,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "fallback_active": self.fallback_active,
            "fallback_reason": self.fallback_reason,
            "paused": self.paused,
            "status": self.status(),
            "queued": self.queued,
            "queue_position": self.queue_position,
            "cancelled": self.cancelled,
            "flag": self.flag,
            "flags": list(self.flags),
            "expected_flags": self.expected_flags,
            "multi_flag": self.multi_flag,
            "pinned": self.pinned,
            "pinned_at": self.pinned_at,
            "archived": self.archived,
            "folder_id": self.folder_id,
            # operator drag-order if set, else creation order (rail sorts by this)
            "order": self.sort_order if self.sort_order is not None else self.created_seq,
            "updated": self.updated_seq,
            "updated_at": self.updated_at,
        }


def _public_binding_snapshot(value: Any) -> dict[str, Any]:
    """Return a secret-free projection of a Run binding snapshot.

    The snapshot shape is intentionally kept stable for the UI (roles, selected
    worker profiles, and review policy), while each leaf is passed through the
    observability allowlist. Unknown nested keys are dropped rather than
    recursively copied, so a legacy ``api_key``/token cannot escape through a
    new summary consumer.
    """
    if not isinstance(value, Mapping):
        return {}

    def leaf(raw: Any) -> dict[str, Any]:
        from apps.web.run_observability import public_binding
        return public_binding(raw)

    out: dict[str, Any] = {}
    version = value.get("version")
    if isinstance(version, (int, float)) and not isinstance(version, bool):
        out["version"] = int(version)
    mode = value.get("llm_mode")
    if mode in {"settings_default", "per_run_override"}:
        out["llm_mode"] = mode
    def source_map(raw: Any) -> dict[str, str]:
        if not isinstance(raw, Mapping):
            return {}
        return {
            str(role): str(source)
            for role, source in raw.items()
            if isinstance(source, str) and source in {"settings_default", "run_override", "fallback", "frozen_snapshot", "unavailable"}
        }

    for key in ("role_sources", "effective_sources"):
        projected = source_map(value.get(key))
        if projected:
            out[key] = projected
    raw_candidates = value.get("fallback_candidates")
    if isinstance(raw_candidates, Mapping):
        out["fallback_candidates"] = {
            str(role): (leaf(binding) if isinstance(binding, Mapping) else str(binding))
            for role, binding in raw_candidates.items()
            if isinstance(binding, Mapping) or isinstance(binding, str)
        }
    fallback = value.get("fallback")
    if isinstance(fallback, Mapping):
        out["fallback"] = {
            str(role): leaf(binding)
            for role, binding in fallback.items()
            if isinstance(binding, Mapping)
        }
    for key in ("worker_profile", "review_policy"):
        raw = value.get(key)
        if isinstance(raw, Mapping):
            out[key] = leaf(raw)
    raw_profiles = value.get("worker_profiles")
    if isinstance(raw_profiles, (list, tuple)):
        out["worker_profiles"] = [leaf(item) for item in raw_profiles if isinstance(item, Mapping)]
    raw_roles = value.get("roles")
    if isinstance(raw_roles, Mapping):
        out["roles"] = {
            str(role): leaf(binding)
            for role, binding in raw_roles.items()
            if isinstance(binding, Mapping)
        }
    return out


# A driver is any coroutine fn(run) that emits onto run.bus and returns.
Driver = Callable[[Run], Awaitable[None]]


def _apply_blackboard_meta(run: "Run", ev: Event) -> None:
    """Reflect scheduler BLACKBOARD_DELTA lifecycle into the rail/summary state so
    the deck shows mid-run progress, not just the terminal RUN_FINISHED. Two things
    the operator complained were invisible (run-11189):
      • flag_found — a flag landed mid-run (collect mode keeps going); merge it into
        run.flags NOW so the N/total counter ticks up instead of staying 0 until the
        run ends.
      • awaiting_operator / collect_idle — the swarm auto-paused waiting for the
        operator (NEED_INPUT). Flip run.paused so the rail shows "paused", not a
        spinner that looks like it's still churning. operator_resumed / a STOP clears
        it (RUN_FINISHED already clears paused on its own)."""
    if ev.event_type is not EventType.BLACKBOARD_DELTA:
        return
    payload = ev.payload or {}
    kind = payload.get("kind")
    if kind == "reason_blocked":
        # A blocked ordinary Intent at the Reviewer quality gate is not a
        # Reason circuit failure. Keep the Intent diagnostic visible, but only
        # pause the whole Run for a run-level Reason block.
        intent_id = str(payload.get("intent_id") or "").strip()
        stage = str(payload.get("stage") or "").strip().lower()
        if intent_id and stage != "reason":
            return
        run.reason_blocked = True
        run.reason_blocked_reason = str(payload.get("reason") or "Reason planner unavailable")[:240]
        run.paused = True
    elif kind in ("reason_recovered", "operator_resumed"):
        run.reason_blocked = False
        run.reason_blocked_reason = ""
        run.paused = False
    elif kind in ("runtime_degraded", "worker_backend_degraded", "engine_degraded"):
        # Runtime degradation is observational and never changes terminal
        # verdicts. Preserve the latest bounded public reason for the rail.
        run.degraded = True
        run.degraded_reason = str(payload.get("reason") or payload.get("backend") or kind)[:240]
    elif kind in ("provider_recovery_activated", "binding_effective_changed"):
        run.fallback_active = True
        run.fallback_reason = str(payload.get("reason") or payload.get("category") or "provider fallback active")[:240]
        run.degraded = False
        run.degraded_reason = ""
    elif kind == "provider_recovery_blocked":
        role = str(payload.get("role") or payload.get("binding_role") or payload.get("component") or "").strip().lower()
        reason = str(payload.get("blocked_reason") or payload.get("reason") or "provider recovery blocked")[:240]
        if role in {"reason", "planner"}:
            run.reason_blocked = True
            run.reason_blocked_reason = reason
            run.paused = True
        else:
            run.degraded = True
            run.degraded_reason = reason
    elif kind == "flag_found":
        run.merge_flags(payload.get("flag"))
    elif kind in ("awaiting_operator", "collect_idle"):
        run.paused = True
    elif kind == "operator_stopped":
        run.paused = False

class RunManager:
    def __init__(
        self,
        *,
        sessions_root: "str | Path | None" = None,
        runtime_snapshot_store: RuntimeSnapshotStore | None = None,
        runtime_snapshot_builder: RuntimeSnapshotBuilder | None = None,
        runtime_pool_manager_factory: Callable[..., ContainerPoolManager] | None = None,
        runtime_cleanup_inspector: RuntimeCleanupInspector | Any | None = None,
    ) -> None:
        # P2-v3: in the compose layout the sessions/ tree must live UNDER the
        # mirrored data root (DSWARM_HOST_DATA_ROOT bind-mounted into the web
        # container), so worker sibling containers — launched by the host daemon —
        # can bind-mount the same physical dir. DSWARM_SESSIONS_ROOT names it
        # (compose points it at <data root>/sessions). Default "sessions" (CWD-
        # relative) preserves the bare-host behaviour.
        if sessions_root is None:
            sessions_root = os.environ.get("DSWARM_SESSIONS_ROOT") or "sessions"
        self.sessions_root = Path(sessions_root)
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self.runtime_snapshot_store = (
            runtime_snapshot_store or RuntimeSnapshotStore(self.sessions_root)
        )
        # M9a production defaults: snapshot preflight proves worker-image
        # availability at dispatch time (fail-fast at POST /start, not a silent
        # dead run), and the pool manager uses the same composition as the TUI
        # (runtime_factory.build_pool_manager_for_run).
        self.runtime_snapshot_builder = (
            runtime_snapshot_builder or RuntimeSnapshotBuilder()
        )
        self.runtime_pool_manager_factory = (
            runtime_pool_manager_factory or self._default_runtime_pool_manager_factory
        )
        self.runtime_cleanup_inspector = runtime_cleanup_inspector
        self.runs: dict[str, Run] = {}
        self.provider_errors = ProviderErrorAggregator()
        self._seq = 0
        self.meta = RunMetaStore(root=self.sessions_root)
        self.target_store = TargetStore(root=self.sessions_root)
        # operator-created rail folders (id → name); runs reference one via meta.
        self.folders = FolderStore(root=self.sessions_root)
        # default worker-roster config (which engines launch per challenge); the
        # dispatch path falls back to this when a request doesn't say otherwise.
        self.worker_config = WorkerConfigStore(root=self.sessions_root)
        # pi-only convenience: mirror DEEPSEEK_API_KEY into pi-main so container
        # workers can mount it without forcing the operator to manage accounts.
        ensure_pi_account_from_env(self.sessions_root)
        # P4: FIFO run queue + global concurrency cap (default 5, 1..8). Every
        # manager start goes through it; the queue is in-memory (transient).
        from apps.web.run_scheduler import RunScheduler
        self.scheduler = RunScheduler(sessions_root=self.sessions_root)
        self._rehydrate()

    def _apply_meta(self, run: "Run") -> None:
        """Overlay persisted operator metadata (pin/archive/rename) onto a run."""
        m = self.meta.get(run.run_id)
        run.pinned = m["pinned"]
        run.pinned_at = m["pinned_at"]
        run.archived = m["archived"]
        run.custom_name = m["custom_name"]
        run.folder_id = m["folder_id"]
        run.sort_order = m["order"]

    def _rehydrate(self) -> None:
        """Re-populate the rail from durable JSONL on startup.

        Without this, a server restart drops every past conversation: self.runs
        starts empty so the rail shows nothing, AND _seq resets to 0 so the next
        "+ New solve" mints `run-0001` — colliding with a STALE run-0001.jsonl and
        replaying its old events under a "new" conversation. Hydrating both fixes
        history loss and the new-solve-shows-old-chat bug at once.

        We build lightweight Run handles (own bus + store) seeded with the
        persisted summary. The full event history is NOT loaded into memory here
        — the events SSE replays it from JSONL on demand. We only need the rail
        metadata + a correctly advanced _seq.
        """
        store = SessionStore(root=self.sessions_root)
        max_seq = 0
        # summaries() is newest-first; create() stamps created_seq in CALL order,
        # and the rail sorts by created_seq DESC — so feed oldest-first to keep the
        # newest conversation on top of the rail.
        target_by_run: dict[str, dict[str, Any]] = {}
        for target in self.target_store.list(include_archived=True):
            for target_run_id in target.get("run_ids") or []:
                target_by_run.setdefault(str(target_run_id), target)

        for s in reversed(store.summaries()):
            rid = s["run_id"]
            # A never-dispatched run is normally only an empty SSE stub and must
            # stay hidden after restart. Target-bound drafts are the deliberate
            # exception: the Target card owns their durable relationship, so they
            # must be rehydrated as selectable drafts instead of disappearing.
            target = target_by_run.get(rid)
            if not s.get("started") and target is None:
                m0 = re.match(r"run-(\d+)$", rid)
                if m0:
                    max_seq = max(max_seq, int(m0.group(1)))
                continue
            run = self.create(rid, _defer_runtime_manager=True)
            # `summary()` falls back name→run_id; treat that as "no real title" so
            # the rail renders its placeholder instead of leaking the bare id.
            run.name = "" if s.get("name") in (None, "", rid) else s["name"]
            run.category = s.get("category", "") or ""
            run.target_id = s.get("target_id") or None
            run.target_name = s.get("target_name", "") or ""
            run.target_category = s.get("target_category", "") or ""
            run.direction_id = s.get("direction_id") or None
            run.direction = s.get("direction") or None
            run.inherited_verified_facts = [item for item in (s.get("inherited_verified_facts") or []) if isinstance(item, dict)]
            if not run.inherited_verified_facts:
                saved_facts = run.dispatch_body.get("inherited_verified_facts") if isinstance(run.dispatch_body, dict) else []
                run.inherited_verified_facts = [item for item in (saved_facts or []) if isinstance(item, dict)]
            # Older event logs do not contain Target fields. Recover the
            # relationship from the durable Target card without touching the
            # append-only run log. This also restores Target-bound drafts.
            if run.target_id is None and target is not None:
                run.target_id = target.get("id")
                run.target_name = target.get("name", "") or ""
                run.target_category = target.get("category", "") or ""
            if target is not None:
                run.dispatch_diagnostics = list(dict.fromkeys(
                    [*run.dispatch_diagnostics, *self._target_dispatch_diagnostics(run)]
                ))
            run.started = bool(s.get("started"))
            # a rehydrated run has NO live task (the swarm coroutine died with the
            # previous server). So a started run is necessarily finished — even if
            # its on-disk summary says finished=False because it was killed mid-run
            # before emitting RUN_FINISHED (a "ghost run": the rail would otherwise
            # spin forever with no terminal event to settle it). Force-settle here.
            run.finished = bool(s.get("finished")) or run.started
            run.solved = bool(s.get("solved"))
            run.failure_reason = (
                str(s.get("reason"))
                if s.get("reason") == "runtime_failure"
                else None
            )
            run.reason_blocked = bool(s.get("reason_blocked"))
            run.reason_blocked_reason = str(s.get("reason_blocked_reason") or "")[:240]
            run.degraded = bool(s.get("degraded"))
            run.degraded_reason = str(s.get("degraded_reason") or "")[:240]
            run.fallback_active = bool(s.get("fallback_active"))
            run.fallback_reason = str(s.get("fallback_reason") or "")[:240]
            run.flag = s.get("flag")
            run.flags = list(s.get("flags") or ([run.flag] if run.flag else []))
            run.expected_flags = int(s.get("expected_flags") or 1)
            run.multi_flag = bool(s.get("multi_flag", False))
            # a rehydrated run is never live → it can't be paused or mid-run.
            run.paused = False
            # order persisted runs by recency of activity (newest gets the highest
            # created_seq, so the rail's reverse sort puts it on top). created_seq
            # is assigned by create() in call order; mirror it into updated_seq so
            # the "recent" section's recency sort matches on startup.
            run.updated_seq = run.created_seq
            run.updated_at = float(s.get("ts", 0.0) or 0.0)
            # overlay operator metadata (pin/archive/rename) from the side table.
            self._apply_meta(run)
            m = re.match(r"run-(\d+)$", rid)
            if m:
                max_seq = max(max_seq, int(m.group(1)))

        # Target cards can own a newly-created draft before its first event is
        # appended. There is no SessionStore summary to iterate in that case,
        # so reconstruct only those explicitly referenced handles. This keeps
        # ordinary abandoned SSE stubs hidden while preserving Target -> Run
        # relationships across a backend restart.
        for rid, target in target_by_run.items():
            if rid in self.runs:
                continue
            run = self.create(rid, _defer_runtime_manager=True)
            run.started = False
            run.finished = False
            run.target_id = target.get("id")
            run.target_name = target.get("name", "") or ""
            run.target_category = target.get("category", "") or ""
            if target is not None:
                run.dispatch_diagnostics = list(dict.fromkeys(
                    [*run.dispatch_diagnostics, *self._target_dispatch_diagnostics(run)]
                ))
            run.direction_id = target.get("primary_direction_id") or None
            primary = next(
                (item for item in target.get("directions") or []
                 if item.get("id") == run.direction_id),
                None,
            )
            run.direction = (primary or {}).get("label") or None
            m = re.match(r"run-(\d+)$", rid)
            if m:
                max_seq = max(max_seq, int(m.group(1)))

        # advance the id counter past every persisted run-NNNN so create_new()
        # never re-mints an id that already has history on disk.
        self._seq = max(self._seq, max_seq)

    def get(self, run_id: str) -> Optional[Run]:
        return self.runs.get(run_id)

    def list_runs(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        """Run summaries for the thread rail, newest first.

        Only STARTED runs are real conversations. A run handle also gets created
        lazily when a deck merely OPENS an SSE stream (so the stream is live the
        instant a run starts) — including for local draft ids that are never
        dispatched. Those empty stubs must not appear in the rail; the active
        draft is shown from the deck's own local state, not this list.

        Archived runs are hidden by default (the rail's "+ archived" view passes
        include_archived=True). Ordering: a RUNNING run always floats to the top
        (so the题 currently being solved is the first thing the operator sees —
        previously we sorted purely by created_seq, which buried a live run under
        already-finished ones when the manager rehydrated from disk in a different
        order than the eval ran). Within the running / non-running groups we sort
        by latest activity (updated_at), newest first, then created_seq as a tiebreak.
        """
        def _key(r: "Run"):
            running = r.status() == "running"
            return (1 if running else 0, r.updated_at or 0.0, r.created_seq)
        return [
            r.summary()
            for r in sorted(self.runs.values(), key=_key, reverse=True)
            if r.started and (include_archived or not r.archived)
        ]

    # ---- operator rail mutations (persisted in the meta side-table) ----------

    def set_pinned(self, run_id: str, pinned: bool, *, now: float) -> bool:
        run = self.runs.get(run_id)
        if run is None:
            return False
        m = self.meta.set_pinned(run_id, pinned, now=now)
        run.pinned, run.pinned_at = m["pinned"], m["pinned_at"]
        return True

    def set_archived(self, run_id: str, archived: bool, *,
                     now: Optional[float] = None) -> bool:
        run = self.runs.get(run_id)
        if run is None:
            return False
        m = self.meta.set_archived(run_id, archived,
                                   now=now if now is not None else time.time())
        run.archived, run.pinned, run.pinned_at = m["archived"], m["pinned"], m["pinned_at"]
        return True

    def rename(self, run_id: str, name: Optional[str]) -> bool:
        run = self.runs.get(run_id)
        if run is None:
            return False
        run.custom_name = self.meta.set_name(run_id, name)["custom_name"]
        return True

    def unique_title(self, run_id: str, title: str) -> str:
        """Disambiguate an auto-derived rail label against the other runs.

        The naming rule's whole point is telling challenges apart (`方向-标识`),
        so two runs deriving the same identifier (a re-solve of one target, two
        arena instances behind one hostname) must not render as identical rows:
        the later one becomes `…-2`, `…-3`, … Operator renames are NOT funneled
        through here — typing an exact duplicate by hand is the operator's call.
        """
        taken = {
            (r.custom_name or r.name or "")
            for rid, r in self.runs.items() if rid != run_id
        } - {""}
        if title not in taken:
            return title
        n = 2
        while f"{title}-{n}" in taken:
            n += 1
        return f"{title}-{n}"

    def set_folder(self, run_id: str, folder_id: Optional[str]) -> bool:
        run = self.runs.get(run_id)
        if run is None:
            return False
        run.folder_id = self.meta.set_folder(run_id, folder_id)["folder_id"]
        return True

    def set_order(self, run_id: str, order: Optional[int]) -> bool:
        run = self.runs.get(run_id)
        if run is None:
            return False
        run.sort_order = self.meta.set_order(run_id, order)["order"]
        return True

    # ---- rail folders (operator-created groupings) ---------------------------

    def list_folders(self) -> list[dict[str, Any]]:
        return self.folders.list()

    def create_folder(self, name: str) -> dict[str, Any]:
        return self.folders.create(name)

    def update_folder(self, fid: str, *, name: Optional[str] = None,
                      order: Optional[int] = None) -> bool:
        return self.folders.update(fid, name=name, order=order)

    def delete_folder(self, fid: str) -> bool:
        # unfile every run that was in this folder, then drop the folder itself.
        self.meta.clear_folder_for_all(fid)
        for run in self.runs.values():
            if run.folder_id == fid:
                run.folder_id = None
        return self.folders.delete(fid)

    async def delete(self, run_id: str) -> bool:
        """Hard-delete a run: cancel its task(s), drop the handle + JSONL + meta."""
        run = self.runs.pop(run_id, None)
        if run is None:
            # still scrub any orphaned on-disk artifacts / meta and stale Target links
            self.target_store.detach_run_everywhere(run_id)
            self._delete_artifacts(run_id)
            return False
        # P4: a queued run has no task to cancel — drop it from the FIFO first
        # (the queue entry's driver closure is released with it).
        self.scheduler.cancel(run_id)
        # Cancel BOTH the swarm task and any live standby worker, then AWAIT them to
        # actually unwind before we close the bus / delete artifacts. Cancelling
        # without awaiting was a use-after-free race: the cancelled coroutine could
        # still be writing to the bus or reading an upload while we closed/removed
        # them. A cancelled task re-raises CancelledError on await — return_exceptions
        # swallows it (and any other shutdown error) so delete never self-destructs.
        pending = [t for t in (run.task, run.standby_task)
                   if t is not None and not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if run.pool_manager is not None:
            try:
                await run.pool_manager.close()
            except Exception:
                LOG.warning("runtime pool close failed while deleting run %s", run_id, exc_info=True)
        await run.bus.close()
        # Remove the relationship from every Target card. This also repairs
        # legacy duplicate/stale links without touching the deleted Run log.
        self.target_store.detach_run_everywhere(run_id)
        self._drop_board_schema(run_id)
        self._delete_artifacts(run_id)
        return True

    def _drop_board_schema(self, run_id: str) -> None:
        dsn = os.environ.get("DSWARM_BOARD_DSN", "").strip()
        if not dsn:
            return
        try:
            from dswarm.swarm.postgres_board import PostgresBoard

            PostgresBoard(dsn, challenge_id=run_id).drop_schema()
        except Exception:  # noqa: BLE001 - cleanup failure must not break delete
            LOG.warning("board schema cleanup failed for run %s", run_id)

    def _delete_artifacts(self, run_id: str) -> None:
        self.meta.forget(run_id)
        safe = safe_run_storage_key(run_id)
        jsonl = self.sessions_root / f"{safe}.jsonl"
        try:
            jsonl.unlink(missing_ok=True)
        except OSError:
            pass
        # also drop the per-run upload dir (sessions/{safe}/) so deleting a
        # conversation doesn't orphan its uploaded challenge files on disk.
        shutil.rmtree(self.sessions_root / safe, ignore_errors=True)

    # ---- retention sweep: auto-archive idle runs, then delete stale ones -----

    def _last_activity(self, run: "Run") -> float:
        """Epoch seconds of a run's most recent event (its idle clock). 0.0 when
        unknown (no persisted events) → such a run is never auto-touched."""
        try:
            return float(run.store.summary(run.run_id).get("ts", 0.0) or 0.0)
        except Exception:
            return 0.0

    async def retention_sweep(self, *, now: float, archive_after_s: float,
                              delete_after_s: float) -> dict[str, list[str]]:
        """One retention pass: archive started runs idle > archive_after_s, and
        DELETE already-archived runs idle > delete_after_s. PINNED runs are never
        auto-touched; runs with an unknown idle clock (ts==0) are skipped. Returns
        {"archived": [...], "deleted": [...]} for logging/tests."""
        archived: list[str] = []
        deleted: list[str] = []
        for run in list(self.runs.values()):
            if not run.started or run.pinned:
                continue
            ts = self._last_activity(run)
            if ts <= 0:
                continue  # can't date it → leave it alone
            idle = now - ts
            meta = self.meta.get(run.run_id)
            if meta["archived"]:
                if idle > delete_after_s:
                    await self.delete(run.run_id)
                    deleted.append(run.run_id)
                    LOG.info("retention: deleted stale archived run %s (idle %.0fs)",
                             run.run_id, idle)
            elif idle > archive_after_s:
                self.set_archived(run.run_id, True, now=now)
                archived.append(run.run_id)
                LOG.info("retention: archived idle run %s (idle %.0fs)", run.run_id, idle)
        return {"archived": archived, "deleted": deleted}

    async def retention_loop(self, *, interval_s: float, archive_after_s: float,
                             delete_after_s: float) -> None:
        """Background task: run retention_sweep every interval_s until cancelled.
        Sleeps FIRST so startup isn't blocked and a short-lived test process never
        triggers a sweep. A sweep failure is logged and the loop continues."""
        while True:
            try:
                await asyncio.sleep(interval_s)
                await self.retention_sweep(
                    now=time.time(), archive_after_s=archive_after_s,
                    delete_after_s=delete_after_s)
            except asyncio.CancelledError:
                break
            except Exception:
                LOG.exception("retention sweep failed; continuing")

    @staticmethod
    def _redact_dispatch_value(value: Any, key: str = "") -> Any:
        """Return JSON-safe dispatch settings without copying credentials.

        Dispatch profiles identify credential accounts, but a caller must never
        put raw API keys/tokens/passwords into the run workspace. The account
        store remains the source of truth for secrets.
        """
        secret_words = (
            "api_key", "apikey", "access_token", "refresh_token", "secret",
            "password", "passwd", "authorization", "private_key",
        )
        lowered = key.lower().replace("-", "_")
        if any(word in lowered for word in secret_words):
            return None
        # Provider endpoints are operator configuration, not challenge input.
        # Strip query/fragment/userinfo before writing the dispatch sidecar so a
        # pasted signed relay URL cannot survive under a non-secret key such as
        # ``base_url``. Challenge target URLs use the separate ``target`` key and
        # are intentionally left untouched.
        if lowered in {"base_url", "endpoint_url", "api_base_url"}:
            return _safe_transport_url(value)
        if isinstance(value, dict):
            return {
                str(k): v
                for k, raw in value.items()
                if (v := RunManager._redact_dispatch_value(raw, str(k))) is not None
            }
        if isinstance(value, (list, tuple)):
            return [RunManager._redact_dispatch_value(v, key) for v in value]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)

    def remember_run_context(self, run_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        """Persist non-secret pre-launch Run context without freezing bindings.

        Target knowledge is selected before /start. Keeping it in the same
        atomic sidecar makes a target-bound draft restart-safe while leaving
        provider/model bindings unfrozen until the real launch.
        """
        run = self.runs.get(run_id) or self.create(run_id)
        previous = self._load_dispatch(run_id)
        safe = self._redact_dispatch_value(dict(values or {}))
        config = dict(previous) if isinstance(previous, dict) else {}
        if isinstance(safe, dict):
            config.update(safe)
        run.dispatch_body = config
        if "inherited_verified_facts" in config:
            run.inherited_verified_facts = [
                item for item in (config.get("inherited_verified_facts") or [])
                if isinstance(item, dict)
            ]
        path = self.workspace_dir(run_id) / ".dswarm_dispatch.json"
        try:
            import json
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            LOG.exception("could not persist run context for %s", run_id)
        return config

    def remember_dispatch(self, run_id: str, body: dict[str, Any] | None) -> dict[str, Any]:
        """Persist the non-secret dispatch settings needed by ``resolve``.

        This is deliberately a sidecar, not a graph event: it is operator
        configuration, not solver evidence. Writes are atomic so a process crash
        cannot leave a partially-written config that makes recovery ambiguous.
        """
        run = self.runs.get(run_id) or self.create(run_id)
        previous = self._load_dispatch(run_id)
        safe = self._redact_dispatch_value(dict(body or {}))
        config = safe if isinstance(safe, dict) else {}
        # Write once: continuations reuse the original binding even if globals changed.
        existing_binding = run.binding_snapshot or (
            previous.get("binding_snapshot")
            if isinstance(previous, dict)
            else None
        )
        binding = (
            existing_binding
            if isinstance(existing_binding, dict) and existing_binding
            else build_binding_snapshot(body, self.worker_config)
        )
        run.binding_snapshot = binding
        # Keep the original effective launch inputs alongside the snapshot. This
        # makes a later resolve deterministic even when this method is called
        # again with a UI form containing changed global/provider selections.
        if isinstance(existing_binding, dict) and existing_binding:
            for key in (
                "engines", "worker_profile_id", "worker_profiles",
                "llm_profiles", "llm_mode", "llm_overrides", "review_policy",
                "worker_backend", "runtime_profiles",
            ):
                if key in previous:
                    config[key] = previous[key]
        config["binding_snapshot"] = binding
        run.dispatch_body = config
        path = self.workspace_dir(run_id) / ".dswarm_dispatch.json"
        try:
            import json
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            LOG.exception("could not persist dispatch settings for %s", run_id)
        return config

    def _load_dispatch(self, run_id: str) -> dict[str, Any]:
        run = self.runs.get(run_id)
        if run is not None and run.dispatch_body:
            return dict(run.dispatch_body)
        path = self.sessions_root / safe_run_storage_key(run_id) / "workspace" / ".dswarm_dispatch.json"
        try:
            import json
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def dispatch_for(self, run_id: str) -> dict[str, Any]:
        """Public read of a run's remembered (redacted) dispatch body — the
        non-secret prompt/challenge settings retitle needs to re-derive a name."""
        return self._load_dispatch(run_id)

    def _target_dispatch_diagnostics(self, run: Run, dispatch: Mapping[str, Any] | None = None) -> tuple[str, ...]:
        """Compare immutable dispatch identity with the current Target card.

        This is metadata validation only. It never edits the sidecar or event log;
        a conflicting historical Run must be replaced with a new Target Run.
        """
        if not run.target_id:
            return ()
        target = self.target_store.get(run.target_id)
        if not isinstance(target, dict):
            return ()
        body = dispatch if isinstance(dispatch, Mapping) else self._load_dispatch(run.run_id)
        challenge = body.get("challenge") if isinstance(body, Mapping) else None
        if not isinstance(challenge, Mapping):
            return ()
        diagnostics: list[str] = []
        stored_target_id = str(
            challenge.get("target_id") or body.get("target_id") or ""
        ).strip()
        if stored_target_id and stored_target_id != str(run.target_id).strip():
            diagnostics.append("target_binding_mismatch")
        stored_category = str(challenge.get("category") or "").strip().casefold()
        target_category = str(target.get("category") or "").strip().casefold()
        if stored_category and target_category and stored_category != target_category:
            diagnostics.append("target_challenge_mismatch")
        stored_name = " ".join(str(challenge.get("name") or "").split()).casefold()
        target_name = " ".join(str(target.get("name") or "").split()).casefold()
        # The startup controller uses this fixed challenge name and intentionally
        # has no Target binding. Keep the generic mismatch code as the public
        # contract; the UI copy explains that it is not resumable.
        if stored_name and target_name and stored_name != target_name:
            diagnostics.append("target_challenge_mismatch")
        stored_address = str(challenge.get("target") or "").strip()
        target_address = str(target.get("target_address") or "").strip()
        if stored_address and target_address and stored_address != target_address:
            diagnostics.append("target_challenge_mismatch")
        return tuple(dict.fromkeys(diagnostics))

    def _reject_target_dispatch_mismatch(self, run: Run, dispatch: Mapping[str, Any] | None = None) -> None:
        diagnostics = tuple(dict.fromkeys(
            [*run.dispatch_diagnostics, *self._target_dispatch_diagnostics(run, dispatch)]
        ))
        if diagnostics:
            run.dispatch_diagnostics = list(diagnostics)
            raise TargetDispatchMismatchError(diagnostics)

    def workspace_dir(self, run_id: str) -> Path:
        """Per-run persistent workspace: sessions/{id}/workspace/.

        Replaces the old tempfile.mkdtemp root so sandbox, artifacts, and
        shared_graph.db survive process restarts. Same id-sanitization as
        uploads_dir / _delete_artifacts."""
        safe = safe_run_storage_key(run_id)
        d = self.sessions_root / safe / "workspace"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def open_workspace(self, run_id: str) -> bool:
        """Open the run's workspace dir in the host file manager (operator-local —
        the deck runs in a browser, so a backend opener is the only way to truly
        reveal Finder/Explorer). Best-effort; False if it can't open."""
        import subprocess
        import sys

        d = self.workspace_dir(run_id)  # created if missing; opening empty is fine
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(d))  # type: ignore[attr-defined]
                return True
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            if shutil.which(opener) is None:
                return False
            subprocess.Popen([opener, str(d)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    def uploads_dir(self, run_id: str) -> Path:
        """Per-run folder where uploaded challenge files land: sessions/{id}/uploads/.

        Each conversation gets its own directory so a file-based challenge's
        handouts stay scoped to that run (the worker later stages them into its
        cwd via CliSolver._stage_attachments). Sanitize the id with the same rule
        as _delete_artifacts so a hostile run_id can't escape sessions/. The dir
        is a sibling of the run's {id}.jsonl log — SessionStore only globs
        *.jsonl, so a directory of the same stem never collides with rehydration.
        """
        safe = safe_run_storage_key(run_id)
        d = self.sessions_root / safe / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def configure_budget(self, run_id: str, body: dict[str, Any] | None) -> None:
        """Apply explicit run budget settings without replacing usage projections."""
        run = self.runs.get(run_id)
        if run is None:
            return
        body = dict(body or {})
        nested = body.get("budget") if isinstance(body.get("budget"), dict) else {}
        profile_caps = body.get("profile_budget_caps", nested.get("profile_budget_caps"))
        account_caps = body.get("account_budget_caps", nested.get("account_budget_caps"))
        warn_ratio = body.get("budget_warn_ratio", nested.get("warn_ratio"))
        if isinstance(profile_caps, dict):
            run.budget_gate.profile_caps = {
                str(key): float(value) for key, value in profile_caps.items()
                if value is not None and float(value) >= 0
            }
        if isinstance(account_caps, dict):
            run.budget_gate.account_caps = {
                str(key): float(value) for key, value in account_caps.items()
                if value is not None and float(value) >= 0
            }
        if warn_ratio is not None:
            run.budget_gate.warn_ratio = min(1.0, max(0.0, float(warn_ratio)))
        # Re-apply durable projection records after cap configuration. The gate is
        # deliberately not recreated, so idempotent/action state remains intact.
        if run.ledger is not None:
            run.budget_gate.rebuild(
                run.ledger.records.values(), run.ledger.budget_actions
            )

    @staticmethod
    def _budget_alert_payload(run: Run, alert: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": "budget_threshold",
            "run_id": run.run_id,
            **dict(alert),
        }

    async def _emit_budget_alert(self, run: Run, alert: dict[str, Any]) -> None:
        try:
            await run.bus.emit(Event(
                event_type=EventType.BUDGET_ALERT,
                run_id=run.run_id,
                payload=self._budget_alert_payload(run, alert),
            ))
        except Exception:
            LOG.debug("failed to emit budget alert for %s", run.run_id, exc_info=True)

    def _apply_accounting_event(self, run: Run, event: Event) -> tuple[dict[str, Any], ...]:
        """Fold canonical accounting events into the ledger and budget gate.

        The ledger remains the idempotency authority.  A duplicate canonical usage
        event therefore cannot double-charge the profile/account budget projection.
        Alert emission is deferred by the caller because this method runs inside a
        bus sink and must never recursively await ``bus.emit``.
        """
        if run.ledger is None:
            return ()
        if event.event_type is EventType.USAGE_RECORDED:
            accepted = run.ledger.apply_event(event)
            if not accepted:
                return ()
            record = UsageRecord(**dict(event.payload or {}))
            verdict = run.budget_gate.apply(record)
            return verdict.alerts
        if event.event_type is EventType.BUDGET_ACTION:
            run.ledger.apply_event(event)
            run.budget_gate.apply_action(dict(event.payload or {}))
        return ()

    def _schedule_budget_alerts(self, run: Run, alerts: tuple[dict[str, Any], ...]) -> None:
        for alert in alerts:
            task = asyncio.create_task(self._emit_budget_alert(run, alert))
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    def ensure_runtime_context(
        self,
        run_id: str,
        *,
        policy: RuntimePolicy,
        worker_profiles: Sequence[Mapping[str, Any]],
        runtime_profiles: Sequence[Mapping[str, Any]],
        run_max_workers: int,
        snapshot_builder: RuntimeSnapshotBuilder | None = None,
        pool_manager_factory: Callable[..., ContainerPoolManager] | None = None,
    ) -> tuple[RuntimePolicy, RuntimeSnapshot | None, ContainerPoolManager | None]:
        """Build or load exactly one immutable runtime context for ``run_id``."""
        if not isinstance(policy, RuntimePolicy):
            raise RuntimePolicyError("invalid_runtime_policy")
        run = self.create(run_id)
        if run.runtime_policy is not None:
            if run.runtime_snapshot is not None and run.pool_manager is None:
                factory = pool_manager_factory or self.runtime_pool_manager_factory
                if factory is not None:
                    run.pool_manager = factory(run_id=run_id, snapshot=run.runtime_snapshot)
            return run.runtime_policy, run.runtime_snapshot, run.pool_manager

        if policy.mode == "local_dev":
            if not policy.local_workers_allowed:
                raise RuntimePolicyError("local_worker_policy_denied")
            run.runtime_policy = policy
            return policy, None, None

        builder = snapshot_builder or self.runtime_snapshot_builder
        if builder is None:
            raise RuntimePolicyError("runtime_snapshot_builder_required")
        snapshot = builder.build(
            run_id=run_id,
            policy=policy,
            worker_profiles=worker_profiles,
            runtime_profiles=runtime_profiles,
            run_max_workers=run_max_workers,
        )
        if snapshot.run_id != run_id:
            raise RuntimePolicyError("runtime_snapshot_run_mismatch")
        if snapshot.runtime_policy != policy:
            raise RuntimePolicyError("runtime_policy_snapshot_mismatch")
        self.runtime_snapshot_store.create(snapshot)
        run.runtime_policy = snapshot.runtime_policy
        run.runtime_snapshot = snapshot
        factory = pool_manager_factory or self.runtime_pool_manager_factory
        if factory is not None:
            run.pool_manager = factory(run_id=run_id, snapshot=snapshot)
        return run.runtime_policy, run.runtime_snapshot, run.pool_manager

    async def _cleanup_before_reopen(self, run: Run) -> RuntimeCleanupResult | None:
        """Prove every stale private runtime is gone before a reopened dispatch."""
        inspector = self.runtime_cleanup_inspector
        if inspector is None:
            return None
        method = getattr(inspector, "cleanup_run_before_reopen", None)
        if not callable(method):
            raise RuntimeError("stale_runtime_cleanup_unproven")
        safe = run.run_id.replace("/", "_").replace("..", "_")
        run_root = self.sessions_root / safe
        result = method(run.run_id, run_root)
        if inspect.isawaitable(result):
            result = await result
        if not getattr(result, "proven", False):
            raise RuntimeError("stale_runtime_cleanup_unproven")
        return result

    def _ensure_runtime_pool_manager(self, run: Run) -> ContainerPoolManager | None:
        """Lazily construct the per-run pool after any reopen barrier."""
        if run.pool_manager is not None or run.runtime_snapshot is None:
            return run.pool_manager
        factory = self.runtime_pool_manager_factory
        if factory is None:
            return None
        run.pool_manager = factory(run_id=run.run_id, snapshot=run.runtime_snapshot)
        return run.pool_manager

    def create(self, run_id: str, *, _defer_runtime_manager: bool = False) -> Run:
        if run_id in self.runs:
            return self.runs[run_id]
        runtime_snapshot = None
        runtime_policy = None
        pool_manager = None
        snapshot_path = self.runtime_snapshot_store.path_for(run_id)
        if snapshot_path.is_file():
            runtime_snapshot = self.runtime_snapshot_store.load(run_id)
            runtime_policy = runtime_snapshot.runtime_policy
            if self.runtime_pool_manager_factory is not None and not _defer_runtime_manager:
                pool_manager = self.runtime_pool_manager_factory(
                    run_id=run_id, snapshot=runtime_snapshot
                )
        bus = EventBus()
        store = SessionStore(root=self.sessions_root)
        self._sync_bus_seq(bus, store=store, run_id=run_id)
        bus.add_critical_sink(store.sink, store.append_checked)
        self._seq += 1
        journal = UsageJournal(
            self.sessions_root / f"{safe_run_storage_key(run_id)}-usage-journal.jsonl"
        )
        ledger = UsageLedger(run_id=run_id)
        # Startup projection rebuild is synchronous and happens before a handle is
        # exposed to any spawn path. A failed replay leaves the guard failed so
        # stop/finalize remain available while new provider calls are rejected.
        try:
            ledger.rebuild(store.read_events(run_id), journal=journal)
        except Exception as exc:
            ledger.mark_failed(str(exc))
        guard = SpawnGuard()
        if ledger.state == "failed":
            guard.mark_failed(ledger.ledger_error or "ledger_rebuild_failed")
        elif ledger.pending_recovery_records():
            # Journal recovery is projected in memory first, then canonicalized
            # through the checked event path before any provider/spawn operation.
            guard.mark_rebuilding()
        run = Run(
            run_id=run_id, bus=bus, cost=CostController(bus=bus), store=store,
            usage_journal=journal, ledger=ledger, spawn_guard=guard,
            budget_gate=ProfileBudgetGate(),
            runtime_policy=runtime_policy,
            runtime_snapshot=runtime_snapshot,
            pool_manager=pool_manager,
            created_seq=self._seq,
            blackboard_token=secrets.token_urlsafe(32),
        )
        self._configure_gateway_bridge(run)
        # Restore the non-secret dispatch sidecar when this handle is created
        # after a backend restart. The helper is intentionally best-effort: old
        # runs simply have no sidecar and resolve falls back to their challenge.
        run.dispatch_body = self._load_dispatch(run_id)
        run.inherited_verified_facts = [item for item in (run.dispatch_body.get("inherited_verified_facts") or []) if isinstance(item, dict)]
        saved_binding = run.dispatch_body.get("binding_snapshot")
        if isinstance(saved_binding, dict):
            run.binding_snapshot = saved_binding
        # sniff run.started / run.finished off the bus to keep rail metadata fresh
        # without making the run anything but a dumb event source.
        async def _meta_sink(ev: Event) -> None:
            # any event = activity. Keep this as metadata only: the rail itself is
            # creation-ordered, otherwise concurrent live runs visually hop around.
            self._seq += 1
            run.updated_seq = self._seq
            run.updated_at = ev.ts
            try:
                alerts = self._apply_accounting_event(run, ev)
                self._schedule_budget_alerts(run, alerts)
            except Exception as exc:
                if run.ledger is not None:
                    run.ledger.mark_failed(str(exc))
                if run.spawn_guard is not None:
                    run.spawn_guard.mark_failed(str(exc))
            if ev.event_type is EventType.RUN_STARTED:
                ch = ev.payload.get("challenge", {}) or {}
                run.started = True
                run.failure_reason = None
                # The derived slug is a PRELIMINARY display name (marked
                # name_is_slug at /start): the rule/LLM title (RUN_TITLED)
                # replaces it. An operator-supplied name is sticky.
                if ch.get("name") and not run.name:
                    run.name = ch["name"]
                run.category = ch.get("category", run.category) or run.category
                if ch.get("expected_flags"):
                    run.expected_flags = int(ch["expected_flags"])
                if "multi_flag" in ch:
                    run.multi_flag = bool(ch["multi_flag"])
            elif ev.event_type is EventType.RUN_TITLED:
                # auto-title landed from the background summarizer; adopt over a
                # derived slug (name_is_slug) but never over an operator name or
                # an operator rename (custom_name wins at display anyway). The
                # title is disambiguated against the fleet so two runs on the
                # same target don't collapse into identical rows.
                title = ev.payload.get("title") or ""
                if title and (not run.name or run.name_is_slug):
                    run.name = self.unique_title(run.run_id, title)
            elif ev.event_type is EventType.HITL_RESPONSE:
                # Reflect pause/resume and clear an operator hand after response.
                action = ev.payload.get("action")
                if action == "pause":
                    run.paused = True
                elif action == "resume":
                    run.paused = False
                run.awaiting_help = False
                run.help_text = ""
            elif ev.event_type is EventType.RUN_REOPENED:
                # Resolve/continue reopens the planner without retaining blocked state.
                run.reason_blocked = False
                run.reason_blocked_reason = ""
                run.finished = False
                run.failure_reason = None
                run.solved = False
                run.paused = False
                if ev.payload.get("reason") == "resolve":
                    return
                bad = ev.payload.get("flag")
                if bad and run.flags:
                    run.flags = [f for f in run.flags if f != bad]
                    run.flag = run.flags[0] if run.flags else None
                else:
                    run.flag = None
                    run.flags = []
            elif ev.event_type is EventType.HITL_REQUEST:
                # a worker raised its hand (NEED_INPUT / env_down: target crashed,
                # instance expired, missing credential…). Surface it on the summary so
                # an operator (or a 1-min poll of /api/runs) sees it WITHOUT scanning
                # JSONL — this does NOT require the run to be "paused" (the swarm may
                # still be hurling workers at the wall while one hand is up).
                run.awaiting_help = True
                run.help_text = str((ev.payload or {}).get("need")
                                     or (ev.payload or {}).get("text") or "")[:300]
            elif ev.event_type is EventType.RUN_FINISHED:
                run.finished = True
                run.failure_reason = (
                    "runtime_failure"
                    if ev.payload.get("reason") == "runtime_failure"
                    else None
                )
                run.paused = False  # a finished run is never "paused"
                run.awaiting_help = False  # finished → no outstanding ask
                run.help_text = ""
                run.solved = bool(ev.payload.get("solved")) or run.solved
                run.merge_flags(ev.payload.get("flags") or ev.payload.get("flag"))
                if ev.payload.get("expected_flags"):
                    run.expected_flags = int(ev.payload["expected_flags"])
                if "multi_flag" in ev.payload:
                    run.multi_flag = bool(ev.payload["multi_flag"])
                if ev.payload.get("reason") == "cancelled":
                    run.cancelled = True
            elif ev.event_type is EventType.RUN_QUEUED:
                # P4: waiting for a scheduler slot — rail shows "queued (N)".
                run.queued = True
                run.queue_position = ev.payload.get("position")
                run.cancelled = False
            elif ev.event_type is EventType.RUN_DISPATCHED:
                # P4: the slot freed — the driver launches (RUN_STARTED follows).
                run.queued = False
                run.queue_position = None
            elif ev.event_type is EventType.RUN_CANCELLED:
                # P4: operator cancelled a queued run (RUN_FINISHED follows with
                # reason=cancelled — this is just the explicit marker).
                run.queued = False
                run.queue_position = None
                run.cancelled = True
            else:
                _apply_blackboard_meta(run, ev)
            # Target activity is a bounded UI index. Raw execution events remain
            # authoritative in the Run SessionStore and are never copied here.
            if run.target_id:
                try:
                    self.target_store.record_run_event(
                        run.target_id, run.run_id, ev.event_type, source_seq=ev.seq,
                        timestamp=ev.ts, status=run.status(), solved=run.solved,
                        reason=str((ev.payload or {}).get("reason") or ""),
                    )
                except Exception as exc:
                    LOG.debug("target activity index failed for %s (%s)", run.run_id, type(exc).__name__)

        bus.add_sink(_meta_sink)
        self.runs[run_id] = run
        # Restore caps/actions only after the Run is registered; configure_budget
        # resolves the live handle from self.runs during restart rehydration.
        self.configure_budget(run_id, run.dispatch_body)
        self._apply_meta(run)
        return run

    def _configure_gateway_bridge(self, run: Run) -> None:
        """Attach the process-wide gateway to this run's owner loop and bus.

        The HTTP gateway serves multiple runs from worker threads, while each run
        owns its own asyncio EventBus. Registration is keyed by ``run_id`` and is
        repeated when a closed bus is rebuilt. Synchronous maintenance callers do
        not have an owner loop yet, so registration is deferred for that path.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        from dswarm.solver.modelgateway import ModelGateway
        from dswarm.solver.credential_accounts import account_store_root

        gateway = ModelGateway.instance()
        gateway.account_root = str(account_store_root(self.sessions_root))
        gateway.sessions_root = str(self.sessions_root)
        gateway.upstream_key = self._resolve_worker_provider_key()
        gateway.upstream_binding_resolver = self._resolve_worker_provider_binding
        gateway.configure_usage_bridge(bus=run.bus, loop=loop, run_id=run.run_id)

    def install_gateway_key_resolver(self) -> None:
        """Register THIS manager's provider-key resolution on the gateway CLASS
        — without instantiating the gateway (starting the server at app boot
        broke the BTW lease guards and changed lazy-start behavior). The class
        resolver is re-read on every gateway request, covering the startup-test
        manager (which never runs the per-run wiring) and settings edits."""
        from dswarm.solver.modelgateway import ModelGateway

        ModelGateway.set_upstream_key_resolver(self._resolve_worker_provider_key)
        ModelGateway.set_upstream_binding_resolver(self._resolve_worker_provider_binding)

    def _resolve_worker_provider_binding(self, provider_ref: str):
        """Resolve one provider into the gateway's host-private token binding.

        The returned object is intentionally never a public DTO. It lets a strict
        container worker use its selected native transport (including Anthropic
        Messages) while only receiving a revocable gateway task token.
        """
        ref = str(provider_ref or "").strip()
        if not ref:
            return None
        try:
            cfg = self.worker_config.get()
            providers = cfg.get("llm_providers") or []
            from dswarm.solver.llm_providers import LLMProviderSecretStore, provider_secret_root, resolve_llm_provider
            from dswarm.solver.modelgateway import GatewayUpstreamBinding

            resolved = resolve_llm_provider(
                ref, providers,
                secret_store=LLMProviderSecretStore(provider_secret_root(self.sessions_root)),
            )
            if resolved is None or not resolved.base_url or not resolved.api_key:
                return None
            return GatewayUpstreamBinding(
                base_url=resolved.base_url,
                api_key=resolved.api_key,
                wire_api=resolved.wire_api,
                auth_mode=resolved.auth_mode,
                auth_header=resolved.auth_header,
                auth_prefix=resolved.auth_prefix,
                provider_id=resolved.provider_id,
            )
        except Exception:
            # This is called in a preflight/token path. Do not expose secrets,
            # endpoints, or provider library exceptions to a worker.
            return None

    def _resolve_worker_provider_key(self) -> str:
        """The gateway's upstream key: the secret of the provider the ENABLED
        worker profiles actually bind to (first present wins).

        Workers authenticate to the gateway with task tokens; the gateway must
        forward with the SAME provider key those bindings resolve to. It used
        to hardcode the pi-main credential account file — when the operator
        re-binds workers to a provider (e.g. zhipu) whose key lives in the
        provider secret store, every worker call 401-ed at the upstream while
        host-side probes (which resolve bindings correctly) stayed green."""
        try:
            cfg = self.worker_config.get()
        except Exception:
            return ""
        refs: list[str] = []
        for p in cfg.get("worker_profiles", []) or []:
            if not isinstance(p, dict) or not p.get("enabled"):
                continue
            ref = str(p.get("provider_ref") or "").strip()
            if ref and ref not in refs:
                refs.append(ref)
        if not refs:
            return ""
        from dswarm.solver.llm_providers import LLMProviderSecretStore, provider_secret_root
        store = LLMProviderSecretStore(provider_secret_root(self.sessions_root))
        for ref in refs:
            val = store.read_secret(ref)
            if val:
                return val
        return ""

    def internal_usage_writer(
        self,
        run: Run,
        *,
        solver_id: str | None = None,
        profile_id: str | None = None,
        configured_account_id: str | None = None,
        worker_instance_id: str | None = None,
    ) -> UsageWriter:
        """Create an internal-producer writer sharing this run's journal."""
        journal = getattr(run, "usage_journal", None) or UsageJournal(
            self.sessions_root
            / f"{safe_run_storage_key(run.run_id)}-usage-journal.jsonl"
        )
        run.usage_journal = journal
        return UsageWriter(
            journal,
            bus=run.bus,
            context=UsageContext(
                run_id=run.run_id,
                challenge_id=run.run_id,
                worker_instance_id=worker_instance_id,
                solver_id=solver_id,
                profile_id=profile_id,
                configured_account_id=configured_account_id,
                billing_account_id=None,
                producer="internal",
            ),
        )

    def fallback_usage_writer(
        self,
        run: Run,
        *,
        solver_id: str | None = None,
        profile_id: str | None = None,
        worker_instance_id: str | None = None,
    ) -> UsageWriter:
        """Create the non-gateway CLI invocation-aggregate writer."""
        journal = getattr(run, "usage_journal", None) or UsageJournal(
            self.sessions_root
            / f"{safe_run_storage_key(run.run_id)}-usage-journal.jsonl"
        )
        run.usage_journal = journal
        return UsageWriter(
            journal,
            bus=run.bus,
            context=UsageContext(
                run_id=run.run_id,
                challenge_id=run.run_id,
                worker_instance_id=worker_instance_id,
                solver_id=solver_id,
                profile_id=profile_id,
                configured_account_id=None,
                billing_account_id=None,
                producer="fallback",
            ),
        )

    def board_token(self, run_id: str) -> str:
        run = self.get(run_id)
        return run.blackboard_token if run else ""

    def verify_board_token(self, run_id: str, token: str) -> bool:
        expected = self.board_token(run_id)
        return bool(expected) and secrets.compare_digest(expected, str(token or ""))

    def archive_legacy_runs(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Archive old SQLite runs under sessions/_archive before cleanup.

        The operation is intentionally conservative: each run is archived to a
        tar.gz, the archive is opened and validated, and only then is the original
        run directory removed. Any failure leaves the source data in place.
        """
        archive_root = self.sessions_root / "_archive"
        archived: list[str] = []
        skipped: list[str] = []
        failed: list[dict[str, str]] = []
        for path in sorted(self.sessions_root.iterdir()):
            name = path.name
            if not path.is_dir() or name.startswith("_"):
                continue
            if name in self.runs:
                run = self.runs[name]
                if run.started and not run.finished:
                    skipped.append(f"{name}:active")
                    continue
            graph_db = path / "graph" / "shared_graph.db"
            jsonl = self.sessions_root / f"{name}.jsonl"
            if not graph_db.exists() and not jsonl.exists():
                skipped.append(f"{name}:no_data")
                continue
            if dry_run:
                archived.append(name)
                continue
            archive_root.mkdir(parents=True, exist_ok=True)
            archive_path = archive_root / f"{name}.tar.gz"
            if archive_path.exists():
                skipped.append(f"{name}:archive_exists")
                continue
            try:
                with tarfile.open(archive_path, "w:gz") as tar:
                    tar.add(path, arcname=name)
                    if jsonl.exists():
                        tar.add(jsonl, arcname=f"{name}.jsonl")
                with tarfile.open(archive_path, "r:gz") as check:
                    if not check.getnames():
                        raise RuntimeError("archive is empty")
                shutil.rmtree(path)
                if jsonl.exists():
                    jsonl.unlink()
                archived.append(name)
            except Exception as exc:  # noqa: BLE001 - cleanup must not lose data
                failed.append({"run_id": name, "error": str(exc)})
                if archive_path.exists():
                    try:
                        archive_path.unlink()
                    except OSError:
                        pass
        return {
            "dry_run": dry_run,
            "archived": archived,
            "skipped": skipped,
            "failed": failed,
        }

    @staticmethod
    def _bump_bus_seq(bus: EventBus, seq: int) -> None:
        try:
            bus._seq = max(int(getattr(bus, "_seq", 0) or 0), int(seq or 0))
        except Exception:
            pass

    def _sync_bus_seq(
        self, bus: EventBus, *, store: Optional[SessionStore] = None,
        run_id: str
    ) -> None:
        store = store or SessionStore(root=self.sessions_root)
        self._bump_bus_seq(bus, store.last_stream_seq(run_id))

    def create_new(self) -> Run:
        """Mint a run under a fresh, never-reused id (for '+ New solve')."""
        self._seq += 1
        run_id = f"run-{self._seq:04d}"
        while run_id in self.runs:
            self._seq += 1
            run_id = f"run-{self._seq:04d}"
        return self.create(run_id)

    async def rebuild_ledger(self, run_id: str) -> Run:
        """Replay canonical usage and reconcile journal terminals on demand.

        This is the explicit operator recovery path behind the budget snapshot
        UI.  It uses the same all-or-fail sequence as startup and pre-spawn
        reconciliation: replay first, project the budget gate, then canonicalize
        journal-only terminals through the checked event path.
        """
        run = self.runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.ledger is None or run.usage_journal is None or run.store is None:
            raise RuntimeError("ledger_unavailable")
        if run.spawn_guard is not None:
            run.spawn_guard.mark_rebuilding()
        try:
            run.ledger.rebuild(
                run.store.read_events(run_id),
                journal=run.usage_journal,
            )
            run.budget_gate.rebuild(
                run.ledger.records.values(),
                run.ledger.budget_actions,
            )
            pending = run.ledger.pending_recovery_records()
            for record in pending:
                await run.bus.emit_checked(Event(
                    event_type=EventType.USAGE_RECORDED,
                    run_id=run.run_id,
                    payload=record.__dict__.copy(),
                ))
            run.ledger.mark_reconciled(record.usage_id for record in pending)
            if run.spawn_guard is not None:
                run.spawn_guard.mark_ready()
            return run
        except Exception as exc:
            run.ledger.mark_failed(str(exc))
            if run.spawn_guard is not None:
                run.spawn_guard.mark_failed(str(exc))
            raise

    async def _reconcile_ledger(self, run: Run) -> None:
        """Canonicalize journal-only terminals before allowing a spawn."""
        if run.ledger is None:
            return
        pending = run.ledger.pending_recovery_records()
        if not pending:
            if run.spawn_guard is not None and run.spawn_guard.ledger_state == "rebuilding":
                run.spawn_guard.mark_ready()
            return
        if run.spawn_guard is not None:
            run.spawn_guard.mark_rebuilding()
        try:
            for record in pending:
                await run.bus.emit_checked(Event(
                    event_type=EventType.USAGE_RECORDED,
                    run_id=run.run_id,
                    payload=record.__dict__.copy(),
                ))
            run.ledger.mark_reconciled(record.usage_id for record in pending)
            if run.spawn_guard is not None:
                run.spawn_guard.mark_ready()
        except Exception as exc:
            run.ledger.mark_failed(str(exc))
            if run.spawn_guard is not None:
                run.spawn_guard.mark_failed(str(exc))
            raise

    def _default_runtime_pool_manager_factory(self, *, run_id: str, snapshot: RuntimeSnapshot, **_: Any) -> ContainerPoolManager:
        """Production pool composition — identical pieces to the TUI path."""
        from dswarm.solver.runtime_factory import build_pool_manager_for_run

        run = self.runs.get(run_id)
        return build_pool_manager_for_run(
            run_id=run_id,
            snapshot=snapshot,
            worker_profiles=self._run_frozen_worker_profiles(run),
            llm_providers=self._run_frozen_provider_registry(run),
            sessions_root=self.sessions_root,
            bus=getattr(run, "bus", None),
            budget_gate=getattr(run, "budget_gate", None) or ProfileBudgetGate(),
        )

    def _run_worker_profiles(self, run: Run) -> list[dict[str, Any]]:
        body = dict(self._load_dispatch(run.run_id) or {})
        wc = self.worker_config.resolve(
            str((body.get("challenge") or {}).get("category") or "")
        )
        profiles = body.get("worker_profiles") or wc.get("worker_profiles") or []
        return [p for p in profiles if isinstance(p, dict)]

    def _run_frozen_worker_profiles(self, run: Run | None) -> list[dict[str, Any]]:
        """Return safe launch-time worker identity/configuration for pool setup."""
        binding = getattr(run, "binding_snapshot", {}) if run is not None else {}
        frozen = (
            binding.get("runtime_worker_profiles")
            if isinstance(binding, Mapping)
            else None
        )
        if isinstance(frozen, list):
            profiles = [dict(profile) for profile in frozen if isinstance(profile, Mapping)]
            if profiles:
                return profiles
        return self._run_worker_profiles(run)

    def _run_frozen_provider_registry(self, run: Run | None) -> list[dict[str, Any]]:
        """Rebuild provider transport metadata from the immutable Run snapshot."""
        binding = getattr(run, "binding_snapshot", {}) if run is not None else {}
        if not isinstance(binding, Mapping):
            return []
        fields = ("base_url", "wire_api", "auth_mode", "auth_header", "auth_prefix")
        providers: dict[str, dict[str, Any]] = {}

        def add_provider(raw: Mapping[str, Any]) -> None:
            # provider_ref is the binding id the pool/worker contract uses; the
            # profile `id` (a stable seat id like seat_pi_147fcb) is NOT a
            # provider. Prefer provider_ref so provider-bound profiles and roles
            # are actually discoverable by the probe/gateway lookup.
            provider_id = str(
                raw.get("provider_ref") or raw.get("id") or ""
            ).strip()
            if not provider_id or provider_id in providers:
                return
            descriptor: dict[str, Any] = {"id": provider_id}
            for transport_field in fields:
                value = raw.get(transport_field)
                if value in (None, ""):
                    continue
                clean = (
                    _safe_transport_url(value)
                    if transport_field == "base_url"
                    else _binding_scalar(value)
                )
                if clean not in (None, ""):
                    descriptor[transport_field] = clean
            providers[provider_id] = descriptor

        frozen_registry = binding.get("runtime_providers")
        if isinstance(frozen_registry, list):
            for provider in frozen_registry:
                if isinstance(provider, Mapping):
                    add_provider(provider)
        roles = binding.get("roles")
        if isinstance(roles, Mapping):
            for role in roles.values():
                if isinstance(role, Mapping):
                    add_provider(role)
        # The full profile list preserves provider_ref/base_url even when the
        # per-role binding (roles.worker) froze only the model.
        for key in ("runtime_worker_profiles", "worker_profiles"):
            profiles = binding.get(key)
            if isinstance(profiles, list):
                for profile in profiles:
                    if isinstance(profile, Mapping):
                        add_provider(profile)
        return list(providers.values())

    def _freeze_dispatch_runtime(self, run: Run) -> None:
        """Freeze exactly one immutable runtime context before first dispatch.

        M9a fail-closed contract: container-backend profiles require a frozen
        runtime policy, and the web deck historically never wired the freeze —
        every spawn failed with ``runtime_policy_required`` while the rail kept
        showing a live run (run-4408). This mirrors drivers._swarm_driver's
        resolution (request body > worker config > env), including the offline
        network clamp, then delegates to the create-once
        :meth:`ensure_runtime_context`. Idempotent across re-dispatch/reopen:
        the first frozen snapshot is authoritative forever.
        """
        if run.runtime_policy is not None:
            return
        body = dict(self._load_dispatch(run.run_id) or {})
        if not body:
            return
        wc = self.worker_config.resolve(
            str((body.get("challenge") or {}).get("category") or "")
        )
        engines = body.get("engines") or wc.get("engines") or []
        worker_profiles = body.get("worker_profiles") or wc.get("worker_profiles") or []
        worker_profiles = [p for p in worker_profiles if isinstance(p, dict)]
        if not worker_profiles:
            return
        # Credential-mode fix: the new-shape projection (seats/environments)
        # carries no base_url, so every container profile would resolve to
        # gateway mode with an unconfigured gateway and the worker would fall
        # back to the default DeepSeek endpoint (model-not-found). A credential
        # account that stores BASE_URL is a custom endpoint: re-attach it so
        # the projection runs in direct mode (BASE_URL+API_KEY into the worker).
        accounts_root = account_store_root(self.sessions_root)
        for profile in worker_profiles:
            label = str(profile.get("credential_account") or "").strip()
            if not label or profile.get("base_url"):
                continue
            base_url_file = accounts_root / label / "BASE_URL"
            if base_url_file.is_file():
                try:
                    url = base_url_file.read_text(encoding="utf-8").strip()
                    if url:
                        profile["base_url"] = url
                except OSError:
                    pass
        worker_backend = resolve_worker_backend(
            request_backend=body.get("worker_backend"),
            config_backend=wc.get("worker_backend"),
            env_backend=os.environ.get("DSWARM_WORKER_BACKEND"),
            in_web_container=is_web_container(),
        )
        runtime_profiles = list(
            body.get("runtime_profiles") or wc.get("runtime_profiles") or []
        )
        selected = _selected_roster(engines, worker_profiles)
        # Same offline clamp as the dispatch path (drivers._swarm_driver): the
        # deck's "offline" switch opts INTO hard network isolation, softened
        # only when a selected profile needs a custom LLM endpoint. With the
        # flag absent, runtime profiles keep their configured network (bridge) —
        # the reverse-dial control link is a hard requirement of the M9a
        # runtime contract. (Do NOT invert this default: network:none pool
        # containers can never pass hello.)
        offline = bool(body.get("offline", False))
        offline_endpoint_profiles = (
            [p for p in selected if profile_uses_endpoint(p)] if offline else []
        )
        strict_offline_network = offline and not offline_endpoint_profiles
        if strict_offline_network:
            runtime_profiles = [
                {**r, "network": "none"}
                if isinstance(r, dict) and str(r.get("backend") or "") == "container"
                else r
                for r in runtime_profiles
            ]
        run_max_workers = int(
            body.get("max_workers", wc.get("max_workers", 10)) or 0
        ) or max(1, len(selected))
        # Local-dev dual gate (docs/runtime-pools.md): BOTH the launcher flag
        # (the deck's explicit local-dev request) and the operator env
        # (DSWARM_ALLOW_LOCAL_WORKERS) must authorize host-local workers.
        policy = build_runtime_policy(
            mode="docker" if worker_backend == "container" else "local_dev",
            local_dev_cli_flag=bool(body.get("local_dev", False)),
        )
        self.ensure_runtime_context(
            run.run_id,
            policy=policy,
            worker_profiles=worker_profiles,
            runtime_profiles=runtime_profiles,
            run_max_workers=run_max_workers,
        )

    async def _settle_start_failure(self, run: Run, exc: BaseException) -> None:
        """Terminate a dispatch that failed before the scheduler took ownership.

        The web route creates the run handle and remembers dispatch settings before
        runtime preflight. If that preflight raises, no driver task exists to run
        the normal terminal-event fallback in ``_launch``. Always publish a
        terminal event and close the bus here so SSE clients and the rail cannot
        mistake the failed attempt for a live run.
        """
        run.started = False
        run.finished = True
        run.solved = False
        run.failure_reason = "runtime_failure"
        run.paused = False
        run.queued = False
        run.queue_position = None
        run.cancelled = False
        run.awaiting_help = False
        run.help_text = ""
        # Preflight exceptions happen on the host and may contain URLs, headers,
        # account material, or an SDK's repr of its request.  Only structured
        # runtime errors have an operator-safe detail contract; all other errors
        # expose the exception type, never the raw message.
        code = getattr(exc, "code", None)
        safe_detail = getattr(exc, "safe_detail", None)
        if isinstance(code, str) and isinstance(safe_detail, str):
            detail = sanitize_public_text(f"{code}: {safe_detail}", limit=500)
        elif isinstance(exc, RuntimePolicyError):
            # RuntimePolicyError messages are stable machine-readable policy
            # codes. Keep them useful, but reject an unexpected non-code value.
            raw = str(exc).strip()
            detail = (sanitize_public_text(raw, limit=500)
                      if re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", raw)
                      else "runtime_policy_error")
        else:
            detail = f"{type(exc).__name__}: startup preflight failed"
        try:
            # A failed POST must still leave a durable terminal marker.  The bus
            # has a critical SessionStore sink, so use the checked path instead of
            # the best-effort publish used by ordinary progress events.
            await run.bus.emit_checked(Event(
                event_type=EventType.RUN_FINISHED,
                run_id=run.run_id,
                payload={
                    "flag": run.flag,
                    "flags": list(run.flags),
                    "expected_flags": run.expected_flags,
                    "multi_flag": run.multi_flag,
                    "solved": False,
                    "reason": "runtime_failure",
                    "detail": detail,
                },
            ))
        except Exception as publish_exc:
            LOG.debug("failed to publish dispatch failure for %s (%s)",
                      run.run_id, type(publish_exc).__name__)
        finally:
            if run.pool_manager is not None:
                try:
                    await run.pool_manager.close()
                except Exception as close_exc:
                    LOG.warning("runtime pool close failed after dispatch failure for %s (%s)",
                                run.run_id, type(close_exc).__name__)
                run.pool_manager = None
            await run.bus.close()
            self._unregister_gateway_bridge(run.run_id)

    async def start(self, run_id: str, driver: Driver) -> Run:
        """Dispatch a run through the P4 scheduler: below the concurrency cap it
        launches immediately (today's behavior); at the cap it enters the FIFO
        queue (RUN_QUEUED with its position) and starts when a slot frees.

        A run that is ALREADY live (task pending) is not double-dispatched —
        the existing handle is returned untouched.
        """
        run = self.create(run_id)
        if run.task is not None and not run.task.done():
            return run  # already live — never stack a second driver on one run
        try:
            # A previous failed/finished dispatch closes its EventBus. Reusing
            # that run id must rebuild the bus before any new events are emitted;
            # otherwise the retry can appear successful while SSE subscribers
            # remain attached to a permanently closed bus. Keep the observation
            # because the route resets ``run.finished`` before calling start.
            bus_was_closed = bool(getattr(run.bus, "_closed", False))
            if bus_was_closed:
                self._fresh_bus(run)
            await self._reconcile_ledger(run)
            if run.runtime_snapshot is not None and (run.finished or bus_was_closed):
                # The closed-bus marker is also the reliable previous-cycle
                # signal: /start clears the in-memory finished bit up front.
                await self._cleanup_before_reopen(run)
            # M9a: freeze the runtime context BEFORE dispatch so a misconfigured
            # launch fails here (operator-visible) instead of as a silently dead
            # run whose every spawn raised runtime_policy_required.
            if getattr(driver, "dispatch_kind", "swarm") == "swarm":
                self._freeze_dispatch_runtime(run)
            self._ensure_runtime_pool_manager(run)
            if run.spawn_guard is not None:
                await run.spawn_guard.ensure_ready(run_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._settle_start_failure(run, exc)
            raise
        run.started = True  # the operator dispatched it; rail shows queued/running
        position = self.scheduler.submit(run_id, driver)
        if position is None:
            # slot was free and is now held by this run — launch right away.
            self._launch(run, driver)
            return run
        run.queued = True
        run.queue_position = position
        await run.bus.emit(Event(
            event_type=EventType.RUN_QUEUED, run_id=run_id,
            payload={"position": position,
                     "active": self.scheduler.active_count,
                     "limit": self.scheduler.max_concurrent_runs}))
        return run

    async def _emit_provider_diagnostics(
        self, run: "Run", detail: str, *, worker_id: str = "", provider: str = "",
        account_id: str = "", active_workers: int | None = None,
    ) -> None:
        """Emit operator-facing diagnostics for a provider/runtime failure.

        This does not verify/accept flags and does not alter solver state; it only
        translates raw LLM/provider/CLI error text into stable events the UI can
        surface. Fatal quota/auth/model errors are visible immediately as
        ``provider.error``; repeated failures in the sliding window additionally
        produce ``provider.batch_alert`` so the operator knows dispatch should be
        paused or reconfigured.
        """
        raw = _safe_runtime_detail(detail)
        if not raw:
            return
        diag = classify_provider_error(
            raw,
            provider=_safe_runtime_detail(provider, limit=160),
            account_id=_safe_runtime_detail(account_id, limit=160),
            worker_id=_safe_runtime_detail(worker_id or run.run_id, limit=160),
        )
        try:
            await run.bus.emit(Event(
                event_type=EventType.PROVIDER_ERROR,
                run_id=run.run_id,
                solver_id=worker_id or None,
                payload=diag.to_event(),
            ))
        except Exception as emit_exc:
            LOG.warning("failed to emit provider diagnostic for run %s (%s)",
                        run.run_id, type(emit_exc).__name__)
            return
        try:
            active = active_workers
            if active is None:
                active = max(self.scheduler.active_count, 1)
            alert = self.provider_errors.record(
                diag, now=time.time(), active_workers=int(active or 0),
            )
            if alert:
                await run.bus.emit(Event(
                    event_type=EventType.PROVIDER_BATCH_ALERT,
                    run_id=run.run_id,
                    payload=alert,
                ))
        except Exception as aggregate_exc:
            LOG.warning("failed to aggregate provider diagnostic for run %s (%s)",
                        run.run_id, type(aggregate_exc).__name__)

    def _launch(self, run: "Run", driver: Driver) -> None:
        """Create the run's driver task (slot already held by the scheduler).
        The driver's finally frees the slot and fills the queue."""
        run_id = run.run_id
        run.queued = False
        run.queue_position = None

        async def _go() -> None:
            failure_detail = ""
            try:
                await driver(run)
            except Exception as exc:
                LOG.error("driver crashed for run %s (%s)", run_id, type(exc).__name__)
                failure_detail = _safe_runtime_detail(exc)
                await self._emit_provider_diagnostics(run, failure_detail)
            finally:
                # If the driver exited WITHOUT emitting RUN_FINISHED (cancelled
                # mid-run, or it crashed before its own terminal event), the deck
                # never gets a terminal signal and the rail spins forever (a "ghost
                # run"). _meta_sink flips run.finished=True on a real RUN_FINISHED,
                # so a still-False flag here means none was emitted — synthesize one
                # before closing the bus so every run reaches a settled state.
                if not run.finished:
                    # Set the projection before emitting so a sink failure cannot
                    # leave a crashed run looking like an ordinary unfinished run.
                    run.failure_reason = "runtime_failure"
                    try:
                        await run.bus.emit(Event(
                            event_type=EventType.RUN_FINISHED, run_id=run_id,
                            payload={"flag": run.flag, "flags": list(run.flags),
                                     "expected_flags": run.expected_flags,
                                     "multi_flag": run.multi_flag,
                                     "solved": run.solved,
                                     "reason": "runtime_failure",
                                     "detail": failure_detail}))
                    except Exception:
                        pass
                run.finished = True
                # P4: the slot freed — hand the queue the chance to start the next
                # run BEFORE closing this bus (launch is independent of it).
                self.scheduler.release(run_id)
                await self._fill_slots()
                await run.bus.close()
                self._unregister_gateway_bridge(run_id)

        run.task = asyncio.create_task(_go())

    # ── P4 scheduler plumbing ─────────────────────────────────────────────────
    async def _fill_slots(self) -> None:
        """Launch every queued run a freed slot can hold, FIFO (held/paused
        queued runs are skipped but keep their position). Called from a driver's
        finally, a limit raise, and queued-run resume."""
        while True:
            # never over-subscribe: only pop while a slot is actually free.
            if self.scheduler.active_count >= self.scheduler.max_concurrent_runs:
                return
            nxt = self.scheduler.next_to_dispatch()
            if nxt is None:
                return
            rid, _driver = nxt
            run = self.runs.get(rid)
            if run is None:
                self.scheduler.release(rid)  # vanished (deleted) while queued
                continue
            await run.bus.emit(Event(
                event_type=EventType.RUN_DISPATCHED, run_id=rid,
                payload={"active": self.scheduler.active_count,
                         "limit": self.scheduler.max_concurrent_runs}))
            # the emit above yields the loop — the operator may have DELETED this
            # run in the meantime. Never launch a driver on a deleted handle.
            if self.runs.get(rid) is not run:
                self.scheduler.release(rid)
                continue
            self._launch(run, _driver)

    async def cancel_queued(self, run_id: str) -> bool:
        """Operator cancels a run that is STILL WAITING in the queue: drop it
        from the FIFO, settle the run (cancelled + RUN_FINISHED so the rail and
        the event stream get a terminal state). False when it wasn't queued."""
        run = self.runs.get(run_id)
        if run is None or not self.scheduler.cancel(run_id):
            return False
        run.queued = False
        run.queue_position = None
        run.cancelled = True
        await run.bus.emit(Event(
            event_type=EventType.RUN_CANCELLED, run_id=run_id,
            payload={"reason": "operator_cancelled_queued"}))
        await run.bus.emit(Event(
            event_type=EventType.RUN_FINISHED, run_id=run_id,
            payload={"flag": run.flag, "flags": list(run.flags),
                     "expected_flags": run.expected_flags,
                     "multi_flag": run.multi_flag,
                     "solved": False, "reason": "cancelled"}))
        await run.bus.close()
        return True

    async def cancel_run(self, run_id: str) -> bool:
        """P4 'cancel': a queued run is dropped from the FIFO; a LIVE run gets
        the existing graceful stop (operator_stop, workers killed, state kept).
        False for an unknown/finished run."""
        run = self.runs.get(run_id)
        if run is None:
            return False
        if self.scheduler.is_queued(run_id):
            return await self.cancel_queued(run_id)
        if run.task is not None and not run.task.done():
            return await self.post_hitl(run_id, "global", "stop")
        return False

    async def pause_queued(self, run_id: str) -> bool:
        """Hold a queued run: it keeps its queue position but is skipped by
        dispatch until resumed. Emits the same HITL_RESPONSE pause the live-run
        path uses, so the rail's paused icon works unchanged."""
        run = self.runs.get(run_id)
        if run is None or not self.scheduler.pause(run_id):
            return False
        run.paused = True
        await run.bus.emit(Event(
            event_type=EventType.HITL_RESPONSE, run_id=run_id,
            payload=hitl_response_payload("global", "pause")))
        return True

    async def resume_queued(self, run_id: str) -> bool:
        """Un-hold a queued run and give the scheduler a chance to dispatch it."""
        run = self.runs.get(run_id)
        if run is None or not self.scheduler.resume(run_id):
            return False
        run.paused = False
        await run.bus.emit(Event(
            event_type=EventType.HITL_RESPONSE, run_id=run_id,
            payload=hitl_response_payload("global", "resume")))
        await self._fill_slots()
        return True

    def scheduler_snapshot(self) -> dict[str, Any]:
        """BTFly-style SchedulerStatus: settings + active count + FIFO queue with
        per-run title/category enriched from the live run handles."""
        s = self.scheduler
        queue = []
        for rid, pos, at in s.queued_entries():
            run = self.runs.get(rid)
            queue.append({
                "run_id": rid,
                "title": (run.name or rid) if run is not None else rid,
                "category": (run.category or "") if run is not None else "",
                "position": pos,
                "queued_at": at,
            })
        return {
            "settings": {"max_concurrent_runs": s.max_concurrent_runs},
            "active_count": s.active_count,
            "queued_count": s.queued_count,
            "queue": queue,
        }

    def set_scheduler_limit(self, n: int) -> int:
        """Clamp (1..8) + persist the concurrency limit; returns the effective
        value. A raise is dispatched by the caller via dispatch_pending()."""
        return self.scheduler.set_limit(n)

    async def dispatch_pending(self) -> None:
        """Give the scheduler a chance to fill slots (limit raise / resume)."""
        await self._fill_slots()

    # Actions that may start a standby worker after a terminal Run. Review is
    # deliberately excluded: the Target review API owns its read-only snapshot.
    _STANDBY_ACTIONS = {"ask", "hint", "mark_false", "writeup", "redirect", "focus"}
    _LEGACY_ACTIONS = {
        "ask", "hint", "writeup", "pause", "redirect", "focus", "mark_false",
    }
    _COMMAND_ALIASES = {"prompt": "hint"}
    _PUBLIC_COMMANDS = {"prompt", "stop", "resume", "review"}
    @staticmethod
    def _run_recipient_ids(run: Run) -> set[str]:
        """Return recipient ids observed on this Run without inventing lanes."""
        ids: set[str] = {"all", "global", "reason"}
        try:
            for event in run.store.read_events(run.run_id):
                if event.event_type in {EventType.WORKER_STATUS, EventType.WORKER_LIFECYCLE}:
                    payload = event.payload or {}
                    worker = str(payload.get("worker") or event.solver_id or "").strip()
                    if worker:
                        ids.update({worker, f"solver:{worker}"})
                elif event.event_type is EventType.BLACKBOARD_DELTA:
                    payload = event.payload or {}
                    worker = str(payload.get("worker") or payload.get("worker_id") or "").strip()
                    if worker:
                        ids.update({worker, f"solver:{worker}"})
        except Exception:
            pass
        for attr in ("deck_workers", "worker_ids"):
            values = getattr(run, attr, None)
            if isinstance(values, (list, tuple, set)):
                for value in values:
                    worker = str(value or "").strip()
                    if worker:
                        ids.update({worker, f"solver:{worker}"})
        return ids

    async def post_hitl(self, run_id: str, target: str, action: str, **fields: Any) -> bool:
        """Route one command to the selected Run.

        New callers send ``recipients`` and ``client_request_id``.  Those
        recipients are checked against this Run's observed worker identities;
        legacy callers retain the old target spelling for compatibility.
        STOP and RESUME are always Run-wide regardless of selected recipients.
        """
        requested_action = str(action or "").strip().lower()
        if requested_action not in self._PUBLIC_COMMANDS | self._LEGACY_ACTIONS:
            return False
        action = self._COMMAND_ALIASES.get(requested_action, requested_action)
        run = self.runs.get(run_id)
        if run is None:
            return False

        supplied = fields.get("recipients")
        strict_recipients = isinstance(supplied, (list, tuple, set))
        if strict_recipients and requested_action not in self._PUBLIC_COMMANDS:
            return False
        raw_targets: list[str] = []
        if strict_recipients:
            raw_targets.extend(str(item).strip() for item in supplied if str(item).strip())
        if not raw_targets:
            raw_targets = [str(target or "global").strip()]
            if "," in raw_targets[0]:
                raw_targets = [part.strip() for part in raw_targets[0].split(",") if part.strip()]
        raw_targets = ["all" if item == "global" else item for item in raw_targets] or ["all"]
        if action in {"stop", "resume", "review"}:
            raw_targets = ["all"]
        if "all" in raw_targets and len(raw_targets) > 1:
            return False
        if strict_recipients:
            allowed = self._run_recipient_ids(run)
            if any(item not in allowed for item in raw_targets):
                return False
        fields = {key: value for key, value in fields.items()
                  if key not in {"recipients", "run_id", "client_request_id"}}
        if action == "review":
            # Review never queues HITL, starts standby, or mutates Target state.
            return bool(run.finished or run.status() in {"finished", "solved", "failed", "cancelled"})
        if action == "resume" and (run.task is None or run.task.done()):
            if not run.dispatch_body and run.runtime_snapshot is None and not run.started:
                return False
            return await self.resolve(run_id, {"operator_resume": True})
        if action == "writeup" and run.task is not None and not run.task.done():
            return False
        if len(raw_targets) > 1:
            results = [await self.post_hitl(run_id, item, action, **fields) for item in raw_targets]
            return all(results)
        target = raw_targets[0]
        if action == "stop":
            await run.bus.emit(Event(
                event_type=EventType.HITL_RESPONSE, run_id=run_id,
                payload=hitl_response_payload("global", action, **fields)))
            if run.task is not None and not run.task.done():
                try:
                    run.hitl.put_nowait({"action": "stop", "target": "global", **fields})
                except Exception:
                    pass
                for _ in range(40):
                    await asyncio.sleep(0.1)
                    if run.task.done():
                        break
                if not run.task.done():
                    run.task.cancel()
            else:
                run.finished = True
                run.paused = False
                await run.bus.emit(Event(
                    event_type=EventType.RUN_FINISHED, run_id=run_id,
                    payload={"flag": run.flag, "flags": list(run.flags),
                             "expected_flags": run.expected_flags,
                             "multi_flag": run.multi_flag, "solved": run.solved,
                             "reason": "operator_stop"}))
            return True
        sig = (target, action, str(fields.get("text") or fields.get("hint") or ""),
               str(fields.get("url") or fields.get("target_url") or ""),
               str(fields.get("flag") or ""))
        if action != "writeup" and getattr(run, "_last_hitl_sig", None) == sig:
            await run.bus.emit(Event(
                event_type=EventType.HITL_RESPONSE, run_id=run_id,
                payload=hitl_response_payload(target, action, delivery="duplicate", **fields)))
            return True
        run._last_hitl_sig = sig
        live = run.task is not None and not run.task.done()
        delivery = "queued_for_next_worker" if live and action in ("hint", "focus") else (
            "applied_live" if live else "standby" if action in self._STANDBY_ACTIONS else "no_live_workers")
        await run.hitl.put({"target": target, "action": action, **fields})
        await run.bus.emit(Event(
            event_type=EventType.HITL_RESPONSE, run_id=run_id,
            payload=hitl_response_payload(target, action, delivery=delivery, **fields)))
        if not live and action in self._STANDBY_ACTIONS:
            self._ensure_standby(run_id, {"target": target, "action": action, **fields})
        return True


    async def post_worker_cmd(self, run_id: str, action: str, *,
                              engine: Optional[str] = None,
                              solver_id: Optional[str] = None) -> bool:
        """Queue an operator worker command (spawn/kill) for the LIVE scheduler
        to drain. Only meaningful while the run is running; a finished/ghost run
        has no scheduler loop to act on it, so we reject it."""
        run = self.runs.get(run_id)
        if run is None:
            return False
        live = run.task is not None and not run.task.done()
        if not live:
            return False
        cmd: dict[str, Any] = {"action": action}
        if engine:
            cmd["engine"] = engine
        if solver_id:
            cmd["solver_id"] = solver_id
        await run.worker_cmds.put(cmd)
        return True

    async def resolve(self, run_id: str, body: dict[str, Any] | None = None) -> bool:
        """Continue a finished run through the normal scheduler/launch path.

        Recovery must be indistinguishable from a normal dispatch after the
        ``RUN_REOPENED`` marker: it gets a scheduler slot, uses ``_launch`` for
        exception handling/terminal events, and restores the original dispatch
        settings. The old implementation created an untracked task directly; a
        preflight/worker exception therefore produced only ``run.reopened`` and
        silently closed the bus, leaving the UI claiming that the swarm was back
        while no worker ever came online.
        """
        run = self.runs.get(run_id)
        if run is None:
            return False
        if run.task is not None and not run.task.done():
            return False  # already live - nothing to relaunch (use HITL instead)
        # backend configuration.
        ch: dict[str, Any] = {}
        try:
            import json
            wp = self.workspace_dir(run_id) / "winner.json"
            if wp.exists():
                ch = (json.loads(wp.read_text(encoding="utf-8")) or {}).get("challenge") or {}
        except Exception:
            ch = {}
        if not ch:
            try:
                async for ev in run.store.replay(run_id):
                    if ev.event_type is EventType.RUN_STARTED:
                        ch = (ev.payload or {}).get("challenge") or {}
                        # Keep looking: multiple workers emit run.started, but the
                        # challenge payload is equivalent and the first is enough.
                        break
            except Exception:
                ch = {}
        saved = self._load_dispatch(run_id)
        # A Target relationship is durable, while this dispatch sidecar records
        # the immutable historical challenge binding. Never reinterpret a smoke
        # test or another old Run as the current Target's challenge.
        self._reject_target_dispatch_mismatch(run, saved)
        request_body = dict(body or {})
        # Retired swarm fields must fail closed before any runtime cleanup,
        # reopening, or scheduler submission. A historical sidecar is read-only
        # data; do not silently strip or migrate it into a live dispatch.
        from apps.web.drivers import _reject_retired_swarm_fields
        _reject_retired_swarm_fields(saved)
        _reject_retired_swarm_fields(request_body)
        saved_ch = saved.get("challenge") if isinstance(saved.get("challenge"), dict) else {}
        if not ch:
            ch = dict(saved_ch)
        if not ch:
            ch = {"name": run.name or run_id, "category": run.category or "web",
                  "expected_flags": run.expected_flags,
                  "multi_flag": run.multi_flag}
        merged = merge_resolve_dispatch(saved, request_body, historical_challenge=ch)

        # Continue directly in the Reason scheduler on the existing evidence graph.
        # Recovery reuses the existing graph context and passes the current
        # dispatch contract directly to the Reason scheduler. This whole block is
        # preflight: if it fails, no RUN_REOPENED marker has been emitted and the
        # old terminal state remains authoritative.
        try:
            # Reopen only after all read-only reconstruction and validation that
            # can fail without touching runtime state. If this preflight fails,
            # no RUN_REOPENED marker is emitted and the old terminal state stays
            # authoritative. The normal /start path freezes this context before
            # scheduler submission; resolve must do the same or the Swarm
            # constructor rejects a historical local/container binding only after
            # the run has already been marked reopened.
            self._fresh_bus(run)
            await self._reconcile_ledger(run)
            if run.runtime_snapshot is not None:
                await self._cleanup_before_reopen(run)

            from apps.web.drivers import build_driver, preflight_swarm_dispatch
            driver = build_driver(
                merged,
                mgr=self,
                runtime_operation_kind="resolve",
            )
            if getattr(driver, "dispatch_kind", None) == "swarm":
                # A real driver is tagged. Legacy runs without a dispatch sidecar
                # or frozen runtime must not reinterpret the current global roster
                # as their historical execution binding.
                self._freeze_dispatch_runtime(run)
                if run.runtime_policy is None:
                    raise RuntimePolicyError("runtime_policy_required")
                await preflight_swarm_dispatch(merged, self)
            self._ensure_runtime_pool_manager(run)
            if run.spawn_guard is not None:
                await run.spawn_guard.ensure_ready(run_id)

            self.configure_budget(run_id, merged)
            self.remember_dispatch(run_id, merged)
        except BaseException:
            # A failed recovery may have composed a fresh pool manager and bus
            # before readiness/build validation completed. Tear both down so the
            # next retry cannot reuse a half-initialized manager or a closed-bus
            # usage writer. Do not replace the original exception.
            pool = run.pool_manager
            run.pool_manager = None
            if pool is not None:
                try:
                    await pool.close()
                except Exception as close_exc:
                    LOG.warning("runtime pool close failed after resolve preflight failure for %s (%s)",
                                run_id, type(close_exc).__name__)
            try:
                await run.bus.close()
            except Exception as close_exc:
                LOG.debug("event bus close failed after resolve preflight failure for %s (%s)",
                          run_id, type(close_exc).__name__)
            self._unregister_gateway_bridge(run_id)
            raise

        # Reset every lifecycle bit that can make the rail show a stale
        # terminal/queued/cancelled state. Keep old flags as deliberate evidence
        # carried into the continuation.
        run.started = True
        run.finished = False
        run.solved = False
        run.paused = False
        run.queued = False
        run.queue_position = None
        run.cancelled = False
        run.awaiting_help = False
        run.help_text = ""
        await run.bus.emit(Event(
            event_type=EventType.RUN_REOPENED, run_id=run_id,
            payload={"reason": "resolve"}))

        position = self.scheduler.submit(run_id, driver)
        if position is None:
            # _launch owns the task, catches driver failures, emits a terminal
            # runtime_failure, releases the slot, and closes the bus.
            self._launch(run, driver)
        else:
            run.queued = True
            run.queue_position = position
            await run.bus.emit(Event(
                event_type=EventType.RUN_QUEUED, run_id=run_id,
                payload={"position": position,
                         "active": self.scheduler.active_count,
                         "limit": self.scheduler.max_concurrent_runs}))
        return True

    def _unregister_gateway_bridge(self, run_id: str) -> None:
        """Detach a finished run from the process-wide gateway bridge.

        Usage remains recoverable from the run-scoped journal; this only prevents
        late gateway callbacks from targeting a closed EventBus. Standby/resolve
        paths call ``_fresh_bus`` and register the run again before spawning.
        """
        try:
            from dswarm.solver.modelgateway import ModelGateway
            unregister = getattr(ModelGateway.instance(), "unregister_usage_bridge", None)
            if callable(unregister):
                unregister(run_id)
        except Exception:
            LOG.debug("failed to unregister gateway bridge for %s", run_id, exc_info=True)

    def _fresh_bus(self, run: Run) -> None:
        """Replace a run's CLOSED bus with a live one (same sinks) so a standby
        worker's events reach a freshly-opened SSE stream. After the main run
        ended, run.bus was close()d — its subscribers got the end sentinel and the
        browser's EventSource reconnected, but the closed bus won't fan out to new
        subscribers. A new bus, re-wired to the SessionStore + rail meta sinks,
        keeps the durable JSONL append-only and the rail metadata fresh."""
        durable_seq = run.store.last_stream_seq(run.run_id)
        self._bump_bus_seq(run.bus, durable_seq)
        if not getattr(run.bus, "_closed", False):
            return  # still open (live run) — keep it
        new_bus = EventBus()
        new_bus.add_critical_sink(run.store.sink, run.store.append_checked)
        new_bus.add_sink(self._meta_sink_for(run))
        # carry the seq forward so SSE Last-Event-ID continuity holds across runs
        self._bump_bus_seq(new_bus, max(getattr(run.bus, "_seq", 0), durable_seq))
        run.bus = new_bus
        run.cost.bus = new_bus  # cost updates emit onto the live bus too
        self._configure_gateway_bridge(run)

    def _ensure_standby(self, run_id: str, cmd: dict[str, Any]) -> None:
        """Spin up a standby worker to serve `cmd`, unless one is already running
        (serialized — one standby per run). Fire-and-forget; events stream live."""
        run = self.runs.get(run_id)
        if run is None:
            return
        if run.standby_task is not None and not run.standby_task.done():
            return  # a standby is already serving this run — don't pile on
        self._fresh_bus(run)
        from apps.web.drivers import build_standby_driver
        driver = build_standby_driver(cmd, mgr=self)

        async def _go() -> None:
            try:
                await self._reconcile_ledger(run)
                if run.spawn_guard is not None:
                    await run.spawn_guard.ensure_ready(run_id)
                LOG.info("standby worker starting for %s action=%s",
                         run_id, cmd.get("action"))
                await driver(run)
                LOG.info("standby worker finished for %s action=%s",
                         run_id, cmd.get("action"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = _safe_runtime_detail(exc)
                LOG.error("standby worker failed for %s action=%s (%s)",
                           run_id, cmd.get("action"), type(exc).__name__)
                try:
                    await run.bus.emit(Event(
                        event_type=EventType.HITL_REQUEST,
                        run_id=run_id,
                        payload={
                            "target": cmd.get("target") or "global",
                            "source": "standby",
                            "action": cmd.get("action"),
                            "need": f"standby worker failed: {detail}",
                            "text": f"standby worker failed: {detail}",
                        },
                    ))
                except Exception:
                    pass
            finally:
                # do NOT close the bus — keep the run reachable for more follow-ups.
                run.standby_task = None

        run.standby_task = asyncio.create_task(_go())

    def _meta_sink_for(self, run: Run):
        """The rail-metadata sink bound to a specific Run (used when rebuilding a
        fresh bus). Mirrors the inline _meta_sink in create()."""
        async def _meta_sink(ev: Event) -> None:
            self._seq += 1
            run.updated_seq = self._seq
            run.updated_at = ev.ts
            try:
                alerts = self._apply_accounting_event(run, ev)
                self._schedule_budget_alerts(run, alerts)
            except Exception as exc:
                if run.ledger is not None:
                    run.ledger.mark_failed(str(exc))
                if run.spawn_guard is not None:
                    run.spawn_guard.mark_failed(str(exc))
            if ev.event_type is EventType.RUN_REOPENED:
                run.reason_blocked = False
                run.reason_blocked_reason = ""
                run.finished = False
                run.failure_reason = None
                run.solved = False
                run.paused = False
                # Reopening clears a transient Reason block only. Do not invent
                # a provider recovery event or silently clear fallback history.
                if ev.payload.get("reason") == "resolve":
                    return
                bad = ev.payload.get("flag")
                if bad and run.flags:
                    run.flags = [f for f in run.flags if f != bad]
                    run.flag = run.flags[0] if run.flags else None
                else:
                    run.flag = None
                    run.flags = []
            elif ev.event_type is EventType.HITL_REQUEST:
                # a (standby) worker raised its hand — surface it on the summary, same
                # as the inline _meta_sink in create().
                run.awaiting_help = True
                run.help_text = str((ev.payload or {}).get("need")
                                     or (ev.payload or {}).get("text") or "")[:300]
            elif ev.event_type is EventType.HITL_RESPONSE:
                # mirror the primary sink: reflect pause/resume AND lower the hand.
                action = ev.payload.get("action")
                if action == "pause":
                    run.paused = True
                elif action == "resume":
                    run.paused = False
                run.awaiting_help = False
                run.help_text = ""
            elif ev.event_type is EventType.RUN_FINISHED:
                run.finished = True
                run.failure_reason = (
                    "runtime_failure"
                    if ev.payload.get("reason") == "runtime_failure"
                    else None
                )
                run.paused = False
                run.awaiting_help = False
                run.help_text = ""
                run.solved = bool(ev.payload.get("solved")) or run.solved
                run.merge_flags(ev.payload.get("flags") or ev.payload.get("flag"))
                if ev.payload.get("expected_flags"):
                    run.expected_flags = int(ev.payload["expected_flags"])
                if "multi_flag" in ev.payload:
                    run.multi_flag = bool(ev.payload["multi_flag"])
            else:
                _apply_blackboard_meta(run, ev)
        return _meta_sink

    async def shutdown(self) -> None:
        """Cancel every live task on server shutdown so no swarm/standby coroutine —
        and its shelled CLI subprocess group — survives as a budget-eating zombie.
        Cancels BOTH run.task AND standby_task (the latter was leaking: a standby
        worker spun up to answer a post-solve follow-up kept running). The titler is a
        detached create_task with no stored handle, so it can't be cancelled here; it
        is short-lived and self-terminates."""
        pending: list[asyncio.Task] = []
        for run in list(self.runs.values()):
            for t in (run.task, run.standby_task):
                if t is not None and not t.done():
                    t.cancel()
                    pending.append(t)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        managers: dict[int, Any] = {}
        for run in list(self.runs.values()):
            if run.pool_manager is not None:
                managers[id(run.pool_manager)] = run.pool_manager
        for manager in managers.values():
            try:
                await manager.close()
            except Exception:
                LOG.warning("runtime pool close failed during manager shutdown", exc_info=True)
