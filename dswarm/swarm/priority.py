"""Intent-priority normalization and stable ordering helpers.

The planner and operator use intentionally different priority scales.  Keep the
scales explicit instead of pretending their numeric values are comparable.
"""

from __future__ import annotations

import math
from typing import Any

PRIORITY_PLANNER = "planner"
PRIORITY_OPERATOR = "operator"

_LABEL_PRIORITIES = {
    "operator": 100.0,
    "high": 50.0,
    "normal": 0.0,
    "low": -10.0,
}
_VALID_SCALES = {PRIORITY_PLANNER, PRIORITY_OPERATOR}


def normalize_priority(value: Any) -> float:
    """Return a finite floating-point priority without losing planner precision."""
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned in _LABEL_PRIORITIES:
            return _LABEL_PRIORITIES[cleaned]
        try:
            number = float(cleaned)
        except (TypeError, ValueError):
            return 0.0
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
    return number if math.isfinite(number) else 0.0


def normalize_priority_scale(
    value: Any = None,
    *,
    actor: str = "",
    source: str = "",
    raw_priority: Any = None,
) -> str:
    """Normalize an explicit scale or derive it from trusted operator context."""
    explicit = str(value or "").strip().lower()
    if explicit in _VALID_SCALES:
        return explicit

    actor_key = str(actor or "").strip().lower()
    source_key = str(source or "").strip().lower()
    priority_label = (
        str(raw_priority).strip().lower() if isinstance(raw_priority, str) else ""
    )
    if (
        actor_key == PRIORITY_OPERATOR
        or source_key.startswith("operator_")
        or priority_label in _LABEL_PRIORITIES
    ):
        return PRIORITY_OPERATOR
    return PRIORITY_PLANNER


def priority_sort_key(scale: str, priority: Any, created_seq: Any) -> tuple[int, float, int]:
    """Return the canonical scale-first, descending-priority, FIFO key."""
    scale_rank = 0 if normalize_priority_scale(scale) == PRIORITY_OPERATOR else 1
    try:
        seq = int(created_seq)
    except (TypeError, ValueError):
        seq = 0
    return (scale_rank, -normalize_priority(priority), seq)
