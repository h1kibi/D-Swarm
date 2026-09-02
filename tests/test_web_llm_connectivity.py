"""Test-connectivity for the planner/titler LLM endpoint.

Tests the values the operator is editing, while resolving secrets only on the
server side from .env or a selected credential account.  Success is a real
OpenAI-compatible chat completion, not non-empty content and not /models alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional


async def test_llm_endpoint(
    *,
    which: str,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    sessions_root: str | Path | None = None,
    worker_profiles: list[dict[str, Any]] | None = None,
    llm_providers: list[dict[str, Any]] | None = None,
    provider_ref: Optional[str] = None,
    credential_account: Optional[str] = None,
    credential_source: Optional[str] = None,
    wire_api: Optional[str] = None,
) -> dict[str, Any]:
    """Make one minimal chat against the edited endpoint. Never raises.

    The request body supplies visible Planner/Titler fields; secrets are resolved
    server-side from .env or the selected credential account.  The authoritative
    pass/fail remains a real ``chat/completions`` call because relay ``/models``
    support is inconsistent.
    """
    from apps.web.reason_llm import probe_reason_llm_endpoint

    return await probe_reason_llm_endpoint(
        which=which,
        base_url=base_url,
        model=model,
        sessions_root=sessions_root,
        worker_profiles=worker_profiles or [],
        llm_providers=llm_providers or [],
        provider_ref=provider_ref,
        credential_account=credential_account,
        credential_source=credential_source,
        wire_api=wire_api,
    )
