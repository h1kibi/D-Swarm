"""In-memory login throttle.

Per (app instance, client host): a sliding-window failure counter. A successful
login clears the bucket. Single-process by design, like the rest of the web
layer — a restart clears the counters, which is acceptable at team scale and
keeps this dependency-free. Scoping by app instance keeps the scripted test
suite isolated (each TestClient app gets its own counters).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, List, Tuple

MAX_FAILS_ENV = "DSWARM_LOGIN_MAX_FAILS"
WINDOW_S_ENV = "DSWARM_LOGIN_WINDOW_S"

_lock = threading.Lock()
_failures: Dict[Tuple[int, str], List[float]] = {}


def limits() -> Tuple[int, float]:
    """(max failures, window seconds), env-tunable with safe fallbacks."""
    try:
        max_fails = int(os.getenv(MAX_FAILS_ENV, "10"))
    except ValueError:
        max_fails = 10
    try:
        window = float(os.getenv(WINDOW_S_ENV, "300"))
    except ValueError:
        window = 300.0
    return max(1, max_fails), max(1.0, window)


def _fresh(bucket: List[float], window: float, now: float) -> List[float]:
    return [stamp for stamp in bucket if now - stamp < window]


def blocked_for(app: Any, host: str) -> float:
    """Seconds until the next attempt is allowed; 0 when not blocked."""
    max_fails, window = limits()
    key = (id(app), host)
    now = time.monotonic()
    with _lock:
        bucket = _fresh(_failures.get(key, []), window, now)
        if bucket:
            _failures[key] = bucket
        else:
            _failures.pop(key, None)
        if len(bucket) < max_fails:
            return 0.0
        return max(0.0, window - (now - bucket[0]))


def record_failure(app: Any, host: str) -> None:
    _, window = limits()
    key = (id(app), host)
    now = time.monotonic()
    with _lock:
        bucket = _fresh(_failures.get(key, []), window, now)
        bucket.append(now)
        _failures[key] = bucket


def record_success(app: Any, host: str) -> None:
    with _lock:
        _failures.pop((id(app), host), None)
