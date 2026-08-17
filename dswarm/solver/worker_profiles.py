"""WorkerProfile normalization shared by the web config and swarm scheduler.

Profiles are the scheduling unit.  ``profile["name"]`` is what the coordinator
selects; ``profile["engine"]`` is the concrete CLI transport family.
"""

from __future__ import annotations

from typing import Any, Mapping

from dswarm.solver.direction_rules import DEFAULT_DIRECTION_REGISTRY


VALID_BASE_ENGINES = ("pi",)
DEFAULT_WORKER_IMAGE = "ctf-swarm-pi:0.2.0"
TRANSPORT_TO_ENGINE = {
    "pi": "pi",
    "pi_cli": "pi",
}
DEFAULT_ROLES = ["recon", "bootstrap", "explore", "respond", "review"]

# ── direction profiles (route A) ─────────────────────────────────────────────
# One pi engine, seven DIRECTION profiles. Each direction is a specialization
# hook: its own worker image (Kali base + direction config layer), prompt, skill
# extensions and (optionally) its own base_url / credential account. The
# `reverse` challenge category maps to the `rev` direction image tag.
DIRECTIONS = DEFAULT_DIRECTION_REGISTRY.directions
DIRECTION_PROFILE = {
    name: DEFAULT_DIRECTION_REGISTRY.profile_for(name)
    for name in DIRECTIONS
}
for _spec in (
    DEFAULT_DIRECTION_REGISTRY.spec_for(name) for name in DIRECTIONS
):
    if _spec:
        DIRECTION_PROFILE.update({alias: _spec.profile for alias in _spec.aliases})


# Route A ships a generic pi base plus one direction-tagged worker image per
# category. The base tag remains the fallback for the generic pi-worker profile.
_DIRECTION_IMAGE_TAG = {
    "web": "ctf-swarm-pi-web:0.2.0",
    "pwn": "ctf-swarm-pi-pwn:0.2.0",
    "rev": "ctf-swarm-pi-rev:0.2.0",
    "crypto": "ctf-swarm-pi-crypto:0.2.0",
    "misc": "ctf-swarm-pi-misc:0.2.0",
    "forensics": "ctf-swarm-pi-forensics:0.2.0",
    "aisec": "ctf-swarm-pi-aisec:0.2.0",
}


def direction_profile_name(direction: str) -> str:
    """Map a direction/category value to its default worker profile id."""
    canonical, _resolution = DEFAULT_DIRECTION_REGISTRY.canonicalize(direction)
    return DEFAULT_DIRECTION_REGISTRY.profile_for(canonical)


def canonical_direction(value: Any) -> str:
    """Normalize a direction/category value through the shared registry."""
    canonical, _resolution = DEFAULT_DIRECTION_REGISTRY.canonicalize(value)
    return canonical


def direction_image(direction: str) -> str:
    """The worker image tag for a direction, resolved after canonicalization."""
    canonical = canonical_direction(direction)
    return _DIRECTION_IMAGE_TAG.get(canonical, "")


def direction_account_id(direction: str) -> str:
    """Default credential account id for one direction profile."""
    return f"pi-{(direction or '').strip().lower()}-main"


