"""Unified draft/apply helpers for the Worker Settings workspace.

This module is deliberately control-plane only. It validates and persists the
operator's future-run configuration; it never touches a live Run or the solver
provenance/event substrate.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from apps.web.worker_config import (
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_RUNTIME_PROFILES,
    WorkerConfigStore,
)
from apps.web.llm_providers import (
    DEFAULT_PROVIDER_TEMPLATES,
    LLMProviderSecretStore,
    clean_llm_providers,
    valid_provider_id,
)
from dswarm.solver.credential_accounts import CredentialAccountStore, valid_account_id
from dswarm.solver.worker_profiles import canonical_direction, profile_label, profile_ref

_SECRET_KEYS = {"secret", "secret_value", "api_key", "access_token", "refresh_token"}
_BUILTIN_RUNTIMES = {str(row["id"]): row for row in DEFAULT_RUNTIME_PROFILES}
_DIRECTION_CATEGORIES = {
    "web": "web",
    "pwn": "pwn",
    "rev": "reverse",
    "crypto": "crypto",
    "misc": "misc",
    "forensics": "forensics",
    "aisec": "aisec",
}


def sanitize_for_api(value: Any) -> Any:
    """Recursively strip raw credential values from a response object."""
    if isinstance(value, dict):
        return {
            str(k): sanitize_for_api(v)
            for k, v in value.items()
            if str(k).lower() not in _SECRET_KEYS
        }
    if isinstance(value, list):
        return [sanitize_for_api(v) for v in value]
    return value


def workspace_revision(config_store: WorkerConfigStore, account_store: CredentialAccountStore, provider_store: LLMProviderSecretStore | None = None) -> str:
    """Revision covers non-secret config plus public credential metadata."""
    payload = {
        "config": config_store.revision(),
        "accounts": account_store.list(),
        "provider_secrets": provider_store.list() if provider_store is not None else [],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def workspace_snapshot(
    config_store: WorkerConfigStore,
    account_store: CredentialAccountStore,
    provider_store: LLMProviderSecretStore | None = None,
) -> dict[str, Any]:
    return {
        "config": sanitize_for_api(config_store.get()),
        "revision": workspace_revision(config_store, account_store, provider_store),
        "accounts": sanitize_for_api(account_store.list()),
        "provider_templates": sanitize_for_api(DEFAULT_PROVIDER_TEMPLATES),
        "provider_secrets": sanitize_for_api(provider_store.list() if provider_store is not None else []),
    }


_profile_label = profile_label
_profile_ref = profile_ref


def _profile_direction(profile: dict[str, Any]) -> str:
    label = _profile_label(profile).lower()
    if label.startswith("pi-"):
        return canonical_direction(label[3:])
    return ""


def _account_state(
    accounts: list[dict[str, Any]],
    secret_updates: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    state = {
        str(row.get("account_id")): copy.deepcopy(row)
        for row in accounts
        if isinstance(row, dict) and row.get("account_id")
    }

    for update in secret_updates:
        if not isinstance(update, dict):
            continue
        aid = str(update.get("account_id") or "").strip()
        action = str(update.get("action") or "").strip().lower()
        if not aid:
            continue
        if action == "remove":
            state.pop(aid, None)
        elif action == "replace" and str(update.get("value") or "").strip():
            endpoint = str(update.get("base_url") or "").strip()
            state[aid] = {
                "account_id": aid,
                "engine": "pi",
                "mode": "custom_endpoint" if endpoint else "api_key",
                "present": True,
                "details": {
                    "has_secret": True,
                    "base_url_value": endpoint,
                    "target_engine": "pi",
                },
            }
    return state


def _issue(path: str, severity: str, code: str, message: str) -> dict[str, str]:
    return {"path": path, "severity": severity, "code": code, "message": message}


def _severity(profile: dict[str, Any]) -> str:
    return "error" if profile.get("enabled", True) else "warning"


def _valid_endpoint(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except ValueError:
        return False


def _normalized_candidate(
    draft: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    profiles = WorkerConfigStore._clean_worker_profiles(
        draft.get("worker_profiles"), reject_invalid=True
    )
    runtimes = WorkerConfigStore._clean_runtime_profiles(
        draft.get("runtime_profiles"), reject_invalid=True
    )
    llm_profiles = WorkerConfigStore._clean_llm_profiles(
        draft.get("llm_profiles"), reject_invalid=True
    )
    llm_providers = clean_llm_providers(draft.get("llm_providers"), reject_invalid=True)
    return profiles, runtimes, llm_profiles, llm_providers


def derive_routing(profiles: list[dict[str, Any]]) -> tuple[list[str], dict[str, dict[str, Any]], str]:
    """Build automatic routing from the seven direction seats only.

    Custom profiles are intentionally absent. The generic ``pi-worker`` is used
    only as the review/fallback worker by WorkerConfigStore.resolve().
    """
    engines: list[str] = []
    overrides: dict[str, dict[str, Any]] = {}
    system_ref = ""
    for profile in profiles:
        label = _profile_label(profile).lower()
        ref = _profile_ref(profile)
        enabled = bool(profile.get("enabled", True))
        if label == "pi-worker":
            if enabled:
                system_ref = ref
                engines.append(ref)
            continue
        direction = _profile_direction(profile)
        if not direction:
            # Advanced/custom workers are manual-only. They remain available to
            # explicit spawn flows through worker_profiles, but never enter the
            # automatic dispatch roster.
            continue
        category = _DIRECTION_CATEGORIES.get(direction)
        if not category or not enabled:
            continue
        engines.append(ref)
        overrides[category] = {
            "engines": [ref],
            "start_workers": max(1, min(2, int(profile.get("max_running") or 1))),
        }
    return engines, overrides, system_ref


def validate_workspace_draft(
    *,
    current: dict[str, Any],
    draft: dict[str, Any],
    accounts: list[dict[str, Any]],
    secret_updates: list[dict[str, Any]] | None = None,
    provider_secrets: list[dict[str, Any]] | None = None,
    provider_secret_updates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fast structural validation. No network or container work is performed."""
    secret_updates = [u for u in (secret_updates or []) if isinstance(u, dict)]
    provider_secret_updates = [u for u in (provider_secret_updates or []) if isinstance(u, dict)]
    issues: list[dict[str, str]] = []
    try:
        profiles, runtimes, llm_profiles, llm_providers = _normalized_candidate(draft)
    except ValueError as exc:
        return {
            "ok": False,
            "issues": [_issue("draft", "error", "invalid_shape", str(exc))],
            "changes": [],
        }

    runtime_by_id = {str(row.get("id")): row for row in runtimes}
    account_by_id = _account_state(accounts, secret_updates)
    provider_by_id = {str(p.get("id")): p for p in llm_providers}
    psecret_present = {str(row.get("provider_id")): bool(row.get("present")) for row in (provider_secrets or []) if isinstance(row, dict)}
    for update in provider_secret_updates:
        pid = str(update.get("provider_id") or "").strip()
        action = str(update.get("action") or "").strip().lower()
        if action == "remove":
            psecret_present[pid] = False
        elif action == "replace" and str(update.get("value") or "").strip():
            psecret_present[pid] = True

    # Built-in templates are immutable. Direction customization must use a
    # direction-owned runtime id instead of silently changing a shared preset.
    for rid, builtin in _BUILTIN_RUNTIMES.items():
        candidate = runtime_by_id.get(rid)
        if candidate is None:
            issues.append(_issue(
                f"runtime_profiles.{rid}", "error", "builtin_runtime_missing",
                f"Built-in runtime '{rid}' cannot be removed.",
            ))
            continue
        for key in ("backend", "network", "memory", "cpus", "pids_limit"):
            expected = builtin.get(key, "" if key != "pids_limit" else 0)
            actual = candidate.get(key, "" if key != "pids_limit" else 0)
            if str(actual) != str(expected):
                issues.append(_issue(
                    f"runtime_profiles.{rid}.{key}", "error", "builtin_runtime_immutable",
                    f"Built-in runtime '{rid}' is read-only; clone it before customizing.",
                ))
                break


    seen_provider_ids: set[str] = set()
    for index, provider in enumerate(llm_providers):
        path = f"llm_providers.{index}"
        pid = str(provider.get("id") or "").strip()
        if not valid_provider_id(pid):
            issues.append(_issue(path, "error", "invalid_provider_id", "Invalid LLM provider id."))
        elif pid.lower() in seen_provider_ids:
            issues.append(_issue(path, "error", "duplicate_provider_id", f"Duplicate provider id: {pid}."))
        seen_provider_ids.add(pid.lower())
        endpoint = str(provider.get("base_url") or "").strip()
        if not endpoint:
            issues.append(_issue(f"{path}.base_url", "error", "missing_provider_endpoint", "Provider Base URL is required."))
        elif not _valid_endpoint(endpoint):
            issues.append(_issue(f"{path}.base_url", "error", "invalid_endpoint", "Endpoint must be an http(s) URL."))
        if str(provider.get("auth_mode") or "bearer").lower() == "custom" and not str(provider.get("auth_header") or "").strip():
            issues.append(_issue(f"{path}.auth_header", "error", "missing_auth_header", "Custom authentication requires a Header name."))

    labels: set[str] = set()
    for index, profile in enumerate(profiles):
        label = _profile_label(profile)
        path = f"worker_profiles.{index}"
        sev = _severity(profile)
        if not label:
            issues.append(_issue(path, sev, "missing_label", "Worker name is required."))
        elif label.lower() in labels:
            issues.append(_issue(path, sev, "duplicate_label", f"Duplicate worker name: {label}."))
        labels.add(label.lower())

        model = str(profile.get("model") or "").strip()
        if not model:
            issues.append(_issue(f"{path}.model", sev, "missing_model", "Model is required."))

        runtime_id = str(profile.get("runtime") or "").strip()
        runtime = runtime_by_id.get(runtime_id)
        if runtime is None:
            issues.append(_issue(
                f"{path}.runtime", sev, "missing_runtime", "Select a valid runtime environment."
            ))
        elif str(runtime.get("backend")) == "container" and not str(profile.get("image") or "").strip():
            issues.append(_issue(
                f"{path}.image", sev, "missing_image", "Container workers require an image."
            ))

        provider_ref = str(profile.get("provider_ref") or "").strip()
        if provider_ref:
            provider = provider_by_id.get(provider_ref)
            if not provider:
                issues.append(_issue(f"{path}.provider_ref", sev, "unknown_provider", "Selected LLM provider does not exist."))
            else:
                if not psecret_present.get(provider_ref):
                    issues.append(_issue(f"{path}.provider_ref", sev, "missing_provider_secret", "Provider API key is not configured."))
                endpoint = str(provider.get("base_url") or "").strip()
                if endpoint and not _valid_endpoint(endpoint):
                    issues.append(_issue(f"{path}.provider_ref", sev, "invalid_endpoint", "Provider endpoint must be an http(s) URL."))
        else:
            account_id = str(profile.get("credential_account") or "").strip()
            account = account_by_id.get(account_id)
            if not account_id:
                issues.append(_issue(
                    f"{path}.credential_account", sev, "missing_account", "A direction credential is required."
                ))
            elif not account or not account.get("present"):
                issues.append(_issue(
                    f"{path}.credential_account", sev, "missing_secret", "API key is not configured."
                ))

            details = account.get("details") if isinstance(account, dict) and isinstance(account.get("details"), dict) else {}
            endpoint = str(profile.get("base_url") or details.get("base_url_value") or "").strip()
            if not endpoint:
                issues.append(_issue(
                    f"{path}.base_url", sev, "missing_endpoint", "Endpoint URL is required."
                ))
            elif not _valid_endpoint(endpoint):
                issues.append(_issue(
                    f"{path}.base_url", sev, "invalid_endpoint", "Endpoint must be an http(s) URL."
                ))

        wire_api = str(profile.get("wire_api") or "auto").strip().lower()
        if wire_api not in {"auto", "openai", "openai-chat", "openai-responses"}:
            issues.append(_issue(
                f"{path}.wire_api", sev, "invalid_wire_api", "Select a supported OpenAI protocol."
            ))
        auth_mode = str(profile.get("auth_mode") or "bearer").strip().lower()
        if auth_mode not in {"bearer", "x-api-key", "custom"}:
            issues.append(_issue(
                f"{path}.auth_mode", sev, "invalid_auth_mode", "Select a supported authentication mode."
            ))
        if auth_mode == "custom" and not str(profile.get("auth_header") or "").strip():
            issues.append(_issue(
                f"{path}.auth_header", sev, "missing_auth_header", "Custom authentication requires a Header name."
            ))

        try:
            capacity = int(profile.get("max_running") or 0)
        except (TypeError, ValueError):
            capacity = 0
        if capacity < 1:
            issues.append(_issue(
                f"{path}.max_running", sev, "invalid_capacity", "Capacity must be at least 1."
            ))

    valid_llm_sources = {"auto", "env", "account", "provider"}
    valid_wire_apis = {"auto", "openai", "openai-chat", "openai-responses"}
    for key in ("planner", "titler"):
        profile = (draft.get("llm_profiles") or {}).get(key)
        normalized = llm_profiles.get(key) or {}
        path = f"llm_profiles.{key}"
        if not isinstance(profile, dict):
            continue
        model = str(profile.get("model") or normalized.get("model") or "").strip()
        if not model:
            issues.append(_issue(f"{path}.model", "error", "missing_llm_model", "LLM model is required."))
        provider_ref = str(profile.get("provider_ref") or normalized.get("provider_ref") or "").strip()
        if provider_ref:
            if provider_ref not in provider_by_id:
                issues.append(_issue(f"{path}.provider_ref", "error", "unknown_provider", "Selected LLM provider does not exist."))
            elif not psecret_present.get(provider_ref):
                issues.append(_issue(f"{path}.provider_ref", "warning", "missing_provider_secret", "Provider API key is not configured."))
        base_url = str(profile.get("base_url") or normalized.get("base_url") or DEFAULT_DEEPSEEK_BASE_URL).strip()
        if not provider_ref and base_url and not _valid_endpoint(base_url):
            issues.append(_issue(f"{path}.base_url", "error", "invalid_endpoint", "Endpoint must be an http(s) URL."))
        source = str(profile.get("credential_source") or normalized.get("credential_source") or "auto").strip().lower()
        if source not in valid_llm_sources:
            issues.append(_issue(f"{path}.credential_source", "error", "invalid_llm_source", "Select a valid credential source."))
        account_id = str(profile.get("credential_account") or normalized.get("credential_account") or "").strip()
        if provider_ref:
            # Provider-bound ReasonSwarm profiles inherit endpoint and secret from the
            # LLM provider registry. The UI keeps credential_source=provider and
            # credential_account=<provider id> as an explicit, non-secret marker.
            pass
        elif source == "account":
            account = account_by_id.get(account_id)
            if not account_id or not account or not account.get("present"):
                issues.append(_issue(
                    f"{path}.credential_account", "warning", "missing_llm_account",
                    "Selected ReasonSwarm credential account is not configured.",
                ))
        wire_api = str(profile.get("wire_api") or normalized.get("wire_api") or "auto").strip().lower()
        if wire_api not in valid_wire_apis:
            issues.append(_issue(f"{path}.wire_api", "error", "invalid_wire_api", "Select a supported OpenAI protocol."))

    for update in secret_updates:
        aid = str(update.get("account_id") or "").strip()
        action = str(update.get("action") or "").strip().lower()
        if not valid_account_id(aid):
            issues.append(_issue("secret_updates", "error", "invalid_account_id", "Invalid credential id."))
        elif action not in {"replace", "remove"}:
            issues.append(_issue(
                f"secret_updates.{aid}", "error", "invalid_secret_action",
                "Secret action must be replace or remove.",
            ))
        elif action == "replace" and not str(update.get("value") or "").strip():
            issues.append(_issue(
                f"secret_updates.{aid}", "error", "empty_secret", "Replacement API key is empty."
            ))
        elif action == "replace":
            endpoint = str(update.get("base_url") or "").strip()
            if endpoint and not _valid_endpoint(endpoint):
                issues.append(_issue(
                    f"secret_updates.{aid}.base_url", "error", "invalid_endpoint",
                    "Endpoint must be an http(s) URL.",
                ))


    for update in provider_secret_updates:
        pid = str(update.get("provider_id") or "").strip()
        action = str(update.get("action") or "").strip().lower()
        if not valid_provider_id(pid):
            issues.append(_issue("provider_secret_updates", "error", "invalid_provider_id", "Invalid LLM provider id."))
        elif action not in {"replace", "remove"}:
            issues.append(_issue(f"provider_secret_updates.{pid}", "error", "invalid_secret_action", "Secret action must be replace or remove."))
        elif action == "replace" and not str(update.get("value") or "").strip():
            issues.append(_issue(f"provider_secret_updates.{pid}", "error", "empty_secret", "Replacement API key is empty."))

    changes = summarize_changes(
        current,
        {**draft, "worker_profiles": profiles, "runtime_profiles": runtimes, "llm_profiles": llm_profiles, "llm_providers": llm_providers},
        secret_updates,
        provider_secret_updates,
    )
    return {
        "ok": not any(row["severity"] == "error" for row in issues),
        "issues": issues,
        "changes": changes,
    }


