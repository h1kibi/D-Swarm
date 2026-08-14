"""Worker image health and pull routes."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter

router = APIRouter(prefix="/api/settings", tags=["worker-image"])


@router.get("/worker-image")
async def get_worker_image() -> Any:
    from apps.web.worker_image import image_status
    return await asyncio.to_thread(image_status)


@router.post("/worker-image/pull")
async def pull_worker_image() -> Any:
    from apps.web.worker_image import pull_image
    return await asyncio.to_thread(pull_image)
