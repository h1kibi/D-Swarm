"""Worker settings workspace routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from apps.web.http_utils import _require_dict_body, project_probe_result
from dswarm.solver.credential_accounts import CredentialAccountStore, account_store_root

router = APIRouter(prefix="/api/settings/workers", tags=["worker-settings"])

_PROFILE_HEALTH_FIELDS = (
    "profile_id",
    "engine",
    "backend",
    "status",
    "layer",
    "blocker",
    "detail",
    "model",
    "account_id",
    "binding_kind",
    "effective_credential_id",
)


@router.get("")
async def get_worker_settings(request: Request) -> Any:
    from apps.web.worker_settings import workspace_snapshot
    from apps.web.llm_providers import LLMProviderSecretStore, provider_secret_root

    account_store = CredentialAccountStore(
        account_store_root(request.app.state.manager.sessions_root)
    )
    provider_store = LLMProviderSecretStore(
        provider_secret_root(request.app.state.manager.sessions_root)
    )
    return workspace_snapshot(request.app.state.manager.worker_config, account_store, provider_store)


@router.post("/validate")
async def validate_worker_settings(request: Request) -> Any:
    from apps.web.worker_settings import validate_workspace_draft
    from apps.web.llm_providers import LLMProviderSecretStore, provider_secret_root

    body = await _require_dict_body(request)
    draft = body.get("draft")
    if not isinstance(draft, dict):
        raise HTTPException(status_code=400, detail="draft must be an object")
    secret_updates = body.get("secret_updates") or []
    if not isinstance(secret_updates, list):
        raise HTTPException(status_code=400, detail="secret_updates must be a list")
    provider_secret_updates = body.get("provider_secret_updates") or []
    if not isinstance(provider_secret_updates, list):
        raise HTTPException(status_code=400, detail="provider_secret_updates must be a list")
    account_store = CredentialAccountStore(
        account_store_root(request.app.state.manager.sessions_root)
    )
    provider_store = LLMProviderSecretStore(
        provider_secret_root(request.app.state.manager.sessions_root)
    )
    return validate_workspace_draft(
        current=request.app.state.manager.worker_config.get(),
        draft=draft,
        accounts=account_store.list(),
        secret_updates=secret_updates,
        provider_secrets=provider_store.list(),
        provider_secret_updates=provider_secret_updates,
    )


@router.put("/apply")
async def apply_worker_settings(request: Request) -> Any:
    from apps.web.worker_settings import apply_workspace_draft
    from apps.web.llm_providers import LLMProviderSecretStore, provider_secret_root

    body = await _require_dict_body(request)
    draft = body.get("draft")
    if not isinstance(draft, dict):
        raise HTTPException(status_code=400, detail="draft must be an object")
    secret_updates = body.get("secret_updates") or []
    if not isinstance(secret_updates, list):
        raise HTTPException(status_code=400, detail="secret_updates must be a list")
    provider_secret_updates = body.get("provider_secret_updates") or []
    if not isinstance(provider_secret_updates, list):
        raise HTTPException(status_code=400, detail="provider_secret_updates must be a list")
    account_store = CredentialAccountStore(
        account_store_root(request.app.state.manager.sessions_root)
    )
    provider_store = LLMProviderSecretStore(
        provider_secret_root(request.app.state.manager.sessions_root)
    )
    try:
        result = apply_workspace_draft(
            config_store=request.app.state.manager.worker_config,
            account_store=account_store,
            provider_store=provider_store,
            base_revision=str(body.get("base_revision") or ""),
            draft=draft,
            secret_updates=secret_updates,
            provider_secret_updates=provider_secret_updates,
        )
    except RuntimeError as exc:
        if str(exc) == "settings_revision_conflict":
            raise HTTPException(
                status_code=409,
                detail="Worker settings changed elsewhere. Reload before applying.",
            )
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, **result}


@router.post("/probe")
async def probe_worker_settings_draft(request: Request) -> Any:
    from apps.web.worker_endpoint import probe_worker_endpoint, resolve_saved_api_key

    body = await _require_dict_body(request)
    profile = body.get("profile")
    if not isinstance(profile, dict):
        raise HTTPException(status_code=400, detail="profile must be an object")
    api_key = str(body.get("api_key") or "")
    provider_ref = str(profile.get("provider_ref") or "").strip()
    if provider_ref:
        from apps.web.llm_providers import (
            LLMProviderSecretStore, provider_secret_root, probe_llm_provider,
        )

        cfg = request.app.state.manager.worker_config.get()
        providers = [p for p in (cfg.get("llm_providers") or []) if isinstance(p, dict)]
        provider = next((p for p in providers if str(p.get("id") or "") == provider_ref), None)
        if provider is None:
            raise HTTPException(status_code=400, detail="unknown LLM provider")
        if not api_key:
            store = LLMProviderSecretStore(provider_secret_root(request.app.state.manager.sessions_root))
            api_key = store.read_secret(provider_ref)
        edited_provider = {**provider, "default_model": profile.get("model") or provider.get("default_model") or ""}
        return await asyncio.to_thread(
            probe_llm_provider,
            edited_provider,
            api_key=api_key,
            model=str(profile.get("model") or provider.get("default_model") or ""),
            validate_model=bool(body.get("validate_model", False)),
        )
    if not api_key:
        api_key = await asyncio.to_thread(
            resolve_saved_api_key, profile, request.app.state.manager.sessions_root
        )
    return await asyncio.to_thread(
        probe_worker_endpoint,
        profile,
        api_key=api_key,
        validate_model=bool(body.get("validate_model", False)),
    )


@router.post("/test")
async def test_worker_settings(request: Request) -> Any:
    from apps.web.worker_config import backend_for_profile
    from dswarm.core.runtime_env import is_web_container
    from dswarm.solver.profile_health import evaluate_profile_health

    body = await _require_dict_body(request)
    worker_ids = body.get("worker_ids")
    if not isinstance(worker_ids, list) or not all(isinstance(x, str) for x in worker_ids):
        raise HTTPException(status_code=400, detail="worker_ids must be a string list")
    cfg = request.app.state.manager.worker_config.get()
    profiles = [p for p in (cfg.get("worker_profiles") or []) if isinstance(p, dict)]
    requested = {x.strip() for x in worker_ids if x.strip()}
    matches = [
        p for p in profiles
        if requested.intersection({
            str(p.get("id") or ""),
            str(p.get("name") or ""),
            str(p.get("label") or ""),
        })
    ]
    found = {
        value
        for p in matches
        for value in (str(p.get("id") or ""), str(p.get("name") or ""), str(p.get("label") or ""))
        if value in requested
    }
    missing = sorted(requested - found)

    def _probe_all() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for profile in matches:
            backend = backend_for_profile(
                profile,
                runtime_profiles=cfg.get("runtime_profiles") or [],
                worker_backend=str(cfg.get("worker_backend") or ""),
                in_web_container=is_web_container(),
            )
            health = evaluate_profile_health(
                profile,
                backend=backend,
                sessions_root=request.app.state.manager.sessions_root,
                depth="auth",
                llm_providers=cfg.get("llm_providers") or [],
            )
            out.append(project_probe_result(
                health,
                fields=_PROFILE_HEALTH_FIELDS,
                include_ok=True,
                extras={
                    "worker_id": str(
                        profile.get("label") or profile.get("name") or profile.get("id") or ""
                    ),
                },
            ))
        return out

    results = await asyncio.to_thread(_probe_all)
    results.extend({"worker_id": worker_id, "ok": False, "detail": "Unknown worker."} for worker_id in missing)
    return {"ok": bool(results) and all(bool(row.get("ok")) for row in results), "results": results}


@router.put("")
async def put_worker_settings(request: Request) -> Any:
    body = await _require_dict_body(request)
    try:
        from apps.web.drivers import _reject_retired_swarm_fields

        _reject_retired_swarm_fields(body)
        cfg = request.app.state.manager.worker_config.set(
            engines=body.get("engines"),
            max_workers=body.get("max_workers"),
            worker_backend=body.get("worker_backend"),
            wall_clock_budget=body.get("wall_clock_budget"),
            max_total_workers=body.get("max_total_workers"),
            cost_budget_usd=body.get("cost_budget_usd"),
            review_policy=body.get("review_policy"),
            llm_profiles=body.get("llm_profiles"),
            llm_providers=body.get("llm_providers"),
            runtime_profiles=body.get("runtime_profiles"),
            worker_profiles=body.get("worker_profiles"),
            overrides=body.get("overrides"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "config": cfg}
