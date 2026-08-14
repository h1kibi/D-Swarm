"""Authentication routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from apps.web.auth import AuthConfig, check_password, issue_token
from apps.web.http_utils import _require_dict_body

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
async def auth_login(request: Request) -> Any:
    cfg: AuthConfig = request.app.state.auth
    body = await _require_dict_body(request, allow_empty=True)
    if not cfg.enabled:
        return {"ok": True, "token": "", "auth_required": False}
    if not check_password(cfg, body.get("password")):
        raise HTTPException(status_code=401, detail="invalid password")
    return {"ok": True, "token": issue_token(cfg), "auth_required": True}


@router.get("/me")
async def auth_me(request: Request) -> Any:
    from dswarm.core.runtime_env import is_web_container

    cfg: AuthConfig = request.app.state.auth
    return {"authenticated": True, "auth_required": cfg.enabled,
            "in_container": is_web_container()}


@router.post("/ticket")
async def auth_ticket(request: Request) -> Any:
    ticket = request.app.state.tickets.mint()
    return {"ticket": ticket}
