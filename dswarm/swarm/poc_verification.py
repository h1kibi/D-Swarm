"""Bounded, append-only lifecycle data for pentest Verified-PoC runs.

This module intentionally contains no command execution and no flag-verification
logic.  It only defines the immutable registration identity, closed terminal
failure vocabulary, and public-text bounds used by the graph layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import re
from typing import Any, Optional


_MAX_INDICATOR_CHARS = 512
_MAX_INDICATOR_BYTES = 2048
_MAX_PUBLIC_TEXT = 240
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_FLAG_LIKE = re.compile(r"(?:flag\s*\{|ctf\s*\{|[a-z0-9_-]{2,16}\s*\{[^\n]{1,480}\})", re.I)
_REDACTED = re.compile(r"(?:\[redacted\]|<redacted>|\*{3,})", re.I)


class VerificationFailure(str, Enum):
    MISSING_REPRODUCTION = "missing_reproduction"
    DOCKER_RUNTIME_UNAVAILABLE = "docker_runtime_unavailable"
    LEASE_UNAVAILABLE = "lease_unavailable"
    ARTIFACT_UNAVAILABLE = "artifact_unavailable"
    COMMAND_REJECTED = "command_rejected"
    TIMED_OUT = "timed_out"
    EXECUTION_ERROR = "execution_error"
    NONZERO_EXIT = "nonzero_exit"
    INDICATOR_NOT_OBSERVED = "indicator_not_observed"
    PROVENANCE_UNAVAILABLE = "provenance_unavailable"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ReproductionRegistration:
    poc_id: str
    reproduction_id: str
    artifact_id: str
    command: str
    indicator: str
    registration_seq: int = 0
    verification_status: str = "registered"


@dataclass(frozen=True)
class VerificationResult:
    verified: bool
    exit_code: Optional[int] = None
    failure_reason: Optional[VerificationFailure] = None
    observed_location: str = ""
    provenance_artifact_ids: tuple[str, ...] = field(default_factory=tuple)
    diagnostics: str = ""
    elapsed_ms: Optional[int] = None


def normalize_reproduction_indicator(value: str) -> str:
    """Normalize one bounded observable line, rejecting unsafe/secret-like text."""
    if not isinstance(value, str):
        raise ValueError("indicator must be text")
    if _CONTROL_CHARS.search(value):
        raise ValueError("indicator must be one logical line")
    normalized = value.strip()
    if not normalized:
        raise ValueError("indicator must not be empty")
    if _CONTROL_CHARS.search(normalized):
        raise ValueError("indicator must be one logical line")
    if len(normalized) > _MAX_INDICATOR_CHARS:
        raise ValueError("indicator is too long")
    if len(normalized.encode("utf-8")) > _MAX_INDICATOR_BYTES:
        raise ValueError("indicator is too large")
    if _FLAG_LIKE.search(normalized):
        raise ValueError("indicator is flag-like")
    if _REDACTED.search(normalized):
        raise ValueError("indicator is redacted or secret-like")
    return normalized


def reproduction_id_for(*, artifact_id: str, command: str, indicator: str) -> str:
    """Return the stable identity for one immutable command/indicator pair."""
    artifact = str(artifact_id or "").strip()
    executable = str(command or "").strip()
    normalized = normalize_reproduction_indicator(indicator)
    if not artifact:
        raise ValueError("artifact_id is required")
    if not executable:
        raise ValueError("command is required")
    digest = hashlib.sha256(
        f"{executable}\x00{normalized}".encode("utf-8")
    ).hexdigest()
    return f"poc-repro::{artifact}::{digest}"


def sanitize_public_text(value: Any, *, limit: int = _MAX_PUBLIC_TEXT) -> str:
    """Bound diagnostics without allowing control characters into public payloads."""
    text = _CONTROL_CHARS.sub(" ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[: max(0, int(limit))]


def verification_failure_value(value: VerificationFailure | str) -> str:
    """Validate and normalize a closed terminal failure reason."""
    if isinstance(value, VerificationFailure):
        return value.value
    try:
        return VerificationFailure(str(value)).value
    except ValueError as exc:
        raise ValueError("unknown verification failure reason") from exc
