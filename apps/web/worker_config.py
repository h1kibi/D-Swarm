"""Default worker-roster configuration — which Pi worker profiles launch per challenge.

An OPERATOR preference (like the rail meta side-table), not part of the
event-sourced solve: a single small JSON file under the sessions root, loaded on
startup and rewritten on each mutation. It answers "when a challenge is
dispatched and the request doesn't say otherwise, which engines run, and how
many bootstrap workers?" — with optional per-category overrides for direction
profiles and endpoint-backed Pi workers.

The dispatch path (apps/web/drivers.py) reads `resolve(category)` as the FALLBACK
when the request body carries no explicit engines/start_workers; an explicit body
always wins, so this never overrides an intentional per-run choice.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from dswarm.core.runtime_env import is_web_container
from apps.web.llm_providers import clean_llm_providers
from dswarm.solver.worker_profiles import (
    DEFAULT_WORKER_IMAGE,
    VALID_BASE_ENGINES,
    base_engine_for_profile,
    direction_account_id,
    direction_image,
    normalize_profile_roster,
    normalize_worker_profiles,
    resolve_seat_ref,
)
from dswarm.solver.identity_model import (
    migrate_legacy_config,
    seats_to_legacy_profiles,
    is_legal_combo,
)

VALID_ENGINES = VALID_BASE_ENGINES
VALID_BACKENDS = ("local", "container")
DEFAULT_MAX_WORKERS = 8
DEFAULT_START_WORKERS = 1
DEFAULT_WORKER_BACKEND = "container"
DEFAULT_RACE_TIMEOUT = 720
DEFAULT_WALL_CLOCK_BUDGET = 0
DEFAULT_MAX_TOTAL_WORKERS = 0
DEFAULT_COST_BUDGET_USD = 0.0
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_LLM_PROVIDERS: list[dict[str, object]] = []

DEFAULT_REVIEW_POLICY = {
    "enabled": True,
    "engine": "pi-worker",
    "after_race": True,
    "after_fruitless_workers": 3,
    "after_duplicate_intents": 2,
    "on_course_correct": True,
    "on_reason_dry": True,
    "on_candidate_spike": True,
    "on_operator_hint": True,
    "every_completed_workers": 6,
    "candidate_spike_threshold": 5,
    "max_concurrent": 1,
    "allow_review_fallback": False,
    "cooldown_events": 8,
    "timeout": 420,
    "max_review_workers": 12,
}
DEFAULT_LLM_PROFILES = {
    "planner": {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "base_url": DEFAULT_DEEPSEEK_BASE_URL,
        "effort": "medium",
        "timeout": 120,
        "credential_source": "auto",
        "credential_account": "pi-main",
        "wire_api": "auto",
    },
    "titler": {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "base_url": DEFAULT_DEEPSEEK_BASE_URL,
        "effort": "low",
        "timeout": 60,
        "credential_source": "auto",
        "credential_account": "pi-main",
        "wire_api": "auto",
    },
}

DEFAULT_RUNTIME_PROFILES = [
    {"id": "local", "backend": "local", "label": "Local host"},
    {"id": "docker-web", "backend": "container", "label": "Docker web",
     "network": "bridge", "memory": "12g", "cpus": "4", "pids_limit": 2048},
    {"id": "docker-host-target", "backend": "container", "label": "Docker host target",
     "network": "host", "memory": "12g", "cpus": "4", "pids_limit": 2048},
    {"id": "docker-offline", "backend": "container", "label": "Docker offline",
     "network": "none", "memory": "12g", "cpus": "4", "pids_limit": 2048},
    {"id": "docker-pwn-heavy", "backend": "container", "label": "Docker pwn heavy",
     "network": "bridge", "memory": "24g", "cpus": "8", "pids_limit": 4096},
]
# Pi-only roster (route A): one engine (pi), seven DIRECTION profiles plus the
# generic pi-worker fallback. Each direction is a specialization hook: its own
# worker image (Kali base + direction config layer), prompt, skill extensions
# and (optionally) its own base_url / credential account. All inherit the
# shared pi-main account by default so an existing deployment keeps running;
# the operator can bind a per-direction account / base_url in settings later.
# The per-category override map routes each challenge to its direction profile;
# each routed direction needs a recon slot plus one focused explore/attack slot,
# and the global worker ceiling still limits total concurrency.
def _direction_profiles() -> list[dict[str, object]]:
    from dswarm.solver.worker_profiles import DIRECTIONS

    out: list[dict[str, object]] = []
    for direction in DIRECTIONS:
        out.append({
            "id": f"pi-{direction}", "name": f"pi-{direction}",
            "engine": "pi", "transport": "pi_cli",
            "auth": "api_key", "credential_mode": "api_key",
            "credential_account": direction_account_id(direction),
            "api_key_ref": "", "base_url": DEFAULT_DEEPSEEK_BASE_URL, "wire_api": "auto",
            "auth_mode": "bearer", "auth_header": "Authorization", "auth_prefix": "Bearer",
            "runtime": "docker-web",
            "roles": ["recon", "bootstrap", "explore", "respond", "review"],
            "image": direction_image(direction),
            "race": True, "max_running": 2, "max_review_running": 1,
            "priority": 20, "model": "deepseek-v4-flash",
            "effort": "medium", "enabled": False,
        })
    return out


DEFAULT_WORKER_PROFILES = [
    {"id": "pi-worker", "name": "pi-worker",
     "engine": "pi", "transport": "pi_cli",
     "auth": "api_key", "credential_mode": "api_key",
     "credential_account": "pi-main", "api_key_ref": "", "base_url": DEFAULT_DEEPSEEK_BASE_URL,
     "wire_api": "auto", "auth_mode": "bearer",
     "auth_header": "Authorization", "auth_prefix": "Bearer",
     "runtime": "docker-web", "roles": ["recon", "bootstrap", "explore", "respond", "review"],
     "image": DEFAULT_WORKER_IMAGE,
     "race": True, "max_running": 3, "max_review_running": 1, "priority": 10,
     "model": "deepseek-v4-flash", "effort": "medium",
     "enabled": False},
    *_direction_profiles(),
]
DEFAULT_ENGINES = [p["name"] for p in DEFAULT_WORKER_PROFILES]

# Per-category dispatch routing: each challenge category uses its direction
# profile (all plain pi today). A category without an entry falls back to the
# full DEFAULT_ENGINES roster.
DEFAULT_CATEGORY_OVERRIDES = {
    "web": ["pi-web"],
    "pwn": ["pi-pwn"],
    "reverse": ["pi-rev"],
    "crypto": ["pi-crypto"],
    "misc": ["pi-misc"],
    "forensics": ["pi-forensics"],
    "aisec": ["pi-aisec"],
}


def resolve_worker_backend(
    *,
    request_backend: Any = None,
    config_backend: Any = None,
    env_backend: Any = None,
    default_backend: str = DEFAULT_WORKER_BACKEND,
    in_web_container: bool,
) -> str:
    """THE single backend resolver. Every caller (dispatch precheck, settings
    health endpoints, config read/write) routes through this so they can never
    disagree on the effective backend — a disagreement was a false-green axis
    (settings evaluated `local` while dispatch force-containerized).

    Precedence: explicit request > stored config > env > default. Then:
      - `container_dockerexec` is the CONTAINER transport selector; it still means
        "container" for the backend choice, so normalize it.
      - anything not in VALID_BACKENDS falls back to `local`.
      - WEB-CONTAINER OVERRIDE (always applied, NOT optional): when this process
        runs inside a container, `local` would spawn a host-native CLI inside the
        web container (no tools, wrong creds). Force `container`. The override is
        unconditional precisely so settings and dispatch are identical.
    """
    backend = request_backend or config_backend or env_backend or default_backend
    if backend == "container_dockerexec":
        backend = "container"
    if backend not in VALID_BACKENDS:
        backend = "local"
    if backend == "local" and in_web_container:
        return "container"
    return backend


def backend_for_profile(
    profile: dict[str, Any],
    *,
    runtime_profiles: list[dict[str, Any]] | None,
    worker_backend: str,
    in_web_container: bool,
) -> str:
    """Effective backend for ONE profile. A profile names a `runtime` whose own
    `backend` (e.g. `docker-web` → container, `local` → local) takes precedence
    over the global `worker_backend`; the web-container override still applies on
    top via resolve_worker_backend. This is the per-profile resolution dispatch
    uses, so the settings page must use the SAME mapping or the badge can predict
    a different backend than the run actually uses.
    """
    runtime_by_id = {
        str(r.get("id")): r for r in (runtime_profiles or []) if isinstance(r, dict)
    }
    rt = runtime_by_id.get(str(profile.get("runtime") or ""))
    rt_backend = str((rt or {}).get("backend") or "") if rt else ""
    return resolve_worker_backend(
        config_backend=rt_backend or worker_backend,
        in_web_container=in_web_container,
    )


def _profile_kind(profile: dict[str, Any]) -> str:
    mode = str(
        profile.get("credential_mode") or profile.get("auth") or "subscription"
    ).strip()
    return "api" if mode in {"api", "api_key", "oauth_token"} else "sub"


def _canonical_profile_id(profile: dict[str, Any], backend: str) -> str:
    engine = str(profile.get("engine") or "").strip()
    if not engine:
        return str(profile.get("name") or profile.get("id") or "").strip()
    kind = _profile_kind(profile)
    if backend == "local":
        return f"{engine}-api-local" if kind == "api" else f"{engine}-local"
    return f"{engine}-{kind}-container"


def _canonical_profile_aliases(profile: dict[str, Any]) -> set[str]:
    return {
        _canonical_profile_id(profile, "local"),
        _canonical_profile_id(profile, "container"),
    }


def _clean_engines(value: Any, profiles: list[dict[str, Any]] | None = None) -> list[str]:
    """Filter to known profile names, expanding legacy base-engine names."""
    # Direction profile names (pi-web/pi-pwn/...) are REAL profiles now — do NOT
    # collapse them to pi-worker. Only the legacy base-engine "pi" expands (in
    # normalize_profile_roster) to every enabled pi profile.
    return normalize_profile_roster(value, profiles or DEFAULT_WORKER_PROFILES)


def _remap_profile_ref(ref: Any, profiles: list[dict[str, Any]], backend: str) -> Any:
    if not isinstance(ref, str) or backend not in VALID_BACKENDS:
        return ref
    by_name = {str(p.get("name") or p.get("id")): p for p in profiles}
    if ref in by_name:
        return ref
    for p in profiles:
        aliases = _canonical_profile_aliases(p)
        target = _canonical_profile_id(p, backend)
        if ref in aliases and target in by_name:
            return target
    return ref


def _remap_profile_refs(value: Any, profiles: list[dict[str, Any]], backend: str) -> Any:
    if isinstance(value, list):
        return [_remap_profile_ref(v, profiles, backend) for v in value]
    return _remap_profile_ref(value, profiles, backend)


def _clean_engines_for_backend(
    value: Any,
    profiles: list[dict[str, Any]],
    backend: str,
) -> list[str]:
    return _clean_engines(_remap_profile_refs(value, profiles, backend), profiles)


def _profile_name(profile: dict[str, Any]) -> str:
    return str(profile.get("name") or profile.get("id") or "").strip()


_AUTOMATIC_WORKER_LABELS = {
    "pi-worker",
    "pi-web",
    "pi-pwn",
    "pi-rev",
    "pi-crypto",
    "pi-misc",
    "pi-forensics",
    "pi-aisec",
}


def _automatic_worker_profile(profile: dict[str, Any]) -> bool:
    """Whether a profile may participate in future-run automatic routing.

    Advanced/custom Workers are retained for explicit manual spawn commands, but
    never enter the default roster merely because they are enabled.
    """
    label = str(
        profile.get("label") or profile.get("name") or profile.get("id") or ""
    ).strip().lower()
    return label in _AUTOMATIC_WORKER_LABELS


def _ordinary_worker_roles(profile: dict[str, Any]) -> set[str]:
    roles = profile.get("roles") or []
    return {
        str(r)
        for r in roles
        if str(r) in {"race", "bootstrap", "explore", "respond"}
    }


class WorkerConfigStore:
    def __init__(self, root: str | Path = "sessions") -> None:
        self._root = Path(root)
        self.path = self._root / "_worker_config.json"
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = raw
        except (json.JSONDecodeError, OSError):
            # a corrupt config must never break startup — fall back to defaults
            self._data = {}
        self._project_identity_to_legacy()

    def _project_identity_to_legacy(self) -> None:
        """If the on-disk config is NEW-shaped (seats/credentials/environments),
        adapt it into the legacy worker_profiles/runtime_profiles `self._data`
        carries, so the entire existing get() pipeline (5 foreign keys, backend
        remap, drivers) keeps working with ZERO change. Legacy-shaped configs are
        left untouched. Never raises."""
        d = self._data
        if not (isinstance(d.get("seats"), list) and d.get("seats")):
            return
        try:
            seats = [s for s in d["seats"] if isinstance(s, dict)]
            creds = [c for c in (d.get("credentials") or []) if isinstance(c, dict)]
            envs = [e for e in (d.get("environments") or []) if isinstance(e, dict)]
            # adapt seats → legacy worker_profiles for the scheduler/drivers.
            d["worker_profiles"] = seats_to_legacy_profiles(seats, creds, envs)
            # environments → runtime_profiles (same shape, just renamed concept).
            if envs:
                d["runtime_profiles"] = [
                    {k: v for k, v in {
                        "id": e.get("id"), "backend": e.get("backend"),
                        "label": e.get("label"), "network": e.get("network"),
                        "memory": e.get("memory"), "cpus": e.get("cpus"),
                        "pids_limit": e.get("pids_limit"),
                    }.items() if v not in (None, "", 0)}
                    for e in envs
                ]
            # remap any seat-id/label foreign keys (engines[], review.engine, ...)
            # to legacy profile names so the existing remap machinery resolves them.
            alias = {str(s.get("label")): str(s.get("id")) for s in seats if s.get("label")}
            id_to_name = {str(s.get("id")): str(s.get("id")) for s in seats}

            def _to_name(ref: Any) -> Any:
                sid = resolve_seat_ref(ref, seats=seats, alias_table=alias)
                return sid if sid in id_to_name else ref

            if isinstance(d.get("engines"), list):
                d["engines"] = [_to_name(r) for r in d["engines"]]
            # The dispatch lineup MUST track the seats' enabled toggles — that's
            # the only lineup control the seat UI exposes. A stale top-level
            # `engines` (e.g. left over from a legacy config, or a seat that was
            # since enabled/disabled) otherwise wins at get() (it short-circuits
            # the "else enabled seats" fallback), so enabling two more seats in
            # the UI left dispatch racing only the one stale engine. Reconcile:
            # the lineup is exactly the enabled seats, preserving the order of any
            # already named in `engines`, then appending newly-enabled ones.
            directional_routing = d.get("routing_mode") == "directional"
            enabled_ids = [
                str(s.get("id")) for s in seats
                if s.get("enabled", True) and s.get("id")
                and (not directional_routing or _automatic_worker_profile(s))
            ]
            enabled_set = set(enabled_ids)
            prior = [r for r in (d.get("engines") or []) if r in enabled_set]
            d["engines"] = prior + [sid for sid in enabled_ids if sid not in prior]
            if isinstance(d.get("overrides"), dict):
                remapped_overrides: dict[str, Any] = {}
                for cat, ov in d["overrides"].items():
                    if not isinstance(ov, dict):
                        remapped_overrides[str(cat)] = ov
                        continue
                    engines = ov.get("engines")
                    next_ov = dict(ov)
                    if isinstance(engines, list):
                        next_ov["engines"] = [_to_name(r) for r in engines]
                    remapped_overrides[str(cat)] = next_ov
                d["overrides"] = remapped_overrides
            sp = d.get("stage_policy")
            if isinstance(sp, dict):
                review = (sp.get("coordinator") or {}).get("review") if isinstance(sp.get("coordinator"), dict) else None
                if isinstance(review, dict) and review.get("engine"):
                    review["engine"] = _to_name(review["engine"])
        except Exception:  # noqa: BLE001 — projection must never break startup
            pass

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)  # atomic on POSIX

    def revision(self) -> str:
        """Stable non-secret revision for optimistic settings updates."""
        payload = json.dumps(
            self._data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:16]

    def raw_snapshot(self) -> dict[str, Any]:
        """Deep-copy the persisted config for request-level rollback."""
        return copy.deepcopy(self._data)

    def restore_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Restore a previously captured raw snapshot and refresh projections."""
        self._data = copy.deepcopy(snapshot) if isinstance(snapshot, dict) else {}
        self._project_identity_to_legacy()
        self._flush()
        return self.get()

    def _account_modes(self) -> dict[str, str]:
        """Map account_id → on-disk credential mode, so migration binds an empty
        profile to its real default account as engine_key (not host-inherit).
        Never raises — a missing/locked secrets store just yields {}."""
        try:
            from dswarm.solver.credential_accounts import (
                CredentialAccountStore, account_store_root,
            )
            store = CredentialAccountStore(account_store_root(self._root))
            return {a["account_id"]: str(a.get("mode") or "") for a in store.list()}
        except Exception:  # noqa: BLE001
            return {}

    def _custom_endpoint_accounts(self) -> dict[str, dict[str, str]]:
        """Return non-secret custom-endpoint account metadata keyed by account id.

        The credential account store is the UI's source of truth for base_url +
        target_engine. The scheduler/CLI drivers, however, still consume the flat
        legacy profile dict and only switch to EndpointDriver when profile.base_url
        is present. Keep that bridge here so account edits immediately affect both
        settings health checks and real dispatch without copying secrets into the
        worker config JSON.
        """
        try:
            from dswarm.solver.credential_accounts import (
                CredentialAccountStore, account_store_root,
            )
            store = CredentialAccountStore(account_store_root(self._root))
            out: dict[str, dict[str, str]] = {}
            for row in store.list():
                if not isinstance(row, dict):
                    continue
                if row.get("mode") != "custom_endpoint" or not row.get("present"):
                    continue
                details = row.get("details") if isinstance(row.get("details"), dict) else {}
                base_url = str(details.get("base_url_value") or "").strip()
                if not base_url:
                    continue
                account_id = str(row.get("account_id") or "").strip()
                if not account_id:
                    continue
                out[account_id] = {
                    "base_url": base_url,
                    "target_engine": str(
                        details.get("target_engine") or row.get("engine") or ""
                    ).strip().lower(),
                }
            return out
        except Exception:  # noqa: BLE001
            return {}

    def _hydrate_profiles_from_accounts(
        self,
        profiles: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Overlay account-store custom endpoint metadata onto worker profiles.

        This fixes the "settings account has BASE_URL but the worker still calls
        the default provider" class of bugs: the Pi provider override is driven
        by profile base_url, while the settings form stores base_url on the
        credential account. Explicit profile base_url still wins; the account
        store only fills the gap.
        """
        endpoints = self._custom_endpoint_accounts()
        if not endpoints:
            return profiles
        out: list[dict[str, Any]] = []
        for profile in profiles:
            p = dict(profile)
            engine = base_engine_for_profile(p)
            if engine not in VALID_BASE_ENGINES:
                out.append(p)
                continue
            explicit_account = str(p.get("credential_account") or "").strip()
            account_ids = [explicit_account] if explicit_account else [f"{engine}-main"]
            for account_id in account_ids:
                ep = endpoints.get(account_id)
                if not ep:
                    continue
                target = str(ep.get("target_engine") or "").strip().lower()
                # A legacy endpoint with no target marker may be used by an
                # explicitly-bound profile. Empty profile bindings only inherit
                # the engine's own default endpoint when the marker matches.
                if target and target != engine:
                    continue
                if not explicit_account and target != engine:
                    continue
                current_base = str(p.get("base_url") or "").strip().rstrip("/")
                default_base = DEFAULT_DEEPSEEK_BASE_URL.rstrip("/")
                if not current_base or current_base == default_base:
                    p["base_url"] = ep["base_url"]
                p["credential_account"] = account_id
                p["credential_mode"] = "api_key"
                p["auth"] = "api_key"
                break
            out.append(p)
        return out

    def identity_model(self) -> dict[str, Any]:
        """The NEW Credential/Seat/Environment view, authoritative when the on-disk
        config is already new-shaped. Never raises. (When legacy-shaped, callers
        should read get()['seats'/'credentials'/'environments'], which migrates.)"""
        d = self._data
        seats = [s for s in (d.get("seats") or []) if isinstance(s, dict)]
        creds = [c for c in (d.get("credentials") or []) if isinstance(c, dict)]
        envs = [e for e in (d.get("environments") or []) if isinstance(e, dict)]
        seat_alias = {str(s.get("label")): str(s.get("id")) for s in seats if s.get("label")}
        cred_alias = {str(c.get("secret_ref")): str(c.get("id"))
                      for c in creds if c.get("secret_ref")}
        return {
            "credentials": creds, "seats": seats, "environments": envs,
            "seat_alias": seat_alias, "credential_alias": cred_alias,
        }

    def get(self) -> dict[str, Any]:
        """The current default config with everything filled in (never raises)."""
        d = self._data
        runtime_profiles = self._clean_runtime_profiles(d.get("runtime_profiles"))
        worker_profiles = self._hydrate_profiles_from_accounts(
            self._clean_worker_profiles(d.get("worker_profiles"))
        )
        worker_backend = self._clean_backend(d.get("worker_backend"))
        directional_routing = d.get("routing_mode") == "directional"
        enabled_names = {
            _profile_name(p) for p in worker_profiles
            if p.get("enabled", True)
            and _ordinary_worker_roles(p)
            and (not directional_routing or _automatic_worker_profile(p))
        }
        raw_engines = d.get("engines")
        cleaned_engines = [
            ref for ref in _clean_engines_for_backend(
                raw_engines, worker_profiles, worker_backend
            )
            if ref in enabled_names
        ]
        if directional_routing and isinstance(raw_engines, list) and not raw_engines:
            engines = []
        else:
            engines = cleaned_engines or [
                _profile_name(p) for p in worker_profiles
                if _profile_name(p) in enabled_names
            ]
        start_workers = self._coerce_pos_int(d.get("start_workers"), DEFAULT_START_WORKERS)
        max_workers = self._coerce_pos_int(d.get("max_workers"), DEFAULT_MAX_WORKERS)
        wall_clock_budget = self._coerce_nonneg_int(
            d.get("wall_clock_budget"), DEFAULT_WALL_CLOCK_BUDGET)
        max_total_workers = self._coerce_nonneg_int(
            d.get("max_total_workers"), DEFAULT_MAX_TOTAL_WORKERS)
        cost_budget_usd = self._coerce_nonneg_float(
            d.get("cost_budget_usd"), DEFAULT_COST_BUDGET_USD)
        llm_profiles = self._clean_llm_profiles(d.get("llm_profiles"))
        llm_providers = self._clean_llm_providers(d.get("llm_providers"))
        raw_stage_policy = d.get("stage_policy")
        if isinstance(raw_stage_policy, dict):
            raw_stage_policy = json.loads(json.dumps(raw_stage_policy))
            review = raw_stage_policy.setdefault("coordinator", {}).setdefault("review", {})
            review["engine"] = _remap_profile_ref(
                review.get("engine") or DEFAULT_REVIEW_POLICY["engine"],
                worker_profiles,
                worker_backend,
            )
        stage_policy = self._clean_stage_policy(raw_stage_policy, {
            "wall_clock_budget": wall_clock_budget,
            "max_total_workers": max_total_workers,
            "cost_budget_usd": cost_budget_usd,
        })
        names = {str(p.get("name") or p.get("id")) for p in worker_profiles}
        review = stage_policy.setdefault("coordinator", {}).setdefault(
            "review", dict(DEFAULT_REVIEW_POLICY))
        review_engine = _remap_profile_ref(
            review.get("engine") or DEFAULT_REVIEW_POLICY["engine"],
            worker_profiles,
            worker_backend,
        )
        if review_engine not in names:
            review_engine = next(
                (
                    str(p.get("name") or p.get("id"))
                    for p in worker_profiles
                    if "review" in (p.get("roles") or [])
                ),
                engines[0] if engines else DEFAULT_REVIEW_POLICY["engine"],
            )
        review["engine"] = review_engine
        if review_engine not in enabled_names:
            # A disabled System Worker cannot be an active Review dispatcher.
            # Preserve the selected ref for future re-enable, but make the
            # effective fresh/default policy safe.
            review["enabled"] = False
        overrides: dict[str, Any] = {}
        raw_ov = d.get("overrides")
        if not isinstance(raw_ov, dict):
            # route A default: each category dispatches its direction profile.
            raw_ov = DEFAULT_CATEGORY_OVERRIDES
        if isinstance(raw_ov, dict):
            for cat, ov in raw_ov.items():
                # DEFAULT_CATEGORY_OVERRIDES are bare name lists; stored overrides
                # are {"engines": [...]} dicts — accept both shapes.
                if isinstance(ov, list):
                    ov = {"engines": ov}
                if not isinstance(ov, dict):
                    continue
                cat_engines = [
                    ref for ref in _clean_engines_for_backend(
                        ov.get("engines"), worker_profiles, worker_backend
                    )
                    if ref in enabled_names
                ]
                if not cat_engines:
                    continue
                overrides[str(cat)] = {
                    "engines": cat_engines,
                    "start_workers": self._coerce_pos_int(
                        ov.get("start_workers"), len(cat_engines)),
                }
        result = {
            "engines": engines,
            "start_workers": start_workers,
            "max_workers": max_workers,
            "worker_backend": worker_backend,
            "wall_clock_budget": wall_clock_budget,
            "max_total_workers": max_total_workers,
            "cost_budget_usd": cost_budget_usd,
            "stage_policy": stage_policy,
            "llm_profiles": llm_profiles,
            "llm_providers": llm_providers,
            "runtime_profiles": runtime_profiles,
            "worker_profiles": worker_profiles,
            "overrides": overrides,
        }
        # ── additive: attach the new Credential/Seat/Environment view (Phase A
        # iron rule — old fields above stay; new fields are added alongside so the
        # legacy frontend keeps working while the new UI can consume these). ──
        if isinstance(self._data.get("seats"), list) and self._data.get("seats"):
            ident = self.identity_model()
        else:
            res = migrate_legacy_config(
                worker_profiles=worker_profiles, runtime_profiles=runtime_profiles,
                account_modes=self._account_modes(),
            )
            ident = {
                "credentials": [c.to_dict() for c in res.credentials],
                "seats": [s.to_dict() for s in res.seats],
                "environments": [e.to_dict() for e in res.environments],
                "seat_alias": res.seat_alias,
                "credential_alias": res.credential_alias,
            }
        result["credentials"] = ident["credentials"]
        result["seats"] = ident["seats"]
        result["environments"] = ident["environments"]
        result["seat_alias"] = ident["seat_alias"]
        result["credential_alias"] = ident["credential_alias"]
        return result

    def resolve(self, category: Optional[str]) -> dict[str, Any]:
        """The effective roster for a challenge category — the per-category
        override (if any) layered over the defaults. Returns
        {engines, start_workers, max_workers}."""
        cfg = self.get()
        ov = cfg["overrides"].get((category or "").strip())
        if ov:
            # Category overrides narrow the roster to one direction profile. The
            # global fallback ceiling may still be 10 (or the sum of every enabled
            # profile), which lets the coordinator repeatedly try to dispatch past
            # that direction profile's max_running capacity. Derive the ceiling from
            # the effective category roster so bootstrap + explore share exactly the
            # configured seats and no bogus "no available worker profile" events are
            # emitted after the profile is full.
            selected = set(ov["engines"])
            eligible = [
                p for p in cfg["worker_profiles"]
                if _profile_name(p) in selected
                and p.get("enabled", True)
                and _ordinary_worker_roles(p)
            ]
            category_max_workers = sum(
                self._coerce_pos_int(p.get("max_running"), 1) for p in eligible
            ) or cfg["max_workers"]
            return {
                "engines": ov["engines"],
                "start_workers": min(ov["start_workers"], category_max_workers),
                "max_workers": category_max_workers,
                "worker_backend": cfg["worker_backend"],
                "wall_clock_budget": cfg["wall_clock_budget"],
                "max_total_workers": cfg["max_total_workers"],
                "cost_budget_usd": cfg["cost_budget_usd"],
                "stage_policy": cfg["stage_policy"],
                "llm_profiles": cfg["llm_profiles"],
                "llm_providers": cfg.get("llm_providers", []),
                "runtime_profiles": cfg["runtime_profiles"],
                "worker_profiles": cfg["worker_profiles"],
            }
        # Unknown/unclassified categories use only the hidden System Worker.
        # They must never fan out across every enabled direction/custom seat.
        generic = next((
            p for p in cfg["worker_profiles"]
            if str(p.get("label") or p.get("name") or p.get("id")) == "pi-worker"
            and p.get("enabled", True)
            and _ordinary_worker_roles(p)
        ), None)
        fallback_engines = [_profile_name(generic)] if generic else []
        fallback_max = (
            self._coerce_pos_int(generic.get("max_running"), 1) if generic else 0
        )
        return {
            "engines": fallback_engines,
            "start_workers": min(cfg["start_workers"], fallback_max) if fallback_max else 0,
            "max_workers": fallback_max,
            "worker_backend": cfg["worker_backend"],
            "wall_clock_budget": cfg["wall_clock_budget"],
            "max_total_workers": cfg["max_total_workers"],
            "cost_budget_usd": cfg["cost_budget_usd"],
            "stage_policy": cfg["stage_policy"],
            "llm_profiles": cfg["llm_profiles"],
            "llm_providers": cfg.get("llm_providers", []),
            "runtime_profiles": cfg["runtime_profiles"],
            "worker_profiles": cfg["worker_profiles"],
        }

    def set(
        self,
        *,
        engines: Any = None,
        start_workers: Any = None,
        max_workers: Any = None,
        worker_backend: Any = None,
        wall_clock_budget: Any = None,
        max_total_workers: Any = None,
        cost_budget_usd: Any = None,
        stage_policy: Any = None,
        llm_profiles: Any = None,
        llm_providers: Any = None,
        runtime_profiles: Any = None,
        worker_profiles: Any = None,
        overrides: Any = None,
        routing_mode: Any = None,
    ) -> dict[str, Any]:
        """Update the default config. Each arg is optional; only provided fields
        change. Invalid values are rejected (raise ValueError) so a bad PUT
        doesn't silently persist garbage."""
        target_backend = (
            self._require_backend(worker_backend)
            if worker_backend is not None
            else self._clean_backend(self._data.get("worker_backend"))
        )
        if routing_mode is not None:
            mode = str(routing_mode).strip().lower()
            if mode != "directional":
                raise ValueError("routing_mode must be 'directional'")
            self._data["routing_mode"] = mode
        if engines is not None:
            profiles_for_engine_validation = (
                self._clean_worker_profiles(worker_profiles, reject_invalid=True)
                if worker_profiles is not None
                else self._clean_worker_profiles(self._data.get("worker_profiles"))
            )
            cleaned = _clean_engines_for_backend(
                engines, profiles_for_engine_validation, target_backend)
            explicit_empty = isinstance(engines, list) and not engines
            if not cleaned and not explicit_empty:
                raise ValueError("engines must name at least one enabled worker profile")
            self._data["engines"] = cleaned
        if start_workers is not None:
            self._data["start_workers"] = self._require_pos_int(
                start_workers, "start_workers")
        if max_workers is not None:
            self._data["max_workers"] = self._require_pos_int(
                max_workers, "max_workers")
        if worker_backend is not None:
            self._data["worker_backend"] = target_backend
        if wall_clock_budget is not None:
            self._data["wall_clock_budget"] = self._require_nonneg_int(
                wall_clock_budget, "wall_clock_budget")
        if max_total_workers is not None:
            self._data["max_total_workers"] = self._require_nonneg_int(
                max_total_workers, "max_total_workers")
        if cost_budget_usd is not None:
            self._data["cost_budget_usd"] = self._require_nonneg_float(
                cost_budget_usd, "cost_budget_usd")
        if stage_policy is not None:
            profiles_for_stage = self._clean_worker_profiles(
                worker_profiles if worker_profiles is not None else self._data.get("worker_profiles"))
            clean_stage = (
                json.loads(json.dumps(stage_policy))
                if isinstance(stage_policy, dict)
                else stage_policy
            )
            if isinstance(clean_stage, dict):
                review = clean_stage.setdefault("coordinator", {}).setdefault("review", {})
                review["engine"] = _remap_profile_ref(
                    review.get("engine") or DEFAULT_REVIEW_POLICY["engine"],
                    profiles_for_stage,
                    target_backend,
                )
            self._data["stage_policy"] = self._clean_stage_policy(clean_stage, {})
        if llm_profiles is not None:
            self._data["llm_profiles"] = self._clean_llm_profiles(
                llm_profiles, reject_invalid=True)
        if llm_providers is not None:
            self._data["llm_providers"] = self._clean_llm_providers(
                llm_providers, reject_invalid=True)
        if runtime_profiles is not None or worker_profiles is not None:
            next_runtime_profiles = (
                self._clean_runtime_profiles(runtime_profiles, reject_invalid=True)
                if runtime_profiles is not None
                else self._clean_runtime_profiles(self._data.get("runtime_profiles"))
            )
            next_worker_profiles = (
                self._clean_worker_profiles(worker_profiles, reject_invalid=True)
                if worker_profiles is not None
                else self._clean_worker_profiles(self._data.get("worker_profiles"))
            )
            runtime_ids = {p["id"] for p in next_runtime_profiles}
            for p in next_worker_profiles:
                if p["runtime"] not in runtime_ids:
                    raise ValueError(f"worker profile {p['id']} references unknown runtime")
            if runtime_profiles is not None:
                self._data["runtime_profiles"] = next_runtime_profiles
            if worker_profiles is not None:
                self._data["worker_profiles"] = next_worker_profiles
        if overrides is not None:
            if not isinstance(overrides, dict):
                raise ValueError("overrides must be an object")
            clean_ov: dict[str, Any] = {}
            for cat, ov in overrides.items():
                if not isinstance(ov, dict):
                    raise ValueError(f"override for {cat} must be an object")
                cat_engines = _clean_engines(
                    ov.get("engines"),
                    self._clean_worker_profiles(self._data.get("worker_profiles")),
                )
                if not cat_engines:
                    raise ValueError(f"override for {cat} must name valid worker profiles")
                entry: dict[str, Any] = {"engines": cat_engines}
                if ov.get("start_workers") is not None:
                    entry["start_workers"] = self._require_pos_int(
                        ov["start_workers"], f"{cat}.start_workers")
                clean_ov[str(cat)] = entry
            self._data["overrides"] = clean_ov
        # max_workers is a READ-ONLY derived value = sum of the eligible seats'
        # max_running. Recompute it whenever the roster (per-seat capacity) or the
        # dispatch lineup could have changed — i.e. worker_profiles or engines were
        # supplied. (The frontend no longer sends an editable max_workers; a stale
        # one in the payload is overwritten by the derived sum.) We deliberately do
        # NOT mutate any seat's max_running, so an edited value never "reverts".
        self._sync_worker_counts(
            link_profile_capacity=(
                worker_profiles is not None or engines is not None
            )
        )
        # New-schema-on-disk (user decision): whenever the legacy worker_profiles
        # change (the v2 frontend still saves in legacy shape), derive and persist
        # the Credential/Seat/Environment model alongside, so disk carries the new
        # shape as the source of truth. Reads then prefer the seats[] block.
        if worker_profiles is not None or runtime_profiles is not None:
            self._persist_identity_from_legacy()
        self._flush()
        return self.get()

    def _persist_identity_from_legacy(self) -> None:
        """Derive seats/credentials/environments from the current legacy
        worker_profiles + runtime_profiles and write them into self._data, so the
        on-disk config is the new shape. Never raises — a derivation failure just
        leaves the legacy shape (still readable)."""
        try:
            # preserve any labels the user already set on existing seats (the
            # legacy save path drops the label field, so re-deriving would reset
            # them to "<engine> worker"); keyed by the stable seat id.
            prior_labels = {
                str(s.get("id")): str(s.get("label") or "")
                for s in (self._data.get("seats") or []) if isinstance(s, dict)
            }
            cfg = self.get()  # normalized legacy view
            res = migrate_legacy_config(
                worker_profiles=cfg["worker_profiles"],
                runtime_profiles=cfg["runtime_profiles"],
                account_modes=self._account_modes(),
            )
            seats = []
            for s in res.seats:
                d = s.to_dict()
                if prior_labels.get(d["id"]):
                    d["label"] = prior_labels[d["id"]]
                seats.append(d)
            self._data["seats"] = seats
            self._data["credentials"] = [c.to_dict() for c in res.credentials]
            self._data["environments"] = [e.to_dict() for e in res.environments]
            # The seats[] block is additive; we leave the legacy engines[]/
            # review.engine foreign keys in their current (readable) form rather than
            # rewriting them to seat ids on every save. Rationale: the legacy
            # set_runtime_environment recipe path renames profiles by readable
            # canonical id, and churning foreign keys to seat ids here would collide
            # with it. resolve_seat_ref() bridges either form at the read boundaries
            # (health route, scheduler), and _project_identity_to_legacy reconciles a
            # new-shaped file on load. Stable seat ids still live in seats[].
        except Exception:  # noqa: BLE001
            pass

    def set_identity_model(
        self,
        *,
        seats: Any = None,
        credentials: Any = None,
        environments: Any = None,
    ) -> dict[str, Any]:
        """Persist the NEW Credential/Seat/Environment model to disk.

        Validates the §3.7 hard constraint (container environment forbids a
        system_inherit credential) and rejects an illegal combo with ValueError —
        the save-time gate Codex specified, so an illegal config never persists.
        After writing, re-projects to legacy worker_profiles so the in-memory
        scheduler view stays consistent. Each arg optional; only provided ones
        change. Never silently drops a bad value — it raises."""
        if seats is not None:
            if not isinstance(seats, list):
                raise ValueError("seats must be a list")
            self._data["seats"] = [s for s in seats if isinstance(s, dict)]
        if credentials is not None:
            if not isinstance(credentials, list):
                raise ValueError("credentials must be a list")
            self._data["credentials"] = [c for c in credentials if isinstance(c, dict)]
        if environments is not None:
            if not isinstance(environments, list):
                raise ValueError("environments must be a list")
            self._data["environments"] = [e for e in environments if isinstance(e, dict)]

        # §3.7 legality gate: a seat on a container environment may not use a
        # system_inherit credential (host login isn't mounted into the container).
        cred_by_id = {str(c.get("id")): c for c in self._data.get("credentials") or []}
        env_by_id = {str(e.get("id")): e for e in self._data.get("environments") or []}
        for s in self._data.get("seats") or []:
            cred = cred_by_id.get(str(s.get("credential_id"))) or {}
            env = env_by_id.get(str(s.get("environment_id"))) or {}
            kind = str(cred.get("kind") or "")
            backend = str(env.get("backend") or "")
            if kind and backend and not is_legal_combo(kind=kind, backend=backend):
                label = s.get("label") or s.get("id")
                raise ValueError(
                    f"非法组合:Agent「{label}」在容器环境下使用了「系统登录」凭据。"
                    f"容器不挂载宿主登录态,请改用引擎凭据或自定义端点。"
                )
        # keep the legacy projection in sync so get()/scheduler see the change.
        self._project_identity_to_legacy()
        self._flush()
        return self.get()

    def _sync_worker_counts(self, *, link_profile_capacity: bool) -> None:
        # Direction is roster→max (the operator owns per-seat capacity; the global
        # `max_workers` ceiling is a READ-ONLY derived value = sum of the eligible
        # seats' `max_running`). We NEVER mutate a seat's max_running here — that
        # is what ballooned a stale single-seat lineup up to max_workers and made
        # an edited value "revert" on save (Bug B). Instead max_workers tracks the
        # roster sum, up AND down, so "3 workers each running 1 → max 3" always
        # holds and editing any seat is reflected immediately.
        if link_profile_capacity:
            profiles = self._clean_worker_profiles(self._data.get("worker_profiles"))
            backend = self._clean_backend(self._data.get("worker_backend"))
            directional_routing = self._data.get("routing_mode") == "directional"
            selected = _clean_engines_for_backend(
                self._data.get("engines"), profiles, backend) or [
                    _profile_name(p) for p in profiles
                    if p.get("enabled", True)
                    and (not directional_routing or _automatic_worker_profile(p))
                ]
            selected_set = set(selected)
            eligible = [
                p for p in profiles
                if _profile_name(p) in selected_set and _ordinary_worker_roles(p)
            ]
            if eligible:
                self._data["max_workers"] = sum(
                    self._coerce_pos_int(p.get("max_running"), 1) for p in eligible)

        # start_workers is still capped by the (possibly just-derived) ceiling.
        max_workers = self._coerce_pos_int(
            self._data.get("max_workers"), DEFAULT_MAX_WORKERS)
        start_workers = self._coerce_pos_int(
            self._data.get("start_workers"), DEFAULT_START_WORKERS)
        if start_workers > max_workers:
            self._data["start_workers"] = max_workers

    def set_runtime_environment(self, *, backend: str, runtime_id: str) -> dict[str, Any]:
        """Unify the run's runtime across ALL enabled worker profiles (DESIGN §5).

        Since the model is one-container-per-run, every profile that could be
        dispatched (default engines OR a per-category override's engines — i.e.
        the whole enabled set) must agree on the runtime, else the old "first
        worker's runtime wins, displayed backend lies" bug returns. So we set
        `worker_backend` AND rewrite every enabled profile's `runtime` to the
        chosen id in one atomic flush.
        """
        backend = (backend or "").strip()
        runtime_id = (runtime_id or "").strip()
        if backend not in VALID_BACKENDS:
            raise ValueError("backend must be 'local' or 'container'")
        runtime_profiles = self._clean_runtime_profiles(self._data.get("runtime_profiles"))
        rt = next((r for r in runtime_profiles if r["id"] == runtime_id), None)
        if rt is None:
            raise ValueError(f"unknown runtime id: {runtime_id}")
        if rt["backend"] != backend:
            raise ValueError(
                f"runtime {runtime_id!r} is backend {rt['backend']!r}, not {backend!r}")
        profiles = self._clean_worker_profiles(self._data.get("worker_profiles"))
        rename: dict[str, str] = {}
        taken = {str(p.get("name") or p.get("id")) for p in profiles}
        for p in profiles:
            old_id = str(p["id"])
            desired = _canonical_profile_id(p, backend)
            if old_id in _canonical_profile_aliases(p) and old_id != desired:
                if desired not in taken:
                    taken.discard(old_id)
                    taken.add(desired)
                    rename[old_id] = desired
                    p["id"] = desired
                    p["name"] = desired
            p["runtime"] = runtime_id  # whole enabled set, incl. override-only ones

        def rewrite_ref(value: Any) -> Any:
            if isinstance(value, str):
                return rename.get(value, _remap_profile_ref(value, profiles, backend))
            if isinstance(value, list):
                return [rewrite_ref(v) for v in value]
            return value

        if "engines" in self._data:
            self._data["engines"] = rewrite_ref(self._data.get("engines"))
        raw_stage = self._data.get("stage_policy")
        stage = json.loads(json.dumps(raw_stage)) if isinstance(raw_stage, dict) else {}
        coord = stage.setdefault("coordinator", {})
        review = coord.setdefault("review", {})
        review["engine"] = rewrite_ref(
            review.get("engine") or DEFAULT_REVIEW_POLICY["engine"])
        self._data["stage_policy"] = stage

        raw_overrides = self._data.get("overrides")
        if isinstance(raw_overrides, dict):
            overrides = json.loads(json.dumps(raw_overrides))
            for ov in overrides.values():
                if isinstance(ov, dict) and ov.get("engines") is not None:
                    ov["engines"] = rewrite_ref(ov.get("engines"))
            self._data["overrides"] = overrides

        self._data["worker_backend"] = backend
        self._data["worker_profiles"] = profiles
        self._flush()
        return self.get()

    @staticmethod
    def _coerce_pos_int(value: Any, default: int) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return default
        return n if n > 0 else default

    @staticmethod
    def _coerce_nonneg_int(value: Any, default: int) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError):
            return default
        return n if n >= 0 else default

    @staticmethod
    def _coerce_nonneg_float(value: Any, default: float) -> float:
        try:
            n = float(value)
        except (TypeError, ValueError):
            return default
        return n if n >= 0 else default

    @staticmethod
    def _coerce_bool(value: Any, default: bool) -> bool:
        if value is None:
            return default
        return bool(value)

    @staticmethod
    def _require_pos_int(value: Any, field: str) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be a positive integer") from exc
        if n <= 0:
            raise ValueError(f"{field} must be a positive integer")
        return n

    @staticmethod
    def _require_nonneg_int(value: Any, field: str) -> int:
        try:
            n = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be a non-negative integer") from exc
        if n < 0:
            raise ValueError(f"{field} must be a non-negative integer")
        return n

    @staticmethod
    def _require_nonneg_float(value: Any, field: str) -> float:
        try:
            n = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} must be a non-negative number") from exc
        if n < 0:
            raise ValueError(f"{field} must be a non-negative number")
        return n

    @staticmethod
    def _clean_llm_profiles(value: Any, *, reject_invalid: bool = False) -> dict[str, dict[str, Any]]:
        if value is None:
            return {k: dict(v) for k, v in DEFAULT_LLM_PROFILES.items()}
        if not isinstance(value, dict):
            if reject_invalid:
                raise ValueError("llm_profiles must be an object")
            return {k: dict(v) for k, v in DEFAULT_LLM_PROFILES.items()}
        out = {k: dict(v) for k, v in DEFAULT_LLM_PROFILES.items()}
        for key in ("planner", "titler"):
            raw = value.get(key)
            if raw is None:
                continue
            if not isinstance(raw, dict):
                if reject_invalid:
                    raise ValueError(f"llm_profiles.{key} must be an object")
                continue
            model = str(raw.get("model") or out[key]["model"]).strip()
            provider = str(raw.get("provider") or out[key]["provider"]).strip()
            # base_url is visible and explicit in the UI. Empty/non-string values
            # restore the DeepSeek OpenAI-compatible default; the API key is never
            # stored here (env/account store remains authoritative for secrets).
            raw_base = raw.get("base_url")
            base_url = str(raw_base).strip() if isinstance(raw_base, str) else ""
            provider_ref = str(raw.get("provider_ref") or out[key].get("provider_ref") or "").strip()
            if provider_ref:
                # Provider registry owns endpoint, auth and protocol. Keep the
                # legacy fields only as non-secret compatibility markers.
                base_url = ""
            elif not base_url:
                base_url = DEFAULT_DEEPSEEK_BASE_URL
            effort = str(raw.get("effort") or out[key].get("effort") or "medium").strip().lower()
            timeout = WorkerConfigStore._coerce_nonneg_int(raw.get("timeout"), int(out[key].get("timeout") or 0))
            if not model:
                if reject_invalid:
                    raise ValueError(f"llm_profiles.{key}.model must be non-empty")
                model = str(out[key]["model"])
            credential_source = str(raw.get("credential_source") or out[key].get("credential_source") or "auto").strip().lower()
            if credential_source not in {"auto", "env", "account", "provider"}:
                credential_source = "auto"
            credential_account = str(raw.get("credential_account") or out[key].get("credential_account") or "pi-main").strip()
            wire_api = str(raw.get("wire_api") or out[key].get("wire_api") or "auto").strip().lower()
            if wire_api not in {"auto", "openai", "openai-chat", "openai-responses"}:
                wire_api = "auto"
            if provider_ref:
                credential_source = "provider"
                credential_account = provider_ref
                wire_api = "auto"
            out[key] = {
                "provider": provider or out[key]["provider"],
                "model": model,
                "base_url": base_url,
                "effort": effort or str(out[key].get("effort") or "medium"),
                "timeout": timeout,
                "credential_source": credential_source,
                "credential_account": credential_account,
                "wire_api": wire_api,
                "provider_ref": provider_ref,
            }
        return out


    @staticmethod
    def _clean_llm_providers(value: Any, *, reject_invalid: bool = False) -> list[dict[str, Any]]:
        if value is None:
            return [dict(p) for p in DEFAULT_LLM_PROVIDERS]
        return clean_llm_providers(value, reject_invalid=reject_invalid)

    @staticmethod
    def _clean_stage_policy(value: Any, defaults: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            value = {}
        wall = int((value.get("coordinator") or {}).get(
            "wall_clock_budget", defaults.get("wall_clock_budget", 0)) or 0)
        max_workers = int(value.get("budgets", {}).get(
            "max_total_workers", defaults.get("max_total_workers", 0)) or 0)
        cost = float(value.get("budgets", {}).get(
            "cost_budget_usd", defaults.get("cost_budget_usd", 0.0)) or 0.0)
        raw_review = (value.get("coordinator") or {}).get("review")
        review = dict(DEFAULT_REVIEW_POLICY)
        if isinstance(raw_review, dict):
            review["enabled"] = bool(raw_review.get("enabled", review["enabled"]))
            review["engine"] = str(raw_review.get("engine") or review["engine"]).strip()
            for key in ("provider_ref", "credential_source", "credential_account", "base_url", "wire_api", "model"):
                if raw_review.get(key) is not None:
                    review[key] = str(raw_review.get(key) or "").strip()
            if review.get("provider_ref"):
                review["credential_source"] = "provider"
                review["credential_account"] = review["provider_ref"]
                review["base_url"] = ""
                review["wire_api"] = "auto"
            for key in (
                "after_fruitless_workers", "after_duplicate_intents",
                "every_completed_workers", "candidate_spike_threshold",
                "max_concurrent", "cooldown_events", "timeout", "max_review_workers",
            ):
                if raw_review.get(key) is not None:
                    review[key] = WorkerConfigStore._coerce_nonneg_int(
                        raw_review.get(key), int(review[key]))
            for key in (
                "after_race", "on_course_correct", "on_reason_dry",
                "on_candidate_spike", "on_operator_hint", "allow_review_fallback",
            ):
                if raw_review.get(key) is not None:
                    review[key] = bool(raw_review.get(key))
        return {
            "prepare": dict(value.get("prepare") or {}),
            "coordinator": {"wall_clock_budget": wall, "review": review},
            "budgets": {"max_total_workers": max_workers,
                        "cost_budget_usd": cost},
        }

    @staticmethod
    def _clean_backend(value: Any) -> str:
        # Single source of truth for the effective backend (precedence + alias +
        # fallback + the web-container override that coerces local→container so a
        # stale/explicit "local" never reaches the swarm). No-op on a bare host.
        return resolve_worker_backend(
            config_backend=value if isinstance(value, str) else None,
            in_web_container=is_web_container(),
        )

    @staticmethod
    def _require_backend(value: Any) -> str:
        if isinstance(value, str) and value in VALID_BACKENDS:
            if value == "local" and is_web_container():
                raise ValueError(
                    "worker_backend 'local' is not allowed when the web control "
                    "plane runs inside a container — use 'container'")
            return value
        raise ValueError("worker_backend must be local or container")

    @staticmethod
    def _clean_runtime_profiles(value: Any, *, reject_invalid: bool = False) -> list[dict[str, Any]]:
        if value is None:
            return [dict(p) for p in DEFAULT_RUNTIME_PROFILES]
        if not isinstance(value, list):
            if reject_invalid:
                raise ValueError("runtime_profiles must be a list")
            return [dict(p) for p in DEFAULT_RUNTIME_PROFILES]
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, dict):
                if reject_invalid:
                    raise ValueError("runtime profile must be an object")
                continue
            pid = str(item.get("id") or "").strip()
            backend = item.get("backend")
            if not pid or backend not in VALID_BACKENDS:
                if reject_invalid:
                    raise ValueError("runtime profile requires id and valid backend")
                continue
            out.append({
                "id": pid,
                "backend": backend,
                "label": str(item.get("label") or pid),
                "network": str(item.get("network") or ("bridge" if backend == "container" else "")),
                "memory": str(item.get("memory") or ""),
                "cpus": str(item.get("cpus") or ""),
                "pids_limit": WorkerConfigStore._coerce_nonneg_int(item.get("pids_limit"), 0),
            })
        return out or [dict(p) for p in DEFAULT_RUNTIME_PROFILES]

    @staticmethod
    def _clean_worker_profiles(value: Any, *, reject_invalid: bool = False) -> list[dict[str, Any]]:
        return normalize_worker_profiles(
            value,
            defaults=DEFAULT_WORKER_PROFILES,
            reject_invalid=reject_invalid,
        )
