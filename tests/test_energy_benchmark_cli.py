"""CLI coverage for the independent M7 offline benchmark runner."""

from __future__ import annotations

import io
import json
import sys
from types import ModuleType

from dswarm.swarm.energy import EnergyConfig
from dswarm.swarm.energy_benchmark import EnergyBenchmarkSuite
from scripts.energy_benchmark import main


CFG = EnergyConfig({
    "verified_witness": 1.0,
    "verified": 0.8,
    "candidate": 0.5,
})


def _install_suite(monkeypatch, name: str = "local_energy_suite"):
    module = ModuleType(name)
    calls = []

    def build_suite():
        calls.append(True)
        return EnergyBenchmarkSuite(cases=(), config=CFG, top_k=2)

    module.build_suite = build_suite
    monkeypatch.setitem(sys.modules, name, module)
    return calls


def test_cli_writes_valid_json_to_stdout(monkeypatch):
    calls = _install_suite(monkeypatch)
    stdout = io.StringIO()

    exit_code = main(["local_energy_suite:build_suite"], stdout=stdout)

    assert exit_code == 0
    assert calls == [True]
    payload = json.loads(stdout.getvalue())
    assert payload["kind"] == "m7_offline_scheduling_reorder_estimate"
    assert payload["cases"] == []
    assert payload["report"]["qualified_runs"] == 0


def test_cli_writes_utf8_output_file(monkeypatch, tmp_path):
    _install_suite(monkeypatch)
    output = tmp_path / "reports" / "energy.json"
    stdout = io.StringIO()

    exit_code = main([
        "local_energy_suite:build_suite",
        "--output", str(output),
    ], stdout=stdout)

    assert exit_code == 0
    assert stdout.getvalue() == ""
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["kind"] == "m7_offline_scheduling_reorder_estimate"
