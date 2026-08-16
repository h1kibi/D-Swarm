"""Run-scoped route telemetry sidecar persistence.

This module deliberately does not publish graph events and is not an evidence
source.  It owns only the append-only JSONL metrics artifact plus the durable
consumer checkpoint used by low-frequency aggregation.
"""

from __future__ import annotations

import copy
import json
import math
import os
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

_SCHEMA_VERSION = 1
_DEFAULT_MAX_BYTES = 5 * 1024 * 1024
_DEFAULT_RETENTION_GENERATIONS = 3
_KINDS = frozenset(
    {
        "fact_appended",
        "dedupe_hit",
        "summary_recorded",
        "fact_projected",
        "fact_promoted",
    }
)


@dataclass(frozen=True)
class RouteMetricRecord:
    """One raw sidecar observation before sink-owned sequence assignment."""

    record_id: str
    kind: str
    challenge_id: str
    event_ts: float
    observed_at: float
    actor: str = ""
    fact_seq: int | None = None
    route_hash: str = ""
    route_lineage: str = "unattributed"
    lineage_reason: str = ""
    intent_ids: tuple[str, ...] = ()
    verified: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.record_id, str) or not self.record_id.strip():
            raise ValueError("record_id is required")
        if self.kind not in _KINDS:
            raise ValueError(f"unsupported route telemetry kind: {self.kind!r}")
        if not isinstance(self.challenge_id, str) or not self.challenge_id.strip():
            raise ValueError("challenge_id is required")
        for field_name, value in (
            ("event_ts", self.event_ts),
            ("observed_at", self.observed_at),
        ):
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"{field_name} must be finite")
        if self.fact_seq is not None and (
            not isinstance(self.fact_seq, int)
            or isinstance(self.fact_seq, bool)
            or self.fact_seq <= 0
        ):
            raise ValueError("fact_seq must be None or a positive integer")
        if not isinstance(self.verified, bool):
            raise ValueError("verified must be boolean")

    def to_row(self, *, run_id: str, record_seq: int) -> dict[str, Any]:
        intent_ids = sorted(
            {
                str(intent_id).strip()
                for intent_id in self.intent_ids
                if str(intent_id).strip()
            }
        )
        return {
            "schema_version": _SCHEMA_VERSION,
            "record_id": self.record_id.strip(),
            "record_seq": record_seq,
            "kind": self.kind,
            "run_id": run_id,
            "challenge_id": self.challenge_id.strip(),
            "event_ts": float(self.event_ts),
            "observed_at": float(self.observed_at),
            "actor": str(self.actor or ""),
            "fact_seq": self.fact_seq,
            "route_hash": str(self.route_hash or ""),
            "route_lineage": str(self.route_lineage or "unattributed"),
            "lineage_reason": str(self.lineage_reason or ""),
            "intent_ids": intent_ids,
            "verified": self.verified,
        }


@dataclass
class _PathState:
    lock: threading.RLock = field(default_factory=threading.RLock)
    seen_record_ids: set[str] = field(default_factory=set)
    last_record_id: str = ""
    last_record_seq: int = 0
    counters: dict[str, Any] = field(default_factory=dict)
    delta: dict[str, Any] = field(default_factory=dict)
    partial_lines_ignored: int = 0


_PATH_STATES_GUARD = threading.Lock()
_PATH_STATES: dict[str, _PathState] = {}


def _path_state(path: Path) -> _PathState:
    key = str(path.resolve())
    with _PATH_STATES_GUARD:
        state = _PATH_STATES.get(key)
        if state is None:
            state = _PathState()
            _PATH_STATES[key] = state
        return state


def _empty_counters() -> dict[str, Any]:
    return {
        "records_total": 0,
        "verified_total": 0,
        "by_kind": {},
        "by_lineage": {},
        "by_route": {},
    }


def _increment_map(target: dict[str, int], key: str) -> None:
    target[key] = int(target.get(key, 0)) + 1


def _increment_counters(target: dict[str, Any], row: Mapping[str, Any]) -> None:
    target["records_total"] = int(target.get("records_total", 0)) + 1
    if bool(row.get("verified", False)):
        target["verified_total"] = int(target.get("verified_total", 0)) + 1

    by_kind = target.setdefault("by_kind", {})
    _increment_map(by_kind, str(row.get("kind") or "unknown"))

    lineage = str(row.get("route_lineage") or "unattributed")
    by_lineage = target.setdefault("by_lineage", {})
    _increment_map(by_lineage, lineage)

    route = str(row.get("route_hash") or "unattributed")
    by_route = target.setdefault("by_route", {})
    route_counters = by_route.setdefault(
        route,
        {"records_total": 0, "verified_total": 0, "by_kind": {}},
    )
    route_counters["records_total"] = int(route_counters.get("records_total", 0)) + 1
    if bool(row.get("verified", False)):
        route_counters["verified_total"] = int(route_counters.get("verified_total", 0)) + 1
    _increment_map(route_counters.setdefault("by_kind", {}), str(row.get("kind") or "unknown"))