def summarize_changes(
    current: dict[str, Any],
    draft: dict[str, Any],
    secret_updates: list[dict[str, Any]],
    provider_secret_updates: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    provider_secret_updates = provider_secret_updates or []
    changes: list[dict[str, Any]] = []
    try:
        current_profiles, current_runtimes, current_llm_profiles, current_llm_providers = _normalized_candidate(current)
    except ValueError:
        current_profiles = [p for p in (current.get("worker_profiles") or []) if isinstance(p, dict)]
        current_runtimes = current.get("runtime_profiles") or []
        current_llm_profiles = current.get("llm_profiles") or {}
        current_llm_providers = current.get("llm_providers") or []
    try:
        draft_profiles, draft_runtimes, draft_llm_profiles, draft_llm_providers = _normalized_candidate(draft)
    except ValueError:
        draft_profiles = [p for p in (draft.get("worker_profiles") or []) if isinstance(p, dict)]
        draft_runtimes = draft.get("runtime_profiles") or []
        draft_llm_profiles = draft.get("llm_profiles") or {}
        draft_llm_providers = draft.get("llm_providers") or []
    cur_profiles = {
        _profile_label(p): p for p in current_profiles if isinstance(p, dict)
    }
    for profile in draft_profiles:
        if not isinstance(profile, dict):
            continue
        label = _profile_label(profile)
        prior = cur_profiles.get(label)
        fields = []
        defaults = {
            "provider_ref": "",
            "base_url": "",
            "wire_api": "auto",
            "auth_mode": "bearer",
            "auth_header": "Authorization",
            "auth_prefix": "Bearer",
            "api_key_ref": "",
        }
        numeric = {"max_running", "max_review_running"}
        for key in ("enabled", "provider_ref", "base_url", "wire_api", "auth_mode", "auth_header", "auth_prefix", "model", "effort", "runtime", "image", "max_running", "max_review_running"):
            if prior is None:
                fields.append(key)
                continue
            left = prior.get(key, defaults.get(key))
            right = profile.get(key, defaults.get(key))
            if key in numeric:
                try:
                    left = int(left or 0)
                    right = int(right or 0)
                except (TypeError, ValueError):
                    pass
            if left != right:
                fields.append(key)
        if fields:
            changes.append({"scope": "worker", "id": label, "fields": fields})
    if current_runtimes != draft_runtimes:
        changes.append({"scope": "runtime", "id": "templates", "fields": ["runtime_profiles"]})
    if current.get("stage_policy") != draft.get("stage_policy"):
        changes.append({"scope": "reason", "id": "ReasonSwarm", "fields": ["stage_policy"]})
    if current_llm_profiles != draft_llm_profiles:
        changes.append({"scope": "reason", "id": "LLM", "fields": ["llm_profiles"]})
    if current_llm_providers != draft_llm_providers:
        changes.append({"scope": "provider", "id": "LLM 提供商", "fields": ["llm_providers"]})
    for update in secret_updates:
        changes.append({
            "scope": "secret",
            "id": str(update.get("account_id") or ""),
            "fields": [str(update.get("action") or "replace")],
        })
    for update in provider_secret_updates:
        changes.append({
            "scope": "provider_secret",
            "id": str(update.get("provider_id") or ""),
            "fields": [str(update.get("action") or "replace")],
        })
    return changes


def _snapshot_accounts(store: CredentialAccountStore, account_ids: set[str]) -> dict[str, dict[str, bytes] | None]:
    snapshots: dict[str, dict[str, bytes] | None] = {}
    for account_id in account_ids:
        base = store.root / account_id
        if not base.exists():
            snapshots[account_id] = None
            continue
        material: dict[str, bytes] = {}
        for path in base.iterdir():
            if path.is_file():
                material[path.name] = path.read_bytes()
        snapshots[account_id] = material
    return snapshots


def _restore_accounts(store: CredentialAccountStore, snapshots: dict[str, dict[str, bytes] | None]) -> None:
    for account_id, material in snapshots.items():
        base = store.root / account_id
        if base.exists():
            shutil.rmtree(base)
        if material is None:
            continue
        base.mkdir(parents=True, exist_ok=True)
        try:
            base.chmod(0o700)
        except OSError:
            pass
        for name, value in material.items():
            path = base / name
            path.write_bytes(value)
            try:
                path.chmod(0o600)
            except OSError:
                pass


def apply_workspace_draft(
    *,
    config_store: WorkerConfigStore,
    account_store: CredentialAccountStore,
    provider_store: LLMProviderSecretStore | None = None,
    base_revision: str = "",
    draft: dict[str, Any] | None = None,
    secret_updates: list[dict[str, Any]] | None = None,
    provider_secret_updates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    draft = draft or {}
    secret_updates = [u for u in (secret_updates or []) if isinstance(u, dict)]
    provider_secret_updates = [u for u in (provider_secret_updates or []) if isinstance(u, dict)]
    current_revision = workspace_revision(config_store, account_store, provider_store)
    if base_revision and base_revision != current_revision:
        raise RuntimeError("settings_revision_conflict")

    current = config_store.get()
    validation = validate_workspace_draft(
        current=current,
        draft=draft,
        accounts=account_store.list(),
        secret_updates=secret_updates,
        provider_secrets=provider_store.list() if provider_store is not None else [],
        provider_secret_updates=provider_secret_updates,
    )
    if not validation["ok"]:
        raise ValueError("worker settings draft has fatal validation issues")

    config_snapshot = config_store.raw_snapshot()
    touched = {
        str(row.get("account_id") or "").strip()
        for row in secret_updates
        if str(row.get("account_id") or "").strip()
    }
    touched_providers = {
        str(row.get("provider_id") or "").strip()
        for row in provider_secret_updates
        if str(row.get("provider_id") or "").strip()
    }
    account_snapshot = _snapshot_accounts(account_store, touched)
    provider_snapshot = _snapshot_accounts(provider_store, touched_providers) if provider_store is not None else {}

    try:
        for update in secret_updates:
            account_id = str(update.get("account_id") or "").strip()
            action = str(update.get("action") or "").strip().lower()
            if action == "remove":
                account_store.delete(account_id)
                continue
            base_url = str(update.get("base_url") or "").strip()
            account_store.upsert_secret(
                account_id=account_id,
                engine="api" if base_url else "pi",
                secret=str(update.get("value") or ""),
                base_url=base_url or None,
                target_engine="pi",
            )


        if provider_store is not None:
            for update in provider_secret_updates:
                provider_id = str(update.get("provider_id") or "").strip()
                action = str(update.get("action") or "").strip().lower()
                if action == "remove":
                    provider_store.delete(provider_id)
                    continue
                provider_store.upsert_secret(provider_id, str(update.get("value") or ""))

        profiles, runtimes, llm_profiles, llm_providers = _normalized_candidate(draft)
        engines, overrides, system_ref = derive_routing(profiles)
        stage_policy = copy.deepcopy(draft.get("stage_policy") or current.get("stage_policy") or {})
        coordinator = stage_policy.setdefault("coordinator", {})
        review = coordinator.setdefault("review", {})
        review["enabled"] = bool(system_ref)
        if system_ref:
            review["engine"] = system_ref

        config_store.set(
            # An explicit empty roster is meaningful: all direction/System
            # Workers may be disabled while custom Workers remain manual-only.
            engines=engines,
            start_workers=draft.get("start_workers", current.get("start_workers")),
            worker_backend=draft.get("worker_backend", current.get("worker_backend")),
            wall_clock_budget=draft.get("wall_clock_budget", current.get("wall_clock_budget")),
            max_total_workers=draft.get("max_total_workers", current.get("max_total_workers")),
            cost_budget_usd=draft.get("cost_budget_usd", current.get("cost_budget_usd")),
            stage_policy=stage_policy,
            llm_profiles=llm_profiles,
            llm_providers=llm_providers,
            runtime_profiles=runtimes,
            worker_profiles=profiles,
            overrides=overrides,
            routing_mode="directional",
        )
    except Exception:
        _restore_accounts(account_store, account_snapshot)
        if provider_store is not None:
            _restore_accounts(provider_store, provider_snapshot)
        config_store.restore_snapshot(config_snapshot)
        raise

    snapshot = workspace_snapshot(config_store, account_store, provider_store)
    snapshot["issues"] = validation["issues"]
    snapshot["changes"] = validation["changes"]
    return snapshot
