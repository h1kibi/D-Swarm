"""Credential account settings routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from apps.web.http_utils import _require_dict_body
from dswarm.solver.credential_accounts import CredentialAccountStore, account_store_root

router = APIRouter(prefix="/api/settings", tags=["credentials"])


@router.get("/credential-accounts")
async def list_credential_accounts(request: Request) -> Any:
    store = CredentialAccountStore(account_store_root(request.app.state.manager.sessions_root))
    return {"accounts": store.list()}


@router.put("/credential-accounts/{account_id}")
async def put_credential_account(account_id: str, request: Request) -> Any:
    body = await _require_dict_body(request)
    store = CredentialAccountStore(account_store_root(request.app.state.manager.sessions_root))
    try:
        account = store.upsert_secret(
            account_id=account_id,
            engine=str(body.get("engine") or ""),
            secret=(body.get("secret") if body.get("secret") is not None else None),
            base_url=(body.get("base_url") if body.get("base_url") is not None else None),
            target_engine=(
                body.get("target_engine") if body.get("target_engine") is not None else None
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "account": account}


@router.delete("/credential-accounts/{account_id}")
async def delete_credential_account(account_id: str, request: Request) -> Any:
    store = CredentialAccountStore(account_store_root(request.app.state.manager.sessions_root))
    return {"ok": store.delete(account_id)}


@router.post("/credential-accounts/{account_id}/test")
async def test_credential_account(account_id: str, request: Request) -> Any:
    body = await _require_dict_body(request, allow_empty=True)
    from apps.web.account_test import probe_account

    engine = str(body.get("engine") or "").strip()
    backend = str(body.get("backend") or "local").strip()
    if backend not in ("local", "container"):
        backend = "local"
    return await asyncio.to_thread(
        probe_account,
        engine=engine,
        account_id=account_id,
        sessions_root=request.app.state.manager.sessions_root,
        backend=backend,
    )


@router.get("/system-login")
async def get_system_login(request: Request) -> Any:
    from dswarm.solver.credential_accounts import detect_system_login

    logins = await asyncio.to_thread(
        lambda: {e: detect_system_login(e) for e in ("pi",)}
    )
    return {"logins": logins}
