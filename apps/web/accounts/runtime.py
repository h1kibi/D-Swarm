"""Account-system runtime configuration guards.

The account JWTs are HMAC-signed; an unset ``DSWARM_JWT_SECRET`` falls back to a
public dev default so local development works out of the box. That fallback is
only acceptable while the backend stays on loopback — reachable from anywhere
else it means anyone can forge an admin token. ``create_app`` calls
``require_jwt_secret_for_public_bind`` next to the P3 password fail-fast.
"""
from __future__ import annotations

import os

from apps.web.auth import BIND_ENV
from dswarm.core.runtime_env import is_loopback_host

JWT_SECRET_ENV = "DSWARM_JWT_SECRET"
DEV_JWT_SECRET = "dev-secret-change-in-production"


def jwt_secret() -> str:
    """The account-JWT signing secret (env, else the guarded dev fallback)."""
    return os.getenv(JWT_SECRET_ENV) or DEV_JWT_SECRET


def require_jwt_secret_for_public_bind() -> None:
    """Refuse to start on a non-loopback bind without an explicit JWT secret."""
    if os.getenv(JWT_SECRET_ENV):
        return
    if is_loopback_host(os.getenv(BIND_ENV, "")):
        return
    raise RuntimeError(
        "DSWARM_JWT_SECRET must be set when binding to a non-loopback host: "
        "account tokens would otherwise be signed with a public default secret."
    )
