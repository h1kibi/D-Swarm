"""M7-0 sidecar crash-recovery protocol tests (docs/10).

Covers contract items: 26, 36-38, 45, 49-51, 58-61, 74-85, 87-93, 98-102,
104-113, 119-127.
"""

from __future__ import annotations

import json
import time
import types

import pytest

import dswarm.swarm.energy_sidecar as sidecar_mod
from dswarm.swarm import energy
from dswarm.swarm.energy import (
    SCHEMA_VERSION,
    CycleTrace,
    EnergyDecision,
    GraphCycleSnapshot,
    decision_id_for,
)
from dswarm.swarm.energy_sidecar import (
    EnergyTraceSink,
    ResumeGuardError,
)


def _sink(tmp_path, run="r1", enabled=True, sub="run"):
    return EnergyTraceSink(
        tmp_path / sub, run_id=run, challenge_id="t1", enabled=enabled)


def _decision(trace_id, index=0, intent_id="i0"):
    return EnergyDecision(
        decision_id=decision_id_for("r1", trace_id, index, intent_id, "reason"),
        trace_id=trace_id, reason_cycle_id="reason-1", intent_id=intent_id,
        route_hash="route:a", worker_lane="ordinary", priority=0.5,
        normalized_priority=0.5, priority_scale="planner",
        original_index=index, decision_source="reason")


def _snapshot(**kw):
    values = dict(graph_after_seq=0, observations=(), dead_ends=(),
                  complete=True, exclusion_reason="", observed_fact_count=0,
                  captured_fact_count=0, stored_fact_count=0)
    values.update(kw)
    return GraphCycleSnapshot(**values)


def _trace(trace_id, decisions=(), snapshot=None, complete=True, reason=""):
    snapshot = snapshot if snapshot is not None else _snapshot()
    return CycleTrace(
        schema_version=SCHEMA_VERSION, trace_id=trace_id,
        reason_cycle_id="reason-1", decision_ts=1234567.0,
        expected_decision_count=len(decisions), decisions=tuple(decisions),
        snapshot=snapshot, complete=complete, exclusion_reason=reason,
        serialized_bytes=0, serialized_bytes_attempted=None)


def _full_cycle(sink, trace_id="m7-cycle::r1::inst::1"):
    ok1 = sink.start_cycle(trace_id, reason_cycle_id="reason-1",
                           decision_ts=1234567.0)
    ok2 = sink.write_trace(_trace(trace_id, decisions=(_decision(trace_id),)))
    return ok1, ok2


def _segments(tmp_path, sub="run"):
    return sorted((tmp_path / sub / "metrics").glob(
        "energy-cycle-traces.*.jsonl"))


def _lines(tmp_path, sub="run"):
    out = []
    for seg in _segments(tmp_path, sub):
        for raw in seg.read_bytes().split(b"\n"):
            if raw.strip():
                out.append(json.loads(raw.decode("utf-8")))
    return out


# ------------------------------------------------------------------ 26/36/45

def test_26_partial_tail_truncated_on_reopen(tmp_path):
    sink = _sink(tmp_path)
    _full_cycle(sink)
    seg = _segments(tmp_path)[0]
    with seg.open("ab") as handle:
        handle.write(b'{"kind":"cycle_trace","tra')
    sink2 = _sink(tmp_path)  # reopen: truncate tail, then resume guard
    fold = sink2.fold()
    assert fold.corrupt is False
    assert fold.cycles_written == 1


def test_36_and_45_sidecar_dedup_key_is_trace_id(tmp_path):
    sink = _sink(tmp_path)
    _full_cycle(sink, trace_id="t-a")
    # duplicate trace_id -> corrupt (docs/10 classification)
    sink.start_cycle("t-a", reason_cycle_id="reason-2", decision_ts=1.0)
    sink.write_trace(_trace("t-a", decisions=(_decision("t-a", 1),)))
    fold = sink.fold()
    assert fold.corrupt is True
    assert "duplicate_started" in fold.reasons


