from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from dswarm.models.solve_graph import Challenge
from dswarm.solver.cli_solver import CliSolver


class _Graph:
    def __init__(self, *, register_error=None):
        self.saved = []
        self.repros = []
        self.register_error = register_error


    def save_poc(self, **kwargs):
        self.saved.append(kwargs)
        return 1

    def register_poc_reproduction(self, **kwargs):
        if self.register_error is not None:
            raise self.register_error
        self.repros.append(kwargs)
        return {
            "poc_id": kwargs["poc_id"],
            "reproduction_id": "poc-repro::test",
            "indicator": kwargs["indicator"],
        }


def _solver(tmp_path: Path, *, mode: str, graph: _Graph):
    challenge = Challenge(id="marker", name="marker", category="web", mode=mode)
    solver = CliSolver(None, challenge, shared_graph=graph, workdir=str(tmp_path), kb=False)
    solver._current_workdir = tmp_path.resolve()
    return solver


def _capture_bb(solver):
    events = []

    async def emit(kind, **fields):
        events.append((kind, fields))

    solver._emit_bb = emit
    return events


def test_ctf_mode_ignores_poc_repro_marker(tmp_path):
    graph = _Graph()
    solver = _solver(tmp_path, mode="ctf", graph=graph)

    asyncio.run(solver._stream_markers("POC_REPRO=poc.py|vulnerable"))

    assert graph.repros == []
    assert solver._pending_poc_repros == {}


def test_pentest_repro_before_save_is_registered_after_matching_save(
    tmp_path, monkeypatch
):
    graph = _Graph()
    solver = _solver(tmp_path, mode="pentest", graph=graph)
    poc = tmp_path / "poc.py"
    poc.write_text("print('vulnerable')", encoding="utf-8")

    monkeypatch.setattr(
        "dswarm.solver.cli_solver.materialize_shared_artifact",
        lambda *args, **kwargs: {"sha256": "artifact-1", "path": str(poc)},
    )

    asyncio.run(solver._stream_markers("POC_REPRO=poc.py|vulnerable"))
    assert solver._pending_poc_repros == {str(poc.resolve()): "vulnerable"}
    assert graph.repros == []

    asyncio.run(
        solver._stream_markers(
            "POC_SAVE=poc.py|python3 poc.py|available|reproducible"
        )
    )

    assert len(graph.saved) == 1
    assert graph.repros == [{"actor": solver.solver_id, "poc_id": "poc-artifact-1", "indicator": "vulnerable"}]
    assert solver._pending_poc_repros == {}


def test_pentest_repro_rejects_path_escape_without_registration(tmp_path):
    graph = _Graph()
    solver = _solver(tmp_path, mode="pentest", graph=graph)

    asyncio.run(solver._stream_markers("POC_REPRO=../poc.py|vulnerable"))

    assert graph.repros == []
    assert solver._pending_poc_repros == {}



def test_ctf_mode_does_not_even_dispatch_repro_parser(tmp_path, monkeypatch):
    graph = _Graph()
    solver = _solver(tmp_path, mode="ctf", graph=graph)

    def fail_parser(_text):
        raise AssertionError("CTF must not dispatch POC_REPRO parsing")

    monkeypatch.setattr(solver, "_extract_poc_repros", fail_parser)
    asyncio.run(solver._stream_markers("POC_REPRO=poc.py|vulnerable"))


def test_registration_delta_contains_digest_not_raw_indicator(tmp_path):
    graph = _Graph()
    solver = _solver(tmp_path, mode="pentest", graph=graph)
    events = _capture_bb(solver)
    path = tmp_path / "poc.py"
    path.write_text("print('vulnerable')", encoding="utf-8")
    solver._poc_paths[str(path.resolve())] = "poc-1"

    asyncio.run(solver._stream_markers("POC_REPRO=poc.py|vulnerable"))

    assert len(graph.repros) == 1
    kind, payload = events[-1]
    assert kind == "poc_reproduction_registered"
    assert payload["indicator_digest"] == hashlib.sha256(
        b"vulnerable"
    ).hexdigest()
    assert payload["indicator_length"] == len("vulnerable")
    assert "indicator" not in payload


@pytest.mark.parametrize(
    "indicator",
    [
        "",
        "\nmarker",
        "x" * 513,
        "flag{not-a-real-secret}",
        "[REDACTED]",
    ],
)
def test_invalid_indicator_is_rejected_without_pending_state(tmp_path, indicator):
    graph = _Graph()
    solver = _solver(tmp_path, mode="pentest", graph=graph)
    events = _capture_bb(solver)

    asyncio.run(solver._stream_markers(f"POC_REPRO=poc.py|{indicator}"))

    assert graph.repros == []
    assert solver._pending_poc_repros == {}
    assert events[-1][0] == "poc_reproduction_rejected"
    assert "indicator" not in events[-1][1]


def test_duplicate_identical_marker_is_idempotent(tmp_path):
    graph = _Graph()
    solver = _solver(tmp_path, mode="pentest", graph=graph)
    path = tmp_path / "poc.py"
    path.write_text("print('vulnerable')", encoding="utf-8")
    solver._poc_paths[str(path.resolve())] = "poc-1"

    asyncio.run(solver._stream_markers("POC_REPRO=poc.py|vulnerable"))
    asyncio.run(solver._stream_markers("POC_REPRO=poc.py|vulnerable"))

    assert len(graph.repros) == 1


def test_conflicting_indicator_is_rejected_and_original_is_preserved(tmp_path):
    graph = _Graph(register_error=ValueError("conflicting reproduction registration"))
    solver = _solver(tmp_path, mode="pentest", graph=graph)
    events = _capture_bb(solver)
    path = tmp_path / "poc.py"
    path.write_text("print('vulnerable')", encoding="utf-8")
    solver._poc_paths[str(path.resolve())] = "poc-1"

    asyncio.run(solver._stream_markers("POC_REPRO=poc.py|different"))

    assert graph.repros == []
    rejected = [fields for kind, fields in events
                if kind == "poc_reproduction_rejected"]
    assert rejected
    assert rejected[-1]["poc_id"] == "poc-1"
    assert "different" not in repr(rejected[-1])


def test_unresolved_pending_repros_are_discarded_at_worker_cleanup(tmp_path):
    graph = _Graph()
    solver = _solver(tmp_path, mode="pentest", graph=graph)
    asyncio.run(solver._stream_markers("POC_REPRO=poc.py|vulnerable"))
    assert solver._pending_poc_repros

    solver._discard_pending_poc_repros()

    assert solver._pending_poc_repros == {}



def test_conflicting_pending_indicator_is_rejected_without_replacement(tmp_path):
    graph = _Graph()
    solver = _solver(tmp_path, mode="pentest", graph=graph)
    events = _capture_bb(solver)

    asyncio.run(solver._stream_markers("POC_REPRO=poc.py|first"))
    asyncio.run(solver._stream_markers("POC_REPRO=poc.py|second"))

    assert solver._pending_poc_repros == {str((tmp_path / "poc.py").resolve()): "first"}
    assert events[-1] == (
        "poc_reproduction_rejected",
        {"status": "rejected", "note": "conflicting pending reproduction indicator"},
    )
