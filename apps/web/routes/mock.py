"""Mock endpoints for frontend initialization"""
from fastapi import APIRouter

router = APIRouter()

@router.get("/api/runs")
async def get_runs():
    return {"runs": [], "total": 0}

@router.get("/api/targets")
async def get_targets():
    return {"targets": [], "total": 0}

@router.get("/api/folders")
async def get_folders():
    return {"folders": [], "total": 0}

@router.get("/api/runtime-pools")
async def get_runtime_pools():
    return {"pools": []}

@router.get("/api/scheduler")
async def get_scheduler():
    return {"status": "ok"}