# ------------------------------------------------------------------ 37/49/80

def test_37_and_49_segments_never_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar_mod, "MAX_SEGMENT_BYTES", 240)
    sink = _sink(tmp_path)
    for i in range(4):
        _full_cycle(sink, trace_id=f"m7-cycle::r1::inst::{i}")
    segments = _segments(tmp_path)
    assert len(segments) >= 2
    before = sorted(p.name for p in segments)
    sink.finalize()
    sink2 = _sink(tmp_path)  # reopen must not delete anything
    sink2.finalize()
    after = sorted(p.name for p in _segments(tmp_path))
    # reopen appends resume_epoch lines (possibly rotating), but never deletes
    assert set(before) <= set(after)
    assert segments[0].name.endswith("000000.jsonl")


def test_80_segment_rotation_and_reopen_continuation(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar_mod, "MAX_SEGMENT_BYTES", 240)
    sink = _sink(tmp_path)
    for i in range(3):
        _full_cycle(sink, trace_id=f"m7-cycle::r1::inst::{i}")
    assert len(_segments(tmp_path)) >= 2
    highest_before = len(_segments(tmp_path))
    sink2 = _sink(tmp_path)
    _full_cycle(sink2, trace_id="m7-cycle::r1::inst::9")
    assert len(_segments(tmp_path)) >= highest_before
    assert sink2.fold().cycles_written == 4


# --------------------------------------------------------------- 38/50/82/101

def test_38_and_50_middle_malformed_line_is_corrupt(tmp_path):
    sink = _sink(tmp_path)
    _full_cycle(sink)
    seg = _segments(tmp_path)[0]
    raw = seg.read_bytes()
    raw = raw.replace(b'"kind":"cycle_trace"', b'{not-json', 1)
    seg.write_bytes(raw)
    fold = sink.fold()
    assert fold.corrupt is True
    assert "malformed_line" in fold.reasons


def test_82_non_last_segment_partial_tail_is_corrupt(tmp_path):
    sink = _sink(tmp_path)
    _full_cycle(sink, trace_id="t1")
    metrics = tmp_path / "run" / "metrics"
    seg0 = metrics / "energy-cycle-traces.000000.jsonl"
    seg1 = metrics / "energy-cycle-traces.000001.jsonl"
    seg0.write_bytes(b'{"kind":"cycle_trace","tra')  # partial, NOT last
    seg1.write_bytes(
        b'{"kind":"cycle_started","trace_id":"t2","schema_version":1,'
        b'"reason_cycle_id":"r","decision_ts":1.0}\n')
    fold = sink.fold()
    assert fold.corrupt is True


def test_101_duplicate_and_orphan_classifications(tmp_path):
    sink = _sink(tmp_path)
    _full_cycle(sink, trace_id="t1")
    sink.start_cycle("t2", reason_cycle_id="r", decision_ts=1.0)  # orphan
    fold = sink.fold()
    assert fold.cycles_started == 2
    assert fold.cycles_written == 1
    assert fold.cycles_failed == 1
    assert fold.orphan_started == ["t2"]
    assert fold.identity_holds is True


def test_101_trace_without_started_is_corrupt(tmp_path):
    metrics = tmp_path / "run" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "energy-cycle-traces.000000.jsonl").write_bytes(
        b'{"kind":"cycle_trace","trace_id":"t9","schema_version":1}\n')
    sink = _sink(tmp_path)
    fold = sink.fold()
    assert fold.corrupt is True
    assert "trace_without_started" in fold.reasons


# ------------------------------------------------------------------ 51/79/84

