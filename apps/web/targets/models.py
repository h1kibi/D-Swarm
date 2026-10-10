"""Target domain primitives for the target-first web control plane."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Target:
    id: str
    name: str
    kind: str = "ctf"
    category: str = "misc"
    description: str = ""
    target_address: str | None = None
    attachments: list[str] = field(default_factory=list)
    expected_flags: int = 1
    multi_flag: bool = False
    flag_format_hint: str = ""
    primary_direction_id: str | None = None
    directions: list[dict[str, Any]] = field(default_factory=list)
    hints: list[dict[str, Any]] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    verified_facts: list[dict[str, Any]] = field(default_factory=list)
    # Operator-approved review records retain source/provenance metadata. They
    # are a Target history projection and never accepted flag evidence.
    audit_records: list[dict[str, Any]] = field(default_factory=list)
    # Durable operator/run lifecycle index for the Target detail timeline.
    # Run event logs remain the execution authority; this is only a UI projection.
    activity: list[dict[str, Any]] = field(default_factory=list)
    archived_at: float | None = None
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "category": self.category,
            "description": self.description,
            "target_address": self.target_address,
            "attachments": list(self.attachments),
            "expected_flags": self.expected_flags,
            "multi_flag": self.multi_flag,
            "flag_format_hint": self.flag_format_hint,
            "primary_direction_id": self.primary_direction_id,
            "directions": list(self.directions),
            "hints": list(self.hints),
            "run_ids": list(self.run_ids),
            "verified_facts": list(self.verified_facts),
            "audit_records": list(self.audit_records),
            "activity": list(self.activity),
            "archived_at": self.archived_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
