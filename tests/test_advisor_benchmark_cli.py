from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4

import pytest


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "advisor_benchmark.py"


@pytest.fixture
def suite_module(request: pytest.FixtureRequest):
    created: list[Path] = []

    def create(*, mode: str = "silent") -> tuple[str, Path]:
        package_name = f"pytest_m8_{uuid4().hex}"
        package = REPO / "local_benchmarks" / package_name
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        import_line = (
            'print("IMPORT SECRET RAWFLAG{import}")\n' if mode == "import_output" else ""
        )
        factory_line = (
            '    print("FACTORY SECRET RAWFLAG{factory}")\n'
            if mode == "factory_output" else ""
        )
        planner_line = (
            '            print("PLANNER SECRET RAWFLAG{planner}")\n'
            if mode == "benchmark_output" else ""
        )
        source = f'''from pathlib import Path
{import_line}from dswarm.solver.reason import Intent, ReasonResult
from dswarm.swarm.advisor_benchmark import AdvisorBenchmarkCase, AdvisorBenchmarkSuite
from dswarm.swarm.advisor_experiment import AdvisorReferenceObjective, make_advisor_fixture
from dswarm.swarm.advisor_runner import AdvisorPlannerResult

ROOT = Path(__file__).resolve().parents[2]
ARTIFACT = ROOT / "eval_runs" / "m8-advisor" / "{package_name}"


def build_suite():
{factory_line}    fixture = make_advisor_fixture(
        benchmark_run_id="run-cli", challenge_id="challenge-cli",
        challenge_mode="ctf", expected_flags=3,
        captured_flags_before_source=0, source_event_seq=42,
        source_event_ts=1.0, source_intent_id="intent-source",
        source_route_hash="route-source", next_cycle_id="reason-2",
        graph_summary="OPAQUE RAWFLAG{{summary}}", fact_index="[10] fact",
        available_fact_seqs=(10,), max_intents=4, goal="find remaining",
        reference_objectives=(AdvisorReferenceObjective(
            objective_id="HIDDEN-CLI-OBJECTIVE", route_hash="route-advisor",
        ),),
    )

    def factory(arm):
        async def planner(_request):
{planner_line}            route = "route-baseline" if arm == "baseline" else "route-advisor"
            return AdvisorPlannerResult(result=ReasonResult(
                goal_met=False,
                intents=[Intent(
                    intent_id=f"intent-{{route}}", goal=f"inspect {{route}}",
                    route_hash=route, worker_class="code", direction="web",
                    priority=0.5, from_facts=[10],
                )],
                audit_notes=[], pinned_facts=[10], dispatches=[],
            ))
        return planner

    return AdvisorBenchmarkSuite(
        artifact_root=ARTIFACT,
        cases=(AdvisorBenchmarkCase(
            case_root=ARTIFACT / "case-1", fixture=fixture,
            planner_factory=factory, timeout_s=1.0, cleanup_timeout_s=0.2,
        ),),
    )
'''
        (package / "suite.py").write_text(source, encoding="utf-8")
        created.append(package)
        request.addfinalizer(
            lambda p=package, a=(REPO / "eval_runs" / "m8-advisor" / package_name): (
                shutil.rmtree(p, ignore_errors=True),
                shutil.rmtree(a, ignore_errors=True),
            )
        )
        return f"local_benchmarks.{package_name}.suite:build_suite", package

    return create


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )


def test_silent_success_emits_exactly_one_safe_json_line(suite_module):
    spec, _ = suite_module()

    completed = _run(spec)

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert completed.stdout.endswith("\n")
    assert completed.stdout.count("\n") == 1
    payload = json.loads(completed.stdout)
    assert payload["kind"] == "m8_advisor_benchmark_result"
    assert payload["reported_case_count"] == 1
    for forbidden in (
        "RAWFLAG{summary}", "HIDDEN-CLI-OBJECTIVE", "OPAQUE", "route-advisor",
    ):
        assert forbidden not in completed.stdout


@pytest.mark.parametrize(
    ("mode", "code"),
    (
        ("import_output", "suite_factory_wrote_output"),
        ("factory_output", "suite_factory_wrote_output"),
        ("benchmark_output", "benchmark_wrote_output"),
    ),
)
def test_python_level_output_is_discarded_and_only_fixed_code_is_emitted(
    suite_module, mode, code,
):
    spec, _ = suite_module(mode=mode)

    completed = _run(spec)

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert completed.stderr == code + "\n"
    assert "SECRET" not in completed.stderr
    assert "RAWFLAG" not in completed.stderr


@pytest.mark.parametrize(
    ("spec", "code"),
    (
        ("not-a-module-factory", "invalid_suite_spec"),
        ("missing_m8_module:build_suite", "suite_import_failed"),
        ("json:missing_factory", "suite_factory_missing"),
        ("json:loads", "suite_factory_failed"),
    ),
)
def test_invalid_suite_factory_uses_fixed_errors(spec, code):
    completed = _run(spec)

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert completed.stderr == code + "\n"


def test_output_file_is_byte_identical_and_stdout_stays_empty(suite_module):
    spec, package = suite_module()
    expected = _run(spec)
    assert expected.returncode == 0
    target = REPO / "eval_runs" / "m8-advisor" / package.name / "report.json"

    completed = _run(spec, "--output", str(target))

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    assert target.read_bytes() == expected.stdout.encode("utf-8")


@pytest.mark.parametrize(
    "target",
    (
        REPO / "report.json",
        REPO / "docs" / "report.json",
        REPO / "eval_runs",
        REPO / "sessions",
        REPO / "eval_runs" / ".." / "docs" / "report.json",
    ),
)
def test_output_path_outside_allowed_descendants_is_rejected(
    suite_module, target,
):
    spec, _ = suite_module()
    before = target.read_bytes() if target.is_file() else None

    completed = _run(spec, "--output", str(target))

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert completed.stderr == "output_path_not_allowed\n"
    if before is not None:
        assert target.read_bytes() == before


def test_atomic_replace_failure_preserves_old_target_and_uses_fixed_code(
    suite_module, monkeypatch, capsys,
):
    spec, package = suite_module()
    module_path = SCRIPT
    loaded_spec = importlib.util.spec_from_file_location("m8_cli_under_test", module_path)
    assert loaded_spec is not None and loaded_spec.loader is not None
    cli = importlib.util.module_from_spec(loaded_spec)
    loaded_spec.loader.exec_module(cli)
    target = REPO / "eval_runs" / "m8-advisor" / package.name / "report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("old-report\n", encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("SECRET replace failure")

    monkeypatch.setattr(cli.os, "replace", fail_replace)
    code = cli.main([spec, "--output", str(target)])
    captured = capsys.readouterr()

    assert code != 0
    assert captured.out == ""
    assert captured.err == "output_write_failed\n"
    assert target.read_text(encoding="utf-8") == "old-report\n"
    assert not tuple(target.parent.glob(f".{target.name}.*.tmp"))


def test_gitignore_rules_are_effective_not_just_present():
    for relative in (
        "local_benchmarks/example.py",
        "eval_runs/m8-advisor/example.json",
    ):
        completed = subprocess.run(
            ["git", "check-ignore", "-q", relative], cwd=REPO, check=False,
        )
        assert completed.returncode == 0, relative
