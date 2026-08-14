"""Run-folder routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from apps.web.http_utils import _require_dict_body

router = APIRouter(prefix="/api/folders", tags=["folders"])


@router.get("")
async def list_folders(request: Request) -> Any:
    return {"folders": request.app.state.manager.list_folders()}


@router.post("")
async def create_folder(request: Request) -> Any:
    body = await _require_dict_body(request)
    f = request.app.state.manager.create_folder(body.get("name", ""))
    return {"folder": f}


@router.patch("/{folder_id}")
async def update_folder(folder_id: str, request: Request) -> Any:
    body = await _require_dict_body(request)
    ok = request.app.state.manager.update_folder(
        folder_id, name=body.get("name"), order=body.get("order"))
    return {"ok": ok}


@router.delete("/{folder_id}")
async def delete_folder(folder_id: str, request: Request) -> Any:
    ok = request.app.state.manager.delete_folder(folder_id)
    return {"ok": ok}
