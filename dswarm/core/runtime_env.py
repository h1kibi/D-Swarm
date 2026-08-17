"""Shared runtime-environment and Web launch-policy helpers.

The coordinator must fail closed before starting Docker or a host-local uvicorn
process when a Web publication is non-loopback without a password. The same
policy is consumed by the shell launcher and the Python Web app.
"""

from __future__ import annotations

import ipaddress
import os

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class WebLaunchConfigError(ValueError):
    """Raised when a Web control-plane bind violates the launch policy."""

    code = "web_password_required_for_non_loopback"


def _normalize_host(host: str | None) -> str:
    value = (host or "").strip().lower()
    if value.startswith("[") and "]" in value:
        return value[1:value.index("]")]
    if value.count(":") == 1:
        # Accept the host:port spelling used by environment/configuration
        # without treating a bare IPv6 address as a port-bearing value.
        return value.split(":", 1)[0]
    return value


def is_loopback_host(host: str | None) -> bool:
    """Return whether a bind/publication host is loopback or unset.

    Wildcards, LAN addresses, and non-localhost hostnames are intentionally not
    loopback. Any IP in the loopback range is accepted, not only 127.0.0.1.
    """
    normalized = _normalize_host(host)
    if normalized in {"", "localhost"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_web_launch(
    *,
    public_host: str | None,
    password: str | None,
    internal_bind: str | None = None,
    trusted_control_plane: bool = False,
) -> None:
    """Validate Web exposure before starting Docker, uvicorn, or Compose.

    ``public_host`` is the address published to the operator's host/network and
    is always authoritative for password enforcement. ``internal_bind`` may be a
    wildcard inside a trusted Compose control-plane container; it is not treated
    as public exposure in that case. A bare/local process may not hide a wildcard
    internal bind behind a loopback publication.
    """
    password_configured = bool((password or "").strip())
    public_is_loopback = is_loopback_host(public_host)
    if not public_is_loopback and not password_configured:
        raise WebLaunchConfigError(
            f"{WebLaunchConfigError.code}: non-loopback Web publication requires "
            "DSWARM_WEB_PASSWORD"
        )

    if (
        public_is_loopback
        and internal_bind
        and not is_loopback_host(internal_bind)
        and not trusted_control_plane
        and not password_configured
    ):
        raise WebLaunchConfigError(
            f"{WebLaunchConfigError.code}: non-loopback internal Web bind requires "
            "DSWARM_WEB_PASSWORD"
        )


def is_web_container() -> bool:
    """True if THIS process (the coordinator / web-api) is running inside a container.

    Cheap and side-effect-free; safe to call on hot paths. Re-reads the environment
    each call so a test can monkeypatch DSWARM_IN_CONTAINER without reimporting.
    """
    explicit = os.environ.get("DSWARM_IN_CONTAINER")
    if explicit is not None:
        v = explicit.strip().lower()
        if v in _TRUE:
            return True
        if v in _FALSE:
            return False
        return True
    try:
        if os.path.exists("/.dockerenv"):
            return True
    except OSError:
        pass
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8", errors="replace") as fh:
            blob = fh.read()
        if any(tok in blob for tok in ("docker", "containerd", "kubepods")):
            return True
    except OSError:
        pass
    return False