def test_51_and_79_oversize_stub_written_with_exact_bytes(tmp_path,
                                                          monkeypatch):
    monkeypatch.setattr(sidecar_mod, "MAX_TRACE_BYTES", 600)
    sink = _sink(tmp_path)
    sink.start_cycle("t1", reason_cycle_id="r", decision_ts=1.0)
    decisions = tuple(_decision("t1", index=i, intent_id=f"i{i:02d}")
                      for i in range(6))
    trace = _trace("t1", decisions=decisions)
    assert sink.write_trace(trace) is True
    record = _lines(tmp_path)[-1]
    assert record["complete"] is False
    assert record["exclusion_reason"] == "snapshot_size_limit"
    assert record["serialized_bytes_attempted"] is not None
    raw = _segments(tmp_path)[-1].read_bytes().split(b"\n")
    line = [x for x in raw if x.strip()][-1]
    assert record["serialized_bytes"] == len(line)
    assert record["snapshot"]["observations"] == []
    assert record["decisions"] == [energy.decision_to_dict(d)
                                   for d in decisions]


def test_84_snapshot_unavailable_is_excluded_stub_not_failed(tmp_path):
    sink = _sink(tmp_path)
    sink.start_cycle("t1", reason_cycle_id="r", decision_ts=1.0)
    trace = _trace("t1", complete=False, reason="snapshot_unavailable")
    assert sink.write_trace(trace) is True
    fold = sink.fold()
    assert fold.cycles_written == 1
    assert fold.cycles_complete == 0
    assert fold.cycles_excluded == 1
    assert fold.cycles_failed == 0


# ------------------------------------------------------------------ 58/59/77

def test_58_and_59_manifest_fields_and_complete_predicate(tmp_path):
    sink = _sink(tmp_path)
    _full_cycle(sink)
    assert sink.finalize() is True
    manifest = sink.manifest_snapshot()
    for key in ("schema_version", "run_id", "lifecycle_status", "data_quality",
                "exclusion_reasons", "created_ts", "finalized_ts",
                "finalized_resume_epoch_id", "cycles_started", "cycles_written",
                "cycles_failed", "cycles_excluded", "segment_count",
                "total_trace_bytes", "max_trace_bytes", "max_run_trace_bytes",
                "max_segment_bytes", "first_trace_id", "last_trace_id"):
        assert key in manifest
    assert sink.dataset_complete() is True


def test_77_dataset_complete_false_without_finalize(tmp_path):
    sink = _sink(tmp_path)
    _full_cycle(sink)
    assert sink.dataset_complete() is False  # lifecycle still in_progress


def test_78_three_layer_complete_do_not_imply_each_other(tmp_path,
                                                         monkeypatch):
    monkeypatch.setattr(sidecar_mod, "MAX_TRACE_BYTES", 600)
    sink = _sink(tmp_path)
    sink.start_cycle("t1", reason_cycle_id="r", decision_ts=1.0)
    snapshot = _snapshot(complete=True)  # snapshot.complete True
    decisions = tuple(_decision("t1", index=i, intent_id=f"i{i:02d}")
                      for i in range(6))
    trace = _trace("t1", decisions=decisions, snapshot=snapshot)
    sink.write_trace(trace)
    # trace.complete became False via oversize stub; dataset not finalized
    assert sink.dataset_complete() is False
    fold = sink.fold()
    assert fold.cycles_complete == 0
    sink.finalize()
    assert sink.dataset_complete() is False  # excluded trace -> not all complete


# ------------------------------------------------------------------ 60/61/99

def test_60_quality_never_upgrades_on_resume(tmp_path):
    sink = _sink(tmp_path)
    sink.start_cycle("t1", reason_cycle_id="r", decision_ts=1.0)  # orphan
    assert sink.finalize() is True
    assert sink.dataset_complete() is False  # incomplete (orphan)
    sink2 = _sink(tmp_path)  # enabled resume of degraded dataset
    manifest = sink2.manifest_snapshot()
    assert manifest["data_quality"] == "incomplete"
    assert "resume_without_energy_trace" not in manifest["exclusion_reasons"]
    assert sink2.finalize() is True
    assert sink2.dataset_complete() is False


