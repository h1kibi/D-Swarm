"""M7 energy cycle sidecar: append-only JSONL segments + two-dim manifest.

Implements the crash-recovery protocol from docs/10 Contract v9.2:

- line kinds: cycle_started / cycle_trace / resume_epoch;
- durable attempt protocol: cycle_started (flush+fsync) BEFORE capture, and
  cycle_trace (flush+fsync) BEFORE _register_decision/dispatch;
- record append failure contract: sample loss never blocks dispatch, but
  recorder_dirty + in-memory quality downgrade + best-effort durable downgrade;
- resume guard (dataset/process resume only): resume_epoch is ALWAYS written
  first (single path); append failure fails fast before any dispatch; manifest
  flip afterwards is best-effort;
- complete is a DERIVED predicate: finalized ∧ clean ∧
  finalized_resume_epoch_id == last_resume_epoch_id ∧ no orphan/corrupt ∧ all
  traces complete ∧ no malformed ∧ total ≤ MAX_RUN_TRACE_BYTES ∧ identity.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from dswarm.swarm.energy import (
    MAX_RUN_TRACE_BYTES,
    MAX_SEGMENT_BYTES,
    MAX_TRACE_BYTES,
    SCHEMA_VERSION,
    CycleTrace,
    SizeFixedPointError,
    build_size_stub,
    encode_cycle_trace_line,
)

MANIFEST_NAME = "energy-cycle-traces.manifest.json"
SEGMENT_PREFIX = "energy-cycle-traces"
_SEGMENT_RE = re.compile(r"^energy-cycle-traces\.(\d{6})\.jsonl$")

LIFECYCLE_IN_PROGRESS = "in_progress"
LIFECYCLE_FINALIZED = "finalized"
QUALITY_CLEAN = "clean"
QUALITY_INCOMPLETE = "incomplete"
QUALITY_CORRUPT = "corrupt"
_QUALITY_RANK = {QUALITY_CLEAN: 0, QUALITY_INCOMPLETE: 1, QUALITY_CORRUPT: 2}


class ResumeGuardError(RuntimeError):
    """The dataset/process resume witness could not be persisted: fail fast."""


def _max_quality(a: str, b: str) -> str:
    return a if _QUALITY_RANK[a] >= _QUALITY_RANK[b] else b


def _new_manifest(run_id: str, created_ts: float) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": str(run_id),
        "lifecycle_status": LIFECYCLE_IN_PROGRESS,
        "data_quality": QUALITY_CLEAN,
        "exclusion_reasons": [],
        "created_ts": float(created_ts),
        "finalized_ts": 0.0,
        "finalized_resume_epoch_id": "",
        "cycles_started": 0,
        "cycles_written": 0,
        "cycles_failed": 0,
        "cycles_excluded": 0,
        "segment_count": 0,
        "total_trace_bytes": 0,
        "max_trace_bytes": MAX_TRACE_BYTES,
        "max_run_trace_bytes": MAX_RUN_TRACE_BYTES,
        "max_segment_bytes": MAX_SEGMENT_BYTES,
        "first_trace_id": "",
        "last_trace_id": "",
    }


@dataclass
class EnergyDatasetFold:
    """Result of the global cross-segment fold (physical append order)."""

    cycles_started: int = 0
    cycles_written: int = 0
    cycles_complete: int = 0
    cycles_excluded: int = 0
    cycles_failed: int = 0
    total_trace_bytes: int = 0
    first_trace_id: str = ""
    last_trace_id: str = ""
    last_resume_epoch_id: str = ""
    corrupt: bool = False
    orphan_started: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def identity_holds(self) -> bool:
        return self.cycles_started == self.cycles_written + self.cycles_failed


class EnergyTraceSink:
    """Run-scoped sidecar writer + dataset integrity gate (docs/10 M7)."""

    def __init__(
        self,
        run_root: str | Path,
        *,
        run_id: str,
        challenge_id: str = "",
        enabled: bool = True,
    ) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id is required")
        self.run_root = Path(run_root)
        self.run_id = run_id.strip()
        self.challenge_id = str(challenge_id or "")
        self.enabled = bool(enabled)
        self.metrics_dir = self.run_root / "metrics"
        self.manifest_path = self.metrics_dir / MANIFEST_NAME
        self._lock = threading.RLock()
        self._dirty = False
        self._quality = QUALITY_CLEAN
        self._last_resume_epoch_id = ""
        self._current_segment = 0
        self._manifest: dict[str, Any] = _new_manifest(self.run_id, time.time())

        existing = self._dataset_exists()
        if not existing and not self.enabled:
            # Strictly zero side effects for a disabled fresh run (docs/10 test 103).
            return
        self.metrics_dir.mkdir(parents=True, exist_ok=True)
        if not existing:
            # Fresh enabled run: manifest only; no resume epoch.
            self._write_manifest(self._manifest)
            return
        # Dataset resume: guard runs regardless of enabled.
        self._open_dataset()
        self._resume_guard()

    # ------------------------------------------------------------------ paths

    def _dataset_exists(self) -> bool:
        if self.manifest_path.exists():
            return True
        return bool(self._segment_files())

    def _segment_files(self) -> list[Path]:
        if not self.metrics_dir.exists():
            return []
        out: list[Path] = []
        for path in self.metrics_dir.glob(f"{SEGMENT_PREFIX}.*.jsonl"):
            if _SEGMENT_RE.match(path.name):
                out.append(path)
        out.sort(key=lambda p: int(_SEGMENT_RE.match(p.name).group(1)))
        return out

    def _segment_path(self, index: int) -> Path:
        return self.metrics_dir / f"{SEGMENT_PREFIX}.{index:06d}.jsonl"

    # ------------------------------------------------------- manifest writing

    def _write_manifest(self, manifest: dict[str, Any]) -> bool:
        """temp -> flush -> fsync -> replace; fsync parent dir on POSIX (best
        effort). Failure keeps the previous manifest and marks the recorder
        dirty (no finalized+clean can ever be claimed)."""
        try:
            temp = self.manifest_path.with_suffix(".json.tmp")
            payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"))
            with temp.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.manifest_path)
            if os.name == "posix":
                try:
                    dir_fd = os.open(str(self.metrics_dir), os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except OSError:
                    pass  # directory fsync is best-effort, unsupported on Windows
            return True
        except Exception:
            self._dirty = True
            try:  # best-effort cleanup must not mask the primary failure
                temp.unlink(missing_ok=True)
            except Exception:
                pass
            return False

    def _load_manifest(self) -> dict[str, Any] | None:
        try:
            raw = self.manifest_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict) or not data.get("run_id"):
                return None
            return data
        except Exception:
            return None

    # ------------------------------------------------------------ appending

    def _append_line(self, line: bytes, *, failure_reason: str | None) -> bool:
        """Append one durable line (flush + fsync) with rotate-before-append.

        ``failure_reason=None`` means the failure is fatal to the caller (resume
        guard); otherwise the record append failure contract runs."""
        with self._lock:
            try:
                text = line + b"\n"
                segment = self._segment_path(self._current_segment)
                size = segment.stat().st_size if segment.exists() else 0
                if size + len(text) > MAX_SEGMENT_BYTES:
                    self._current_segment += 1
                    segment = self._segment_path(self._current_segment)
                with segment.open("ab") as handle:
                    handle.write(text)
                    handle.flush()
                    os.fsync(handle.fileno())
                return True
            except Exception:
                if failure_reason is not None:
                    self._on_append_failure(failure_reason)
                return False

    def _on_append_failure(self, reason: str) -> None:
        """Record append failure contract: never raise, never block dispatch."""
        self._dirty = True
        self._downgrade_quality(QUALITY_INCOMPLETE, reason)
        self._write_manifest(self._manifest)  # best-effort durable downgrade

    def _downgrade_quality(self, quality: str, reason: str) -> None:
        self._quality = _max_quality(self._quality, quality)
        reasons = self._manifest.get("exclusion_reasons") or []
        if reason and reason not in reasons:
            self._manifest["exclusion_reasons"] = [*reasons, reason]
        self._manifest["data_quality"] = self._quality

    # ------------------------------------------------------------ open/guard

    def _open_dataset(self) -> None:
        segments = self._segment_files()
        if segments:
            self._truncate_partial_tail(segments[-1])
            self._current_segment = int(
                _SEGMENT_RE.match(segments[-1].name).group(1))
        manifest = self._load_manifest()
        if manifest is None:
            # Missing/corrupt manifest but segments exist: rebuild from the
            # fold; quality is at least incomplete, NEVER clean (docs/10).
            fold = self._fold()
            manifest = _new_manifest(self.run_id, time.time())
            manifest["cycles_started"] = fold.cycles_started
            manifest["cycles_written"] = fold.cycles_written
            manifest["cycles_failed"] = fold.cycles_failed
            manifest["cycles_excluded"] = fold.cycles_excluded
            manifest["total_trace_bytes"] = fold.total_trace_bytes
            manifest["first_trace_id"] = fold.first_trace_id
            manifest["last_trace_id"] = fold.last_trace_id
            manifest["segment_count"] = len(self._segment_files())
            quality = QUALITY_CORRUPT if fold.corrupt else QUALITY_INCOMPLETE
            manifest["data_quality"] = quality
            manifest["exclusion_reasons"] = list(fold.reasons) or [
                "manifest_missing_or_corrupt"]
        self._manifest = manifest
        self._quality = str(manifest.get("data_quality") or QUALITY_CLEAN)

    def _truncate_partial_tail(self, segment: Path) -> None:
        """Reopen: only the last segment's last line may be partial; truncate
        to the final newline + fsync before appending."""
        try:
            raw = segment.read_bytes()
        except OSError:
            return
        if not raw or raw.endswith(b"\n"):
            return
        last_nl = raw.rfind(b"\n")
        keep = last_nl + 1 if last_nl >= 0 else 0
        if keep == len(raw):
            return
        with segment.open("r+b") as handle:
            handle.truncate(keep)
            handle.flush()
            os.fsync(handle.fileno())

    def _resume_guard(self) -> None:
        """Dataset/process resume guard (docs/10 v9.2): resume_epoch FIRST."""
        manifest = self._manifest
        if manifest.get("lifecycle_status") == LIFECYCLE_IN_PROGRESS:
            # unclean reopen rule (step 0), idempotent
            self._downgrade_quality(QUALITY_INCOMPLETE, "unclean_reopen")
            self._write_manifest(self._manifest)  # best effort
        resume_epoch_id = str(uuid.uuid4())
        line = json.dumps({
            "kind": "resume_epoch",
            "resume_epoch_id": resume_epoch_id,
            "resume_ts": time.time(),  # audit/display only, never causal
            "prior_lifecycle": manifest.get("lifecycle_status", ""),
            "prior_data_quality": manifest.get("data_quality", ""),
            "schema_version": SCHEMA_VERSION,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if not self._append_line(line.encode("utf-8"), failure_reason=None):
            raise ResumeGuardError(
                "energy dataset resume witness (resume_epoch) could not be "
                "persisted; refusing to start so no uncaptured cycle is "
                "dispatched")
        self._last_resume_epoch_id = resume_epoch_id
        # Best-effort manifest flip. Failure is fine: the epoch is the witness.
        self._manifest["lifecycle_status"] = LIFECYCLE_IN_PROGRESS
        if not self.enabled:
            self._downgrade_quality(QUALITY_INCOMPLETE, "resume_without_energy_trace")
        # prior quality != clean (enabled resume of a degraded dataset): keep
        # the historical quality and reasons; do NOT add resume_without_energy_trace.
        self._write_manifest(self._manifest)

    # --------------------------------------------------------------- cycles

    def start_cycle(self, trace_id: str, *, reason_cycle_id: str,
                    decision_ts: float) -> bool:
        """Durable cycle_started BEFORE capture (docs/10 attempt protocol)."""
        if not self.enabled:
            return False
        line = json.dumps({
            "kind": "cycle_started",
            "trace_id": str(trace_id),
            "schema_version": SCHEMA_VERSION,
            "reason_cycle_id": str(reason_cycle_id or ""),
            "decision_ts": float(decision_ts),
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return self._append_line(line.encode("utf-8"),
                                 failure_reason="cycle_started_append_failed")

    def write_trace(self, trace: CycleTrace) -> bool:
        """Durable cycle_trace (full record or oversize stub) BEFORE dispatch."""
        if not self.enabled:
            return False
        try:
            line_bytes, length = encode_cycle_trace_line(trace)
        except SizeFixedPointError:
            line_bytes, length = self._encode_fixed_point_failed_stub(trace)
        if length > MAX_TRACE_BYTES:
            stub = build_size_stub(trace, full_attempted_bytes=length)
            try:
                line_bytes, length = encode_cycle_trace_line(stub)
            except SizeFixedPointError:
                line_bytes, length = self._encode_fixed_point_failed_stub(stub)
        ok = self._append_line(line_bytes,
                               failure_reason="cycle_trace_append_failed")
        if ok:
            assert length == len(line_bytes), "serialized_bytes must equal line bytes"
        return ok

    def _encode_fixed_point_failed_stub(self, trace: CycleTrace) -> tuple[bytes, int]:
        from dswarm.swarm.energy import CycleTrace, GraphCycleSnapshot
        snapshot = trace.snapshot
        stripped = GraphCycleSnapshot(
            graph_after_seq=snapshot.graph_after_seq,
            observations=(),
            dead_ends=(),
            complete=False,
            exclusion_reason="size_fixed_point_failed",
            observed_fact_count=snapshot.observed_fact_count,
            captured_fact_count=snapshot.captured_fact_count,
            stored_fact_count=0,
        )
        stub = CycleTrace(
            schema_version=trace.schema_version,
            trace_id=trace.trace_id,
            reason_cycle_id=trace.reason_cycle_id,
            decision_ts=trace.decision_ts,
            expected_decision_count=trace.expected_decision_count,
            decisions=trace.decisions,
            snapshot=stripped,
            complete=False,
            exclusion_reason="size_fixed_point_failed",
            serialized_bytes=0,
            serialized_bytes_attempted=None,
        )
        self._dirty = True
        self._downgrade_quality(QUALITY_INCOMPLETE, "size_fixed_point_failed")
        return encode_cycle_trace_line(stub)

    # ----------------------------------------------------------------- fold

    @classmethod
    def readonly_fold(cls, run_root: str | Path, *, run_id: str) -> "EnergyDatasetFold":
        """Fold a dataset WITHOUT opening it (no resume guard, no truncation,
        no manifest writes) — used by the offline replay/report path so that
        reading never mutates the dataset."""
        obj = cls.__new__(cls)
        obj.run_root = Path(run_root)
        obj.run_id = run_id
        obj.metrics_dir = obj.run_root / "metrics"
        obj._lock = threading.RLock()
        obj._current_segment = 0
        return obj.fold()

    def fold(self) -> EnergyDatasetFold:
        with self._lock:
            return self._fold()

    def _fold(self) -> EnergyDatasetFold:
        fold = EnergyDatasetFold()
        segments = self._segment_files()
        started: dict[str, int] = {}
        traces: dict[str, int] = {}
        seen_epochs: dict[str, str] = {}  # id -> raw line (identical-dup check)
        last_segment_index = len(segments) - 1
        stop_scan = False
        for seg_index, segment in enumerate(segments):
            if stop_scan:
                break
            try:
                raw = segment.read_bytes()
            except OSError:
                fold.corrupt = True
                fold.reasons.append("segment_unreadable")
                break
            lines = raw.split(b"\n")
            for line_index, line in enumerate(lines):
                if not line.strip():
                    continue
                is_tail = (seg_index == last_segment_index
                           and line_index == len(lines) - 1)
                try:
                    record = json.loads(line.decode("utf-8"))
                except Exception:
                    if is_tail and not raw.endswith(b"\n"):
                        break  # partial tail of the last segment: ignorable
                    fold.corrupt = True
                    fold.reasons.append("malformed_line")
                    stop_scan = True
                    break
                if not isinstance(record, dict):
                    fold.corrupt = True
                    fold.reasons.append("malformed_line")
                    stop_scan = True
                    break
                kind = record.get("kind")
                if kind == "cycle_started":
                    trace_id = str(record.get("trace_id") or "")
                    started[trace_id] = started.get(trace_id, 0) + 1
                    if started[trace_id] > 1:
                        fold.corrupt = True
                        fold.reasons.append("duplicate_started")
                        stop_scan = True
                        break
                elif kind == "cycle_trace":
                    trace_id = str(record.get("trace_id") or "")
                    traces[trace_id] = traces.get(trace_id, 0) + 1
                    if traces[trace_id] > 1:
                        fold.corrupt = True
                        fold.reasons.append("duplicate_trace")
                        stop_scan = True
                        break
                    fold.total_trace_bytes += len(line)
                    fold.last_trace_id = trace_id
                    if not fold.first_trace_id:
                        fold.first_trace_id = trace_id
                    fold.cycles_written += 1
                    if bool(record.get("complete")):
                        fold.cycles_complete += 1
                    else:
                        fold.cycles_excluded += 1
                elif kind == "resume_epoch":
                    epoch_id = str(record.get("resume_epoch_id") or "")
                    line_text = line.decode("utf-8")
                    if epoch_id in seen_epochs:
                        if seen_epochs[epoch_id] != line_text:
                            fold.corrupt = True
                            fold.reasons.append("resume_epoch_content_mismatch")
                            stop_scan = True
                            break
                        continue  # identical duplicate: idempotent fold
                    seen_epochs[epoch_id] = line_text
                    fold.last_resume_epoch_id = epoch_id
                else:
                    fold.corrupt = True
                    fold.reasons.append(f"unknown_kind:{kind}")
                    stop_scan = True
                    break
        fold.cycles_started = len(started)
        for trace_id in started:
            if trace_id not in traces:
                fold.orphan_started.append(trace_id)
                fold.cycles_failed += 1
        for trace_id in traces:
            if trace_id not in started:
                fold.corrupt = True
                fold.reasons.append("trace_without_started")
        if fold.cycles_started != fold.cycles_written + fold.cycles_failed:
            fold.corrupt = True
            fold.reasons.append("count_identity_violation")
        return fold

    # --------------------------------------------------------------- finalize

    def finalize(self) -> bool:
        """Write finalized + ack of the last resume epoch. Never claims clean
        when the recorder is dirty (docs/10 append-failure contract)."""
        with self._lock:
            fold = self._fold()
            if fold.corrupt:
                self._downgrade_quality(QUALITY_CORRUPT, "fold_corrupt")
            if fold.orphan_started:
                self._downgrade_quality(QUALITY_INCOMPLETE, "orphan_started")
            self._manifest["lifecycle_status"] = LIFECYCLE_FINALIZED
            self._manifest["finalized_ts"] = time.time()
            self._manifest["finalized_resume_epoch_id"] = fold.last_resume_epoch_id
            self._manifest["data_quality"] = self._quality
            self._manifest["cycles_started"] = fold.cycles_started
            self._manifest["cycles_written"] = fold.cycles_written
            self._manifest["cycles_failed"] = fold.cycles_failed
            self._manifest["cycles_excluded"] = fold.cycles_excluded
            self._manifest["total_trace_bytes"] = fold.total_trace_bytes
            self._manifest["first_trace_id"] = fold.first_trace_id
            self._manifest["last_trace_id"] = fold.last_trace_id
            self._manifest["segment_count"] = len(self._segment_files())
            return self._write_manifest(self._manifest)

    def dataset_complete(self) -> bool:
        """Derived predicate (docs/10): never a stored state."""
        with self._lock:
            manifest = self._manifest
            fold = self._fold()
            if manifest.get("lifecycle_status") != LIFECYCLE_FINALIZED:
                return False
            if manifest.get("data_quality") != QUALITY_CLEAN:
                return False
            if str(manifest.get("finalized_resume_epoch_id") or "") != fold.last_resume_epoch_id:
                return False
            if fold.corrupt or fold.orphan_started:
                return False
            if fold.cycles_written != fold.cycles_complete:
                return False
            if fold.total_trace_bytes > MAX_RUN_TRACE_BYTES:
                return False
            if not fold.identity_holds:
                return False
            return True

    def manifest_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._manifest))

    @property
    def dirty(self) -> bool:
        return self._dirty