def _compact_counters(counters: Mapping[str, Any]) -> dict[str, Any]:
    if not counters or int(counters.get("records_total", 0)) == 0:
        return {}
    return copy.deepcopy(dict(counters))


class MetricsSink:
    """Append-only route telemetry artifact with replayable aggregation state."""

    def __init__(
        self,
        run_root: str | Path,
        *,
        run_id: str,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        retention_generations: int = _DEFAULT_RETENTION_GENERATIONS,
    ) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id is required")
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        if (
            not isinstance(retention_generations, int)
            or isinstance(retention_generations, bool)
            or retention_generations < 1
        ):
            raise ValueError("retention_generations must be at least 1")

        self.run_root = Path(run_root)
        self.run_id = run_id.strip()
        self.max_bytes = max_bytes
        self.retention_generations = retention_generations
        self.metrics_dir = self.run_root / "metrics"
        self.path = self.metrics_dir / "route-telemetry.jsonl"
        self.checkpoint_path = self.metrics_dir / "route-telemetry.checkpoint.json"
        self.metrics_dir.mkdir(parents=True, exist_ok=True)
        self._state = _path_state(self.path)
        with self._state.lock:
            self._recover_locked()

    @property
    def counters(self) -> dict[str, Any]:
        with self._state.lock:
            return copy.deepcopy(self._state.counters)

    @property
    def partial_lines_ignored(self) -> int:
        with self._state.lock:
            return self._state.partial_lines_ignored

    def append(self, record: RouteMetricRecord) -> bool:
        """Append one unique record; duplicates are successful no-ops."""
        if not isinstance(record, RouteMetricRecord):
            raise TypeError("record must be RouteMetricRecord")
        with self._state.lock:
            record_id = record.record_id.strip()
            if record_id in self._state.seen_record_ids:
                return False

            record_seq = self._state.last_record_seq + 1
            row = record.to_row(run_id=self.run_id, record_seq=record_seq)
            line = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()

            self._state.seen_record_ids.add(record_id)
            self._state.last_record_id = record_id
            self._state.last_record_seq = record_seq
            _increment_counters(self._state.counters, row)
            _increment_counters(self._state.delta, row)

            if self.path.stat().st_size >= self.max_bytes:
                self._rotate_locked()
            return True

    def aggregate_delta(self) -> dict[str, Any]:
        """Return and consume the in-memory delta after atomically checkpointing it."""
        with self._state.lock:
            delta = _compact_counters(self._state.delta)
            self._write_checkpoint_locked()
            self._state.delta = _empty_counters()
            return delta

    def _recover_locked(self) -> None:
        checkpoint = self._read_checkpoint_locked()
        checkpoint_seq = int(checkpoint.get("last_record_seq", 0))
        counters = copy.deepcopy(checkpoint.get("counters") or _empty_counters())
        partial_lines_ignored = int(checkpoint.get("partial_lines_ignored", 0))

        seen_record_ids: set[str] = set()
        last_record_id = str(checkpoint.get("last_record_id") or "")
        last_record_seq = checkpoint_seq
        delta = _empty_counters()

        for row, ignored in self._read_retained_rows_locked():
            partial_lines_ignored += ignored
            if row is None:
                continue
            record_id = str(row["record_id"])
            record_seq = int(row["record_seq"])
            if record_seq > last_record_seq:
                last_record_seq = record_seq
                last_record_id = record_id
            if record_id in seen_record_ids:
                continue
            seen_record_ids.add(record_id)
            if record_seq > checkpoint_seq:
                _increment_counters(counters, row)
                _increment_counters(delta, row)

        self._state.seen_record_ids = seen_record_ids
        self._state.last_record_id = last_record_id
        self._state.last_record_seq = last_record_seq
        self._state.counters = counters
        self._state.delta = delta
        self._state.partial_lines_ignored = partial_lines_ignored

    def _read_retained_rows_locked(self):
        paths = [
            Path(f"{self.path}.{generation}")
            for generation in range(self.retention_generations, 0, -1)
        ]
        paths.append(self.path)
        for path in paths:
            if not path.exists():
                continue
            data = path.read_bytes()
            if not data:
                continue
            lines = data.splitlines(keepends=True)
            offset = 0
            for index, raw in enumerate(lines):
                is_tail = index == len(lines) - 1
                terminated = raw.endswith((b"\n", b"\r"))
                stripped = raw.strip()
                if not stripped:
                    offset += len(raw)
                    continue
                try:
                    row = json.loads(stripped.decode("utf-8"))
                    validated = self._validate_row(row)
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, KeyError):
                    # A torn final append is expected after an abrupt exit.
                    # Remove that incomplete suffix after counting it once so
                    # later appends cannot concatenate onto corrupt JSON and a
                    # second restart cannot count the same damage again.
                    if is_tail and not terminated:
                        with path.open("r+b") as handle:
                            handle.truncate(offset)
                            handle.flush()
                        yield None, 1
                    else:
                        # A malformed complete line is skipped because metrics
                        # are sidecar data and may never block the solver.
                        yield None, 0
                    continue
                if is_tail and not terminated:
                    # A complete JSON object may survive a crash without its
                    # trailing newline. Repair the delimiter before a future
                    # append while retaining the valid observation.
                    with path.open("ab") as handle:
                        handle.write(b"\n")
                        handle.flush()
                yield validated, 0
                offset += len(raw)

    def _validate_row(self, row: Any) -> dict[str, Any]:
        if not isinstance(row, dict):
            raise ValueError("telemetry row must be an object")
        if row.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("unsupported telemetry schema")
        if row.get("run_id") != self.run_id:
            raise ValueError("telemetry run_id mismatch")
        record_id = row.get("record_id")
        record_seq = row.get("record_seq")
        if not isinstance(record_id, str) or not record_id.strip():
            raise ValueError("record_id is required")
        if not isinstance(record_seq, int) or isinstance(record_seq, bool) or record_seq <= 0:
            raise ValueError("record_seq must be positive")
        if row.get("kind") not in _KINDS:
            raise ValueError("unsupported telemetry kind")
        return row

    def _rotate_locked(self) -> None:
        oldest = Path(f"{self.path}.{self.retention_generations}")
        oldest.unlink(missing_ok=True)
        for generation in range(self.retention_generations - 1, 0, -1):
            source = Path(f"{self.path}.{generation}")
            if source.exists():
                os.replace(source, Path(f"{self.path}.{generation + 1}"))
        if self.path.exists():
            os.replace(self.path, Path(f"{self.path}.1"))
        self.path.touch()

    def _read_checkpoint_locked(self) -> dict[str, Any]:
        if not self.checkpoint_path.exists():
            return {}
        try:
            value = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if not isinstance(value, dict) or value.get("schema_version") != _SCHEMA_VERSION:
            return {}
        try:
            last_record_seq = int(value.get("last_record_seq", 0))
            partial_lines_ignored = int(value.get("partial_lines_ignored", 0))
        except (TypeError, ValueError):
            return {}
        if last_record_seq < 0 or partial_lines_ignored < 0:
            return {}
        counters = self._validated_counters(value.get("counters"))
        if counters is None:
            # A corrupt checkpoint cannot safely advance the replay cursor:
            # discard the whole checkpoint and rebuild totals from retained
            # JSONL instead of silently losing pre-cursor observations.
            return {}
        value["last_record_seq"] = last_record_seq
        value["partial_lines_ignored"] = partial_lines_ignored
        value["counters"] = counters
        return value

    @staticmethod
    def _validated_counters(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None

        def count(raw: Any) -> int:
            if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
                raise ValueError
            return raw

        def count_map(raw: Any) -> dict[str, int]:
            if not isinstance(raw, dict):
                raise ValueError
            return {str(key): count(item) for key, item in raw.items()}

        try:
            by_route_raw = value.get("by_route", {})
            if not isinstance(by_route_raw, dict):
                raise ValueError
            by_route: dict[str, dict[str, Any]] = {}
            for route, route_value in by_route_raw.items():
                if not isinstance(route_value, dict):
                    raise ValueError
                by_route[str(route)] = {
                    "records_total": count(route_value.get("records_total", 0)),
                    "verified_total": count(route_value.get("verified_total", 0)),
                    "by_kind": count_map(route_value.get("by_kind", {})),
                }
            return {
                "records_total": count(value.get("records_total", 0)),
                "verified_total": count(value.get("verified_total", 0)),
                "by_kind": count_map(value.get("by_kind", {})),
                "by_lineage": count_map(value.get("by_lineage", {})),
                "by_route": by_route,
            }
        except (TypeError, ValueError):
            return None

    def _write_checkpoint_locked(self) -> None:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "last_record_id": self._state.last_record_id,
            "last_record_seq": self._state.last_record_seq,
            "counters": self._state.counters,
            "partial_lines_ignored": self._state.partial_lines_ignored,
        }
        temporary = self.checkpoint_path.with_name(
            f"{self.checkpoint_path.name}.tmp-{uuid.uuid4().hex}"
        )
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    payload,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.checkpoint_path)
        finally:
            temporary.unlink(missing_ok=True)
