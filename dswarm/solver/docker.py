"""Shared Docker subprocess helper for worker plumbing and image checks."""

from __future__ import annotations

import subprocess


def docker_run(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run docker with UTF-8 output and a bounded timeout."""
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