def coerce_nonneg_int(value: Any, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n >= 0 else default


def coerce_pos_int(value: Any, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def base_engine_for_profile(profile_or_name: Any) -> str:
    """Resolve a profile dict OR a bare string to a BASE engine (pi).

    A bare string may be a base engine ("pi"), a transport ("pi_cli"), or a
    PROFILE ID ("pi-sub-container"). Profile ids are "<base>-<suffix>", so when a
    string is neither a known base nor transport we recover the base from its segments
    (the first segment that is a valid base engine). This is what keeps a profile id
    from being passed straight to DRIVERS[...] (→ KeyError) downstream. The original
    string is returned only when nothing resolves, so callers can still error clearly.
    """
    if isinstance(profile_or_name, dict):
        transport = str(profile_or_name.get("transport") or "").strip()
        engine = str(profile_or_name.get("engine") or "").strip()
        return TRANSPORT_TO_ENGINE.get(transport, engine)
    s = str(profile_or_name or "").strip()
    if s in TRANSPORT_TO_ENGINE:
        return TRANSPORT_TO_ENGINE[s]
    if s in VALID_BASE_ENGINES:
        return s
    # profile id like "codex-sub-container" / "cursor-api-container" → recover base.
    for seg in s.split("-"):
        if seg in VALID_BASE_ENGINES:
            return seg
        if seg in TRANSPORT_TO_ENGINE:
            return TRANSPORT_TO_ENGINE[seg]
    return s


def normalize_worker_profile(item: dict[str, Any], *, reject_invalid: bool = False) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        if reject_invalid:
            raise ValueError("worker profile must be an object")
        return None
    enabled = bool(item.get("enabled", True))
    transport = str(item.get("transport") or item.get("engine") or "").strip()
    engine = TRANSPORT_TO_ENGINE.get(transport, str(item.get("engine") or "").strip())
    if engine not in VALID_BASE_ENGINES:
        if reject_invalid:
            raise ValueError("worker profile requires valid transport/engine")
        return None
    pid = str(item.get("name") or item.get("id") or "").strip()
    if not pid:
        if reject_invalid:
            raise ValueError("worker profile requires name or id")
        return None
    raw_roles = item.get("roles")
    roles = [
        str(r).strip()
        for r in raw_roles
        if isinstance(r, str) and str(r).strip()
    ] if isinstance(raw_roles, list) else []
    if not roles:
        roles = list(DEFAULT_ROLES)
    elif "review" not in roles and any(
        r in roles for r in ("recon", "bootstrap", "explore", "respond")
    ):
        # Compatibility migration for profiles saved before the review-arbiter
        # role existed: execution-capable profiles should be selectable as the
        # single core review worker unless the operator made a non-execution-only
        # profile on purpose.
        roles = [*roles, "review"]
    credential_mode = str(
        item.get("credential_mode") or item.get("auth") or "subscription"
    ).strip() or "subscription"
    if "credential_account" in item:
        raw_account = item.get("credential_account")
    elif "credential_account_ref" in item:
        raw_account = item.get("credential_account_ref")
    else:
        raw_account = f"{engine}-main"
    credential_account = str(raw_account or "").strip()
    provider_ref = str(item.get("provider_ref") or "").strip()
    # A provider binding owns the endpoint, protocol and secret.  Keep only the
    # provider reference so a stale legacy credential_account/base_url cannot
    # shadow or double-bind the profile.
    if provider_ref:
        credential_account = ""
        base_url = ""
        api_key_ref = ""
        wire_api = "auto"
        auth_mode = "bearer"
        auth_header = "Authorization"
        auth_prefix = "Bearer"
    else:
        base_url = str(item.get("base_url") or "").strip()
        api_key_ref = str(item.get("api_key_ref") or "").strip()
        wire_api = str(item.get("wire_api") or "auto").strip().lower()
        auth_mode = str(item.get("auth_mode") or "bearer").strip().lower()
        auth_header = str(item.get("auth_header") or "Authorization").strip()
        auth_prefix = str(item.get("auth_prefix") if item.get("auth_prefix") is not None else "Bearer").strip()
    normalized = {
        "id": pid,
        "name": pid,
        # human-readable display name, carried through so a seat-id-based pid (post
        # identity migration) still renders a friendly name in the UI. Defaults to
        # the pid when no explicit label is given.
        "label": str(item.get("label") or pid).strip(),
        "engine": engine,
        "transport": transport or engine,
        "credential_mode": credential_mode,
        "auth": credential_mode,
        "credential_account": credential_account,
        "api_key_ref": api_key_ref,
        "provider_ref": provider_ref,
        "base_url": base_url,
        "wire_api": wire_api,
        "auth_mode": auth_mode,
        "auth_header": auth_header,
        "auth_prefix": auth_prefix,
        "runtime": str(item.get("runtime") or "docker-web").strip(),
        "image": _normalize_worker_image(item.get("image")),
        "roles": roles,
        "race": bool(item.get("race", "race" in roles)),
        "max_running": coerce_pos_int(item.get("max_running"), 1),
        # 0 means "inherit the global review.max_concurrent"; review capacity is
        # intentionally separate from max_running, which now only gates ordinary
        # race/bootstrap/explore/respond workers.
        "max_review_running": coerce_nonneg_int(item.get("max_review_running"), 0),
        "priority": coerce_nonneg_int(item.get("priority"), 100),
        "model": str(item.get("model") or "").strip(),
        "effort": str(item.get("effort") or "").strip().lower(),
        "enabled": enabled,
    }
    return normalized


def _normalize_worker_image(raw: Any) -> str:
    image = str(raw or "").strip()
    if not image:
        return DEFAULT_WORKER_IMAGE
    # Historical migration only: the FIRST per-category images
    # (ctf-swarm-pi-<cat>:0.1.0) were superseded by the unified image. Explicit
    # per-direction tags (ctf-swarm-pi-<dir>:<ver>) are preserved so the
    # direction-isolation images stay selectable.
    if image.startswith("ctf-swarm-pi") and ":0.1.0" in image:
        return DEFAULT_WORKER_IMAGE
    return image


def normalize_worker_profiles(value: Any, *, defaults: list[dict[str, Any]] | None = None,
                              reject_invalid: bool = False) -> list[dict[str, Any]]:
    if value is None:
        return [dict(p) for p in (defaults or [])]
    if not isinstance(value, list):
        if reject_invalid:
            raise ValueError("worker_profiles must be a list")
        return [dict(p) for p in (defaults or [])]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        profile = normalize_worker_profile(item, reject_invalid=reject_invalid)
        if profile is None:
            continue
        if profile["name"] in seen:
            if reject_invalid:
                raise ValueError("worker profile names must be unique")
            continue
        seen.add(profile["name"])
        out.append(profile)
    return out or [dict(p) for p in (defaults or [])]


def profile_names(profiles: list[dict[str, Any]]) -> list[str]:
    return [str(p["name"]) for p in profiles if p.get("enabled", True)]


def profile_label(profile: Mapping[str, Any]) -> str:
    """Human-facing worker label: explicit label, name, or id."""
    return str(profile.get("label") or profile.get("name") or profile.get("id") or "").strip()


def profile_ref(profile: Mapping[str, Any]) -> str:
    """Stable dispatch reference: name/id before the display label."""
    return str(profile.get("name") or profile.get("id") or profile_label(profile)).strip()


def normalize_profile_roster(values: Any, profiles: list[dict[str, Any]]) -> list[str]:
    """Map profile names and legacy base-engine names to profile-name roster.

    Unknown names are ignored. A legacy base engine expands to every matching
    profile in priority/name order.
    """

    if not isinstance(values, (list, tuple)):
        return []
    by_name = {str(p["name"]): p for p in profiles}
    by_name_lower = {str(p["name"]).lower(): str(p["name"]) for p in profiles}
    by_engine: dict[str, list[str]] = {}
    # coerce_nonneg_int (NOT `priority or 100`): preserve a legal priority 0
    # (highest precedence) instead of silently demoting it to the default.
    for p in sorted(profiles, key=lambda p: (coerce_nonneg_int(p.get("priority"), 100), str(p["name"]))):
        by_engine.setdefault(str(p["engine"]), []).append(str(p["name"]))
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            continue
        # name match is case-insensitive (legacy configs used e.g. "pi-AIsec"
        # while the profile is "pi-aisec"); engine expansion is exact.
        names = (
            [raw] if raw in by_name
            else [by_name_lower[raw.lower()]] if raw.lower() in by_name_lower
            else by_engine.get(raw, [])
        )
        for name in names:
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def profile_uses_endpoint(profile: dict[str, Any] | None) -> bool:
    if not profile:
        return False
    return bool(profile.get("base_url") or profile.get("provider_ref"))


def resolve_seat_ref(
    ref: Any,
    *,
    seats: list[dict[str, Any]],
    alias_table: dict[str, str] | None = None,
) -> str | None:
    """THE single seat-reference resolver (plan §5.0(b)).

    A foreign key in config (engines[]/review.engine/...) may name a
    seat THREE ways:
      - a new seat id (`seat_claude_ab12cd`),
      - a legacy profile name (`claude-local`),
      - a legacy hyphen "canonical" alias (`claude-api-local`, from the old
        worker_config._canonical_profile_id), OR a bare base engine (`claude`).
    All four must resolve to the new seat id. Shared by worker_config / drivers /
    server / swarm so they can never disagree.

    Returns the matched seat id; None when nothing matches (caller decides the
    fallback — NEVER silently swallowed) or when a bare engine is ambiguous across
    multiple seats (None + the caller can expand via the engine fan-out instead).
    """
    if not isinstance(ref, str) or not ref.strip():
        return None
    ref = ref.strip()
    by_id = {str(s.get("id")): s for s in seats if isinstance(s, dict) and s.get("id")}
    if ref in by_id:
        return ref
    alias_table = alias_table or {}
    if ref in alias_table and alias_table[ref] in by_id:
        return alias_table[ref]
    # label match (legacy name kept as the seat label).
    by_label = {str(s.get("label")): str(s.get("id")) for s in seats
                if isinstance(s, dict) and s.get("label")}
    if ref in by_label:
        return by_label[ref]
    # bare base engine → resolve ONLY if exactly one seat for that engine (else
    # ambiguous: the caller should fan out across the engine's seats instead).
    if ref in VALID_BASE_ENGINES:
        matches = [str(s["id"]) for s in seats
                   if isinstance(s, dict) and str(s.get("engine")) == ref and s.get("id")]
        return matches[0] if len(matches) == 1 else None
    return None


def normalize_runtime_profile(
    item: Mapping[str, Any], *, reject_invalid: bool = False
) -> dict[str, Any] | None:
    """Normalize one runtime profile without reading ambient secrets or paths."""

    if not isinstance(item, Mapping):
        if reject_invalid:
            raise ValueError("runtime profile must be an object")
        return None
    runtime_id = str(item.get("id") or "").strip()
    backend = str(item.get("backend") or "").strip().lower()
    if not runtime_id or backend not in {"container", "local"}:
        if reject_invalid:
            raise ValueError("runtime profile requires id and valid backend")
        return None

    raw_features = item.get("runtime_features", ("rcp-v2", "tool-disabled-probe"))
    if isinstance(raw_features, str):
        raw_features = [raw_features]
    if not isinstance(raw_features, (list, tuple, set, frozenset)):
        if reject_invalid:
            raise ValueError("runtime_features must be a sequence")
        raw_features = ("rcp-v2", "tool-disabled-probe")

    normalized = {
        "id": runtime_id,
        "backend": backend,
        "label": str(item.get("label") or runtime_id).strip(),
        "network": str(
            item.get("network") or ("bridge" if backend == "container" else "")
        ).strip(),
        "cpus": str(item.get("cpus") or "1").strip(),
        "memory": str(item.get("memory") or "1g").strip(),
        "pids_limit": item.get("pids_limit") or 256,
        "tmpfs_bytes": item.get("tmpfs_bytes") or 67108864,
        "runtime_features": tuple(sorted({str(value).strip() for value in raw_features if str(value).strip()})),
        "protocol_version": item.get("protocol_version") or 2,
        "pool_max_concurrent_workers": item.get("pool_max_concurrent_workers"),
    }
    return normalized