def test_61_and_99_manifest_rebuild_from_segments_never_clean(tmp_path,
                                                              monkeypatch):
    sink = _sink(tmp_path)
    _full_cycle(sink, trace_id="t1")
    # simulate manifest update failure after the trace landed: patch writes
    monkeypatch.setattr(sink, "_write_manifest", lambda m: False)
    assert sink.finalize() is False
    # the manifest on disk is stale; delete it to simulate loss
    (tmp_path / "run" / "metrics" / sidecar_mod.MANIFEST_NAME).unlink()
    sink2 = _sink(tmp_path)
    manifest = sink2.manifest_snapshot()
    assert manifest["data_quality"] != "clean"
    assert manifest["cycles_written"] == 1
    assert sink2.fold().last_trace_id == "t1"


def test_99_corrupt_manifest_rebuilds_not_clean(tmp_path):
    sink = _sink(tmp_path)
    _full_cycle(sink, trace_id="t1")
    manifest_path = tmp_path / "run" / "metrics" / sidecar_mod.MANIFEST_NAME
    manifest_path.write_text("{corrupt json", encoding="utf-8")
    sink2 = _sink(tmp_path)
    assert sink2.manifest_snapshot()["data_quality"] != "clean"
    assert sink2.fold().cycles_written == 1


# ------------------------------------------------------------------ 74/75/83

def test_74_two_phase_order_in_segment(tmp_path):
    sink = _sink(tmp_path)
    _full_cycle(sink)
    records = _lines(tmp_path)
    assert [r["kind"] for r in records] == ["cycle_started", "cycle_trace"]


def test_75_and_83_orphan_started_counts_as_failed(tmp_path):
    sink = _sink(tmp_path)
    _full_cycle(sink, trace_id="t1")
    sink.start_cycle("t2", reason_cycle_id="r", decision_ts=1.0)
    assert sink.finalize() is True
    manifest = sink.manifest_snapshot()
    assert manifest["cycles_started"] == 2
    assert manifest["cycles_written"] == 1
    assert manifest["cycles_failed"] == 1
    assert manifest["data_quality"] == "incomplete"
    assert sink.dataset_complete() is False


# ------------------------------------------------------------------ 87-93

def test_87_unclean_reopen_downgrades_sticky(tmp_path):
    sink = _sink(tmp_path)  # fresh: in_progress + clean
    assert sink.manifest_snapshot()["data_quality"] == "clean"
    sink2 = _sink(tmp_path)  # "crash" reopen
    manifest = sink2.manifest_snapshot()
    assert manifest["data_quality"] == "incomplete"
    assert "unclean_reopen" in manifest["exclusion_reasons"]
    assert sink2.dataset_complete() is False


def test_88_and_113_finalized_clean_resume_not_auto_downgraded(tmp_path):
    sink = _sink(tmp_path)
    _full_cycle(sink)
    assert sink.finalize() is True
    assert sink.dataset_complete() is True
    sink2 = _sink(tmp_path)  # enabled resume
    manifest = sink2.manifest_snapshot()
    assert manifest["lifecycle_status"] == "in_progress"
    assert manifest["data_quality"] == "clean"
    assert "unclean_reopen" not in manifest["exclusion_reasons"]
    # resume_epoch was recorded (the witness)
    assert sink2.fold().last_resume_epoch_id != ""


def test_89_unclean_reopen_then_finalize_still_not_complete(tmp_path):
    _sink(tmp_path)
    sink2 = _sink(tmp_path)  # unclean reopen downgrade
    assert sink2.finalize() is True
    assert sink2.dataset_complete() is False


def test_90_cycle_started_append_failure_finalize_not_clean(tmp_path,
                                                            monkeypatch):
    sink = _sink(tmp_path)
    original = EnergyTraceSink._append_line

    def flaky(self, line, *, failure_reason=None):
        if failure_reason == "cycle_started_append_failed":
            self._on_append_failure(failure_reason)
            return False
        return original(self, line, failure_reason=failure_reason)

    monkeypatch.setattr(sink, "_append_line", types.MethodType(flaky, sink))
    assert sink.start_cycle("t1", reason_cycle_id="r", decision_ts=1.0) is False
    assert sink.dirty is True
    assert sink.finalize() is True
    manifest = sink.manifest_snapshot()
    assert manifest["lifecycle_status"] == "finalized"
    assert manifest["data_quality"] != "clean"
    assert "cycle_started_append_failed" in manifest["exclusion_reasons"]
    assert sink.dataset_complete() is False


