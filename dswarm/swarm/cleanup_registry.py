"""Typed, run-scoped cleanup actions.

Workers may register *descriptions* of cleanup work, never shell commands.  The
coordinator only executes actions from this small allowlist and every target is
bound to the actor/run at registration time.  This module is deliberately free
of process/shell execution; concrete executors are injected by the run owner.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

CLEANUP_ACTION_TYPES = frozenset({
    "remove_artifact",
    "stop_listener",
    "close_session",
    "revoke_credential",
})

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
_SAFE_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def _clean_field(value: Any, *, field: str, max_length: int = 512) -> str:
    text = str(value or "").strip()
    if not text or len(text) > max_length or any(
        ord(char) < 32 or ord(char) == 127 for char in text
    ):
        raise ValueError(f"invalid cleanup {field}")
    return text


def validate_cleanup_action(
    action_type: Any,
    target: Any,
    *,
    actor: Any = "",
    owner_key: Any = "",
) -> tuple[str, str, str, str]:
    """Validate one typed action and return canonical fields.

    ``target`` is an opaque resource identifier except for ``remove_artifact``.
    Artifact targets are workspace-relative and must stay below ``workers/``;
    this prevents a worker from registering deletion of graph/shared state or a
    host path.  The other resource kinds are still opaque IDs, never argv/text.
    """
    kind = _clean_field(action_type, field="type", max_length=64).lower()
    target_text = _clean_field(target, field="target")
    actor_text = _clean_field(actor, field="actor", max_length=256) if actor else ""
    # Marker parsing happens before the caller attaches run ownership. Keep resource
    # validation usable there; registration supplies and validates actor/owner.
    owner_text = (
        _clean_field(owner_key, field="owner", max_length=256)
        if owner_key else actor_text
    )
    if kind not in CLEANUP_ACTION_TYPES:
        raise ValueError("unsupported cleanup action type")
    if not _SAFE_ID.fullmatch(target_text):
        raise ValueError("cleanup target contains unsupported characters")
    if kind == "remove_artifact":
        parts = target_text.replace("\\", "/").split("/")
        if target_text.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", target_text):
            raise ValueError("artifact cleanup target must be run-relative")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("artifact cleanup target must be canonical")
        if parts[0] != "workers":
            raise ValueError("artifact cleanup target must stay under workers/")
    if owner_text and not _SAFE_OWNER.fullmatch(owner_text):
        raise ValueError("invalid cleanup owner")
    if actor_text and not _SAFE_OWNER.fullmatch(actor_text):
        raise ValueError("invalid cleanup actor")
    return kind, target_text, actor_text, owner_text


def parse_cleanup_marker(value: Any) -> tuple[str, str]:
    """Parse ``CLEANUP=<typed_action>:<target>``; reject raw commands."""
    text = str(value or "").strip()
    if text.count(":") < 1:
        raise ValueError("cleanup marker must be <action_type>:<target>")
    action_type, target = text.split(":", 1)
    kind, target_text, _actor, _owner = validate_cleanup_action(action_type, target)
    return kind, target_text


def cleanup_action_id(
    *, challenge_id: str, actor: str, action_type: str, target: str,
    intent_id: str = "", idempotency_key: str = "",
) -> str:
    """Return a stable ID so repeated worker markers are harmless."""
    material = "|".join((challenge_id, actor, action_type, target, intent_id, idempotency_key))
    return "cleanup-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def public_cleanup_target(target: Any) -> dict[str, Any]:
    """Return a UI-safe target representation without exposing raw resource text."""
    text = str(target or "")
    return {
        "target_digest": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "",
        "target_length": len(text),
    }



def public_cleanup_text(value: Any, *, prefix: str) -> dict[str, Any]:
    """Return digest/length metadata for any private cleanup detail."""
    text = str(value or "")
    return {
        f"{prefix}_digest": hashlib.sha256(text.encode("utf-8")).hexdigest() if text else "",
        f"{prefix}_length": len(text),
    }
