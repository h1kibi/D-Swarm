"""Shared filesystem helpers for credential and LLM-provider secret stores."""

from __future__ import annotations

import time
from pathlib import Path


def chmod_private_dir(path: Path) -> None:
    # Native Windows does not provide POSIX owner-only mode isolation here;
    # these bits are best-effort. The Docker/Linux runtime, not host staging,
    # is the production security boundary.
    try:
        path.chmod(0o700)
    except OSError:
        pass


def atomic_write(path: Path, text: str) -> None:
    """Write a file atomically and keep the private secret permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{int(time.time() * 1000)}.tmp")
    tmp.write_text(text, encoding="utf-8")
    # Keep requesting private-file permissions on POSIX; on Windows this is
    # only a best-effort hint and must not be treated as host isolation.
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    try:
        tmp.replace(path)
    except PermissionError:
        # Windows can briefly hold the destination during antivirus/indexer or
        # reader activity. A single short retry handles that transient lock
        # without masking a persistent replacement failure.
        time.sleep(0.01)
        tmp.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def updated_at(path: Path) -> float | None:
    """Newest mtime in a secret directory, or None when unreadable."""
    try:
        newest = path.stat().st_mtime
        for p in path.rglob("*"):
            try:
                newest = max(newest, p.stat().st_mtime)
            except OSError:
                pass
        return newest
    except OSError:
        return None