def test_91_append_failure_then_crash_reopen_unclean(tmp_path, monkeypatch):
    sink = _sink(tmp_path)
    original = EnergyTraceSink._append_line

    def flaky(self, line, *, failure_reason=None):
        if failure_reason == "cycle_started_append_failed":
            self._on_append_failure(failure_reason)
            return False
        return original(self, line, failure_reason=failure_reason)

    monkeypatch.setattr(sink, "_append_line", types.MethodType(flaky, sink))
    sink.start_cycle("t1", reason_cycle_id="r", decision_ts=1.0)
    sink2 = _sink(tmp_path)  # crash -> reopen
    assert sink2.manifest_snapshot()["data_quality"] == "incomplete"
    assert sink2.finalize() is True
    assert sink2.dataset_complete() is False


def test_92_partial_tail_then_failure_reopen(tmp_path, monkeypatch):
    sink = _sink(tmp_path)

    def partial(self, line, *, failure_reason=None):
        if failure_reason == "cycle_started_append_failed":
            seg = self._segment_path(self._current_segment)
            with seg.open("ab") as handle:
                handle.write(b'{"kind":"cycle_started","tra')
            self._on_append_failure(failure_reason)
            return False
        return EnergyTraceSink._append_line(
            self, line, failure_reason=failure_reason)

    monkeypatch.setattr(sink, "_append_line", types.MethodType(partial, sink))
    sink.start_cycle("t1", reason_cycle_id="r", decision_ts=1.0)
    sink2 = _sink(tmp_path)  # truncate partial + unclean reopen
    fold = sink2.fold()
    assert fold.corrupt is False
    assert sink2.manifest_snapshot()["data_quality"] == "incomplete"


def test_93_manifest_downgrade_write_failure(tmp_path, monkeypatch):
    sink = _sink(tmp_path)
    monkeypatch.setattr(sink, "_write_manifest", lambda m: False)
    original = EnergyTraceSink._append_line

    def flaky(self, line, *, failure_reason=None):
        if failure_reason == "cycle_started_append_failed":
            self._on_append_failure(failure_reason)
            return False
        return original(self, line, failure_reason=failure_reason)

    monkeypatch.setattr(sink, "_append_line", types.MethodType(flaky, sink))
    sink.start_cycle("t1", reason_cycle_id="r", decision_ts=1.0)
    assert sink.dirty is True
    sink.finalize()  # write still fails; on-disk manifest stays in_progress
    on_disk = json.loads((tmp_path / "run" / "metrics" /
                          sidecar_mod.MANIFEST_NAME).read_text("utf-8"))
    assert on_disk["lifecycle_status"] == "in_progress"
    sink2 = _sink(tmp_path)
    assert sink2.manifest_snapshot()["data_quality"] == "incomplete"


# ------------------------------------------------------------------ 98/102

def test_98_manifest_durable_write_and_failure(tmp_path, monkeypatch):
    sink = _sink(tmp_path)
    assert sink.manifest_snapshot()["lifecycle_status"] == "in_progress"
    assert not list((tmp_path / "run" / "metrics").glob("*.tmp"))
    # failure path: replace raises -> dirty, previous manifest retained
    good = sink.manifest_snapshot()

    def boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(sidecar_mod.os, "replace", boom)
    assert sink._write_manifest(good) is False
    assert sink.dirty is True
    assert not list((tmp_path / "run" / "metrics").glob("*.tmp"))


