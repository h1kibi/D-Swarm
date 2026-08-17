"""Coordinator-private, secret-free runtime pool diagnostics.

Runtime lifecycle is deliberately kept out of the evidence graph and canonical
event stream.  This module only records an allowlisted projection of a pool
view in the run's private ``.runtime`` sidecar.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any

from dswarm.solver.container_pool import RuntimePoolView

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def sanitize_pool_id(value: object, *, fallback: str = "pool") -> str:
    """Convert an external identifier into one non-traversing path component."""
    text = str(value or "").strip()
    # Preserve the readable ``pool-v1::`` delimiter while making every other
    # path/control character inert.  Capping a run avoids a huge attacker-
    # supplied separator sequence while retaining a stable readable name.
    text = text.replace("::", "__")
    text = re.sub(r"[^A-Za-z0-9_-]", "_", text)
    text = re.sub(r"_+", lambda match: "_" * min(len(match.group()), 5), text)
    text = text.strip("._-")[:128]
    if not text or text in {".", ".."}:
        return fallback
    return text


def _safe_token(value: object, *, fallback: str = "") -> str:
    text = str(value or "")
    return text if _SAFE_TOKEN_RE.fullmatch(text) else fallback


def _safe_int(value: object, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return minimum
    return max(minimum, value)


def _safe_reason(error: object, view: RuntimePoolView) -> str:
    if error is not None:
        return "runtime_operation_failed"
    if view.failure is not None:
        return _safe_token(view.failure.code, fallback="runtime_operation_failed")
    return "state_transition"


class RuntimeDiagnosticsStore:
    """Persist and read a sanitized private lifecycle projection for one run."""

    def __init__(self, *, run_root: str | os.PathLike[str], run_id: str) -> None:
        self.run_root = Path(run_root)
        self.run_id = sanitize_pool_id(run_id, fallback="run")
        self.root = self.run_root / ".runtime" / "pools"
        self._lock = threading.RLock()

    def _pool_component(self, pool_id: str) -> str:
        return sanitize_pool_id(pool_id)

    def _pool_dir(self, pool_id: str) -> Path:
        return self.root / self._pool_component(pool_id)

    def state_path(self, pool_id: str) -> Path:
        return self._pool_dir(pool_id) / "state.v1.json"

    def lifecycle_path(self, pool_id: str) -> Path:
        return self._pool_dir(pool_id) / "diagnostics" / "lifecycle.jsonl"

    def record_transition(
        self,
        view: RuntimePoolView,
        *,
        error: str | None = None,
        kind: str = "state_transition",
    ) -> dict[str, Any]:
        """Append one sanitized lifecycle row and atomically update state."""
        pool_id = self._pool_component(view.pool_id)
        failure = view.failure.snapshot() if view.failure is not None else None
        if failure is not None:
            failure = {
                "category": _safe_token(failure.get("category"), fallback="infrastructure"),
                "code": _safe_token(failure.get("code"), fallback="runtime_operation_failed"),
            }
        now = time.time()
        reason_code = _safe_reason(error, view)
        state: dict[str, Any] = {
            "run_id": self.run_id,
            "pool_id": pool_id,
            "state": _safe_token(view.state, fallback="unknown"),
            "generation": _safe_int(view.generation),
            "pool_instance_id": _safe_token(view.pool_instance_id),
            "active_workers": _safe_int(view.active_workers),
            "waiting_workers": _safe_int(view.waiting_workers),
            "capacity": _safe_int(view.capacity),
            "failure": failure,
            "reason_code": reason_code,
            "recovery_episode": _safe_int(view.recovery_episode),
            "updated_at": now,
        }
        row = {
            **state,
            "kind": _safe_token(kind, fallback="state_transition"),
            "actor": "",
        }
        with self._lock:
            self._ensure_private_root()
            self._write_state(self.state_path(pool_id), state)
            self._append_jsonl(self.lifecycle_path(pool_id), row)
        return row

    def read_lifecycle(self, pool_id: str) -> list[dict[str, Any]]:
        path = self.lifecycle_path(pool_id)
        if not path.exists():
            return []
        with self._lock:
            data = path.read_bytes()
        if not data:
            return []
        lines = data.splitlines(keepends=True)
        rows: list[dict[str, Any]] = []
        for index, raw in enumerate(lines):
            is_tail = index == len(lines) - 1 and not raw.endswith((b"\n", b"\r"))
            if not raw.strip():
                continue
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                if is_tail:
                    break
                raise ValueError("malformed_runtime_lifecycle")
            if not isinstance(value, dict):
                if is_tail:
                    break
                raise ValueError("malformed_runtime_lifecycle")
            rows.append(value)
        return rows

    def _ensure_private_root(self) -> None:
        for directory in (self.run_root / ".runtime", self.root):
            directory.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass

    @staticmethod
    def _write_state(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            with temporary.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        with path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


__all__ = ["RuntimeDiagnosticsStore", "sanitize_pool_id"]
