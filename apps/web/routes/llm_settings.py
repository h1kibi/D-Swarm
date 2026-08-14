"""LLM provider and endpoint test routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from apps.web.http_utils import _require_dict_body

router = APIRouter(prefix="/api/settings", tags=["llm-settings"])


@router.post("/llm-providers/probe")
async def probe_llm_provider_route(request: Request) -> Any:
    from apps.web.llm_providers import (
        LLMProviderSecretStore, provider_secret_root, probe_llm_provider,
    )

    body = await _require_dict_body(request)
    provider = body.get("provider")
    if not isinstance(provider, dict):
        raise HTTPException(status_code=400, detail="provider must be an object")
    api_key = str(body.get("api_key") or "")
    if not api_key:
        store = LLMProviderSecretStore(provider_secret_root(request.app.state.manager.sessions_root))
        api_key = store.read_secret(str(provider.get("id") or ""))
    try:
        return await asyncio.to_thread(
            probe_llm_provider,
            provider,
            api_key=api_key,
            model=str(body.get("model") or provider.get("default_model") or ""),
            validate_model=bool(body.get("validate_model", False)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/llm/test")
async def test_llm_endpoint_route(request: Request) -> Any:
    body = await _require_dict_body(request)
    from apps.web.llm_test import test_llm_endpoint

    cfg = request.app.state.manager.worker_config.get()
    return await test_llm_endpoint(
        which=str(body.get("which") or "planner"),
        base_url=(body.get("base_url") if body.get("base_url") is not None else None),
        model=(body.get("model") if body.get("model") is not None else None),
        sessions_root=request.app.state.manager.sessions_root,
        worker_profiles=cfg.get("worker_profiles") or [],
        llm_providers=cfg.get("llm_providers") or [],
        provider_ref=(body.get("provider_ref") if body.get("provider_ref") is not None else None),
        credential_account=(
            body.get("credential_account") if body.get("credential_account") is not None else None
        ),
        credential_source=(
            body.get("credential_source") if body.get("credential_source") is not None else None
        ),
        wire_api=(body.get("wire_api") if body.get("wire_api") is not None else None),
    )