def test_102_started_counts_for_rotation_trace_bytes_exclude_it(
        tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar_mod, "MAX_SEGMENT_BYTES", 130)
    sink = _sink(tmp_path)
    _full_cycle(sink, trace_id="t1")
    segments = _segments(tmp_path)
    assert len(segments) >= 2  # started rotated into its own segment
    fold = sink.fold()
    trace_lines = [l for l in _lines(tmp_path) if l.get("kind") == "cycle_trace"]
    expected = sum(
        len(json.dumps(l, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8"))
        for l in trace_lines)
    assert fold.total_trace_bytes == expected


# ------------------------------------------------------------------ 104/110-112

def test_104_disabled_resume_from_finalized_clean(tmp_path):
    sink = _sink(tmp_path)
    _full_cycle(sink)
    assert sink.finalize() is True
    assert sink.dataset_complete() is True
    sink2 = _sink(tmp_path, enabled=False)  # guard runs regardless of the flag
    manifest = sink2.manifest_snapshot()
    assert manifest["lifecycle_status"] == "in_progress"
    assert manifest["data_quality"] == "incomplete"
    assert "resume_without_energy_trace" in manifest["exclusion_reasons"]
    assert sink2.dataset_complete() is False


def test_103_disabled_fresh_run_has_zero_side_effects(tmp_path):
    _sink(tmp_path, enabled=False)
    assert not (tmp_path / "run" / "metrics").exists()


def test_110_resume_guard_fails_fast_when_witness_unwritable(tmp_path,
                                                             monkeypatch):
    _sink(tmp_path)
    original = EnergyTraceSink._append_line

    def failing(self, line, *, failure_reason=None):
        if failure_reason is None:  # resume_epoch append
            return False
        return original(self, line, failure_reason=failure_reason)

    monkeypatch.setattr(sidecar_mod.EnergyTraceSink, "_append_line", failing)
    with pytest.raises(ResumeGuardError):
        _sink(tmp_path)


def test_111_flip_failure_still_resumes_epoch_is_witness(tmp_path,
                                                         monkeypatch):
    sink = _sink(tmp_path)
    _full_cycle(sink)
    assert sink.finalize() is True
    monkeypatch.setattr(sidecar_mod.EnergyTraceSink, "_write_manifest",
                        lambda self, m: False)
    sink2 = _sink(tmp_path)  # must NOT raise
    epoch_id = sink2.fold().last_resume_epoch_id
    assert epoch_id != ""
    # ack on disk is still the old "" -> dataset cannot be complete
    assert sink2.dataset_complete() is False


def test_112_crash_before_guard_leaves_old_dataset_truthful(tmp_path):
    sink = _sink(tmp_path)
    _full_cycle(sink)
    assert sink.finalize() is True
    assert sink.dataset_complete() is True
    # "crash before guard": nothing else ran — no resume_epoch on disk, the
    # old dataset is still the truth (no uncaptured dispatch happened).
    assert not any(r.get("kind") == "resume_epoch"
                   for r in _lines(tmp_path))
    assert sink.fold().cycles_written == 1


# ------------------------------------------------------------------ 106-109

def _trace_append_flaky(monkeypatch, sink):
    original = EnergyTraceSink._append_line

    def flaky(self, line, *, failure_reason=None):
        if failure_reason == "cycle_trace_append_failed":
            self._on_append_failure(failure_reason)
            return False
        return original(self, line, failure_reason=failure_reason)

    monkeypatch.setattr(sink, "_append_line", types.MethodType(flaky, sink))


def test_106_cycle_trace_append_failure_does_not_raise(tmp_path, monkeypatch):
    sink = _sink(tmp_path)
    _trace_append_flaky(monkeypatch, sink)
    assert sink.start_cycle("t1", reason_cycle_id="r", decision_ts=1.0) is True
    result = sink.write_trace(_trace("t1", decisions=(_decision("t1"),)))
    assert result is False  # no exception, dispatch would continue
    assert sink.dirty is True
    assert "cycle_trace_append_failed" in sink.manifest_snapshot()[
        "exclusion_reasons"]


def test_108_trace_failure_finalize_not_clean(tmp_path, monkeypatch):
    sink = _sink(tmp_path)
    _trace_append_flaky(monkeypatch, sink)
    sink.start_cycle("t1", reason_cycle_id="r", decision_ts=1.0)
    sink.write_trace(_trace("t1", decisions=(_decision("t1"),)))
    assert sink.finalize() is True
    assert sink.manifest_snapshot()["data_quality"] != "clean"
    assert sink.dataset_complete() is False


def test_109_trace_failure_then_crash_orphan_incomplete(tmp_path, monkeypatch):
    sink = _sink(tmp_path)
    _trace_append_flaky(monkeypatch, sink)
    sink.start_cycle("t1", reason_cycle_id="r", decision_ts=1.0)
    sink.write_trace(_trace("t1", decisions=(_decision("t1"),)))
    sink2 = _sink(tmp_path)  # crash -> reopen
    fold = sink2.fold()
    assert fold.orphan_started == ["t1"]
    assert fold.cycles_failed == 1
    assert sink2.finalize() is True
    assert sink2.dataset_complete() is False


def test_107_partial_tail_trace_failure_then_reopen(tmp_path, monkeypatch):
    sink = _sink(tmp_path)

    def partial(self, line, *, failure_reason=None):
        if failure_reason == "cycle_trace_append_failed":
            seg = self._segment_path(self._current_segment)
            with seg.open("ab") as handle:
                handle.write(b'{"kind":"cycle_trace","tra')
            self._on_append_failure(failure_reason)
            return False
        return EnergyTraceSink._append_line(
            self, line, failure_reason=failure_reason)

    monkeypatch.setattr(sink, "_append_line", types.MethodType(partial, sink))
    sink.start_cycle("t1", reason_cycle_id="r", decision_ts=1.0)
    sink.write_trace(_trace("t1", decisions=(_decision("t1"),)))
    sink2 = _sink(tmp_path)
    assert sink2.fold().corrupt is False  # tail truncated
    assert sink2.manifest_snapshot()["data_quality"] == "incomplete"


# ------------------------------------------------------------------ 119-127

def test_119_clock_regression_cannot_forge_complete(tmp_path, monkeypatch):
    sink = _sink(tmp_path)
    _full_cycle(sink)
    assert sink.finalize() is True
    # wall clock jumps backwards: resume_ts < old finalized_ts. The predicate
    # must ignore wall clocks entirely (epoch-id acknowledgment only).
    monkeypatch.setattr(sidecar_mod.time, "time", lambda: 1000.0)
    sink2 = _sink(tmp_path)  # resume at t=1000 (< finalized_ts of sink1)
    assert sink2.finalize() is True
    # ack now matches the last epoch -> complete holds DESPITE ts regression,
    # proving the predicate never compares timestamps.
    assert sink2.dataset_complete() is True


def test_120_epoch_duplicate_identical_idempotent_mismatch_corrupt(tmp_path):
    sink = _sink(tmp_path)
    _full_cycle(sink)
    sink.finalize()
    metrics = tmp_path / "run" / "metrics"
    seg = _segments(tmp_path)[0]
    epoch_line = json.dumps({
        "kind": "resume_epoch", "resume_epoch_id": "e-dup",
        "resume_ts": 1.0, "prior_lifecycle": "finalized",
        "prior_data_quality": "clean", "schema_version": 1},
        sort_keys=True, separators=(",", ":"))
    with seg.open("ab") as handle:
        handle.write(epoch_line.encode() + b"\n")  # identical duplicate
    fold = sink.fold()
    assert fold.corrupt is False
    assert fold.last_resume_epoch_id == "e-dup"
    with seg.open("ab") as handle:
        handle.write(epoch_line.replace('"clean"', '"incomplete"').encode()
                     + b"\n")
    assert sink.fold().corrupt is True


def test_121_malformed_resume_epoch_is_corrupt(tmp_path):
    metrics = tmp_path / "run" / "metrics"
    metrics.mkdir(parents=True)
    # complete line (newline-terminated) with invalid JSON -> middle malformed
    (metrics / "energy-cycle-traces.000000.jsonl").write_bytes(
        b'{"kind":"resume_epoch","resume_epoch_id":"e1"\n')
    sink = _sink(tmp_path)
    assert sink.fold().corrupt is True
    assert sink.manifest_snapshot()["data_quality"] != "clean"


def test_123_last_epoch_is_physical_order_not_timestamp(tmp_path, monkeypatch):
    sink = _sink(tmp_path)
    _full_cycle(sink)
    sink.finalize()
    monkeypatch.setattr(sidecar_mod.time, "time", lambda: 5000.0)
    _sink(tmp_path)  # resume E1 with LATER ts
    monkeypatch.setattr(sidecar_mod.time, "time", lambda: 1000.0)
    sink3 = _sink(tmp_path)  # resume E2 with EARLIER ts
    epochs = [r for r in _lines(tmp_path)
              if r.get("kind") == "resume_epoch"]
    assert len(epochs) == 2
    assert epochs[1]["resume_ts"] < epochs[0]["resume_ts"]
    assert sink3.fold().last_resume_epoch_id == epochs[1]["resume_epoch_id"]


def test_126_multiple_resumes_require_ack_of_latest(tmp_path):
    sink = _sink(tmp_path)
    _full_cycle(sink, trace_id="t1")
    assert sink.finalize() is True
    sink2 = _sink(tmp_path)  # resume E1
    assert sink2.dataset_complete() is False  # in_progress
    sink2.finalize()  # ack E1
    assert sink2.dataset_complete() is True
    sink3 = _sink(tmp_path)  # resume E2
    assert sink3.dataset_complete() is False  # E2 not yet acked
    sink3.finalize()  # ack E2
    assert sink3.dataset_complete() is True


def test_127_enabled_resume_of_degraded_dataset_keeps_old_reasons(tmp_path):
    sink = _sink(tmp_path)
    sink.start_cycle("t1", reason_cycle_id="r", decision_ts=1.0)  # orphan
    sink.finalize()
    assert sink.manifest_snapshot()["data_quality"] == "incomplete"
    sink2 = _sink(tmp_path)  # enabled resume
    manifest = sink2.manifest_snapshot()
    assert manifest["data_quality"] == "incomplete"
    assert "resume_without_energy_trace" not in manifest["exclusion_reasons"]
    assert "orphan_started" in manifest["exclusion_reasons"]  # history kept
    assert sink2.dataset_complete() is False


def test_122_resume_epoch_not_counted_in_trace_bytes(tmp_path):
    sink = _sink(tmp_path)
    _full_cycle(sink)
    sink.finalize()
    before = sink.fold().total_trace_bytes
    sink2 = _sink(tmp_path)  # adds resume_epoch line
    assert sink2.fold().total_trace_bytes == before


def test_124_rebuild_derives_last_epoch_and_never_completes(tmp_path):
    sink = _sink(tmp_path)
    _full_cycle(sink)
    sink.finalize()
    sink2 = _sink(tmp_path)  # resume -> epoch E1; ack in finalize
    assert sink2.finalize() is True
    (tmp_path / "run" / "metrics" / sidecar_mod.MANIFEST_NAME).unlink()
    sink3 = _sink(tmp_path)  # rebuild + own guard appends E2
    epochs = [r["resume_epoch_id"] for r in _lines(tmp_path)
              if r.get("kind") == "resume_epoch"]
    assert len(epochs) == 2  # E1 persisted, E2 from the reopen guard
    # last is derived from physical order (E2), rebuilt counts intact
    assert sink3.fold().last_resume_epoch_id == epochs[-1]
    assert sink3.fold().cycles_written == 1
    assert sink3.manifest_snapshot()["data_quality"] != "clean"
    assert sink3.dataset_complete() is False


# ------------------------------------------------------------------ 85 static

def test_85_energy_sidecar_is_bus_free():
    import inspect
    src = inspect.getsource(sidecar_mod)
    assert "EventType" not in src
    assert "_emit" not in src
    assert "blackboard_delta" not in src
