"""Draft-safe endpoint probe facade used by the web API."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dswarm.solver.credential_accounts import account_store_root, runtime_env_for_engine
from dswarm.solver.endpoint_probe import (
    auth_headers,
    endpoint_url,
    error_message,
    model_request,
    normalize_auth,
    normalize_base_url,
    normalize_wire_api,
    parse_models,
    probe_endpoint,
    protocol_mismatch,
)

probe_worker_endpoint = probe_endpoint

# Compatibility aliases for the original web-local probe helpers. Keeping these
# names avoids breaking downstream imports while the implementation lives in the
# shared solver module used by both the settings API and EndpointDriver.
_models = parse_models
_error_message = error_message
_protocol_mismatch = protocol_mismatch
_model_request = model_request


def resolve_saved_api_key(profile: dict[str, Any], sessions_root: str | Path) -> str:
    """Resolve a saved endpoint key without exposing it outside the probe."""
    ref = str(profile.get("api_key_ref") or "").strip()
    if ref.startswith("env:"):
        return os.environ.get(ref[4:], "").strip()
    if ref.startswith("file:"):
        try:
            return Path(ref[5:]).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    account_id = str(profile.get("credential_account") or "").strip() or None
    resolved = runtime_env_for_engine(
        "pi", account_root=account_store_root(sessions_root), account_id=account_id,
        container=False, env={**os.environ, "DSWARM_PI_PROVIDER": "openai"},
    ).env
    file_ref = str(resolved.get("OPENAI_API_KEY_FILE") or "").strip()
    if file_ref:
        try:
            return Path(file_ref).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return str(resolved.get("OPENAI_API_KEY") or "").strip()


__all__ = [
    "auth_headers", "endpoint_url", "normalize_auth", "normalize_base_url",
    "normalize_wire_api", "parse_models", "probe_worker_endpoint",
    "resolve_saved_api_key", "_models", "_error_message",
    "_protocol_mismatch", "_model_request",
]
