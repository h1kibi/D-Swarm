"""Helpers for mapping external identities to filesystem names."""

from __future__ import annotations

import re
import hashlib


def safe_run_storage_key(run_id: str) -> str:
    """Return a deterministic filesystem-safe key for a run identity."""
    text = str(run_id or "")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", text):
        return text
    digest = hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
    return f"invalid-{digest}"
