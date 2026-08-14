"""M1: intent priority normalization and stable ordering contracts."""

from __future__ import annotations

import math

import pytest

from dswarm.swarm.priority import (
    normalize_priority,
    normalize_priority_scale,
    priority_sort_key,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("operator", 100.0),
        ("high", 50.0),
        ("normal", 0.0),
        ("low", -10.0),
        (0.9, 0.9),
        ("0.5", 0.5),
        (None, 0.0),
        ("unknown", 0.0),
    ],
)
def test_normalize_priority(value, expected):
    assert normalize_priority(value) == expected


def test_normalize_priority_rejects_bool_and_non_finite_values():
    assert normalize_priority(True) == 0.0
    assert normalize_priority(math.nan) == 0.0
    assert normalize_priority(math.inf) == 0.0


def test_priority_scale_is_explicit_or_derived_from_operator_context():
    assert normalize_priority_scale("operator", raw_priority=0.1) == "operator"
    assert normalize_priority_scale("planner", actor="operator", raw_priority="operator") == "planner"
    assert normalize_priority_scale(None, actor="operator", raw_priority=-10) == "operator"
    assert normalize_priority_scale(None, source="operator_directive", raw_priority=0) == "operator"
    assert normalize_priority_scale(None, raw_priority="high") == "operator"
    assert normalize_priority_scale(None, raw_priority=0.9) == "planner"


def test_priority_sort_key_is_scale_first_and_fifo_for_equal_priority():
    rows = [
        ("planner-new", priority_sort_key("planner", 0.5, 4)),
        ("operator-low", priority_sort_key("operator", -10, 9)),
        ("planner-high", priority_sort_key("planner", 0.9, 3)),
        ("planner-old", priority_sort_key("planner", 0.5, 2)),
    ]
    assert [name for name, _ in sorted(rows, key=lambda item: item[1])] == [
        "operator-low",
        "planner-high",
        "planner-old",
        "planner-new",
    ]
