"""Small HTTP/env helpers shared by the web server routes."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, is_dataclass
from typing import Any, Mapping, Sequence

import httpx
from fastapi import HTTPException, Request


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        return int(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default



def _probe_source_dict(result: Mapping[str, Any] | object) -> dict[str, Any]:
    """Return a shallow dict view of a probe result without changing values."""
    if isinstance(result, Mapping):
        return dict(result)
    if is_dataclass(result) and not isinstance(result, type):
        return asdict(result)
    return {
        name: getattr(result, name)
        for name in dir(result)
        if not name.startswith("_") and not callable(getattr(result, name, None))
    }


def project_probe_result(
    result: Mapping[str, Any] | object,
    *,
    fields: Sequence[str] | None = None,
    include_ok: bool = False,
    omit_none: bool = False,
    extras: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project a probe/health object into an explicit public API payload.

    This is intentionally a projection helper, not a schema migrator: callers
    choose their public field list, values are not renamed or coerced, and no
    probe-internal keys are exposed unless the caller explicitly asks for them.
    """
    source = _probe_source_dict(result)
    selected = tuple(fields) if fields is not None else tuple(source.keys())
    out: dict[str, Any] = {}
    for key in selected:
        if key not in source:
            continue
        value = source[key]
        if omit_none and value is None:
            continue
        out[key] = value
    if include_ok:
        ok = source.get("ok")
        if ok is None and hasattr(result, "ok"):
            ok = getattr(result, "ok")
        if not (omit_none and ok is None):
            out["ok"] = ok
    if extras:
        for key, value in extras.items():
            if omit_none and value is None:
                continue
            out[key] = value
    return out

MAX_UPLOAD_BYTES = max(1, _env_int("DSWARM_MAX_UPLOAD_MB", 25)) * 1024 * 1024
MAX_UPLOAD_FILES = max(1, _env_int("DSWARM_MAX_UPLOAD_FILES", 20))
MAX_UPLOAD_TOTAL_BYTES = max(
    1, _env_int("DSWARM_MAX_UPLOAD_TOTAL_MB", 100)
) * 1024 * 1024


def _btw_timeout_exception(exc: BaseException) -> bool:
    """Return whether a BTW failure is a transport or wall-clock timeout."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException)):
        return True
    names = {cls.__name__.lower() for cls in type(exc).__mro__}
    return any("timeout" in name for name in names)


async def _require_dict_body(request: "Request", *, allow_empty: bool = False) -> dict[str, Any]:
    """Parse a JSON request body and require it to be a JSON object."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        if allow_empty:
            return {}
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be a JSON object")
    return body
