from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path

import pytest

from dswarm.core.events import EventType
from dswarm.core.usage_journal import UsageRecord
from dswarm.solver.container_pool import RuntimeFailure, RuntimePoolView
from dswarm.swarm.runtime import _RUNTIME_OPERATION_KINDS, runtime_operation_for_spawn
from dswarm.solver.runtime_diagnostics import RuntimeDiagnosticsStore
from apps.web.routes.runtime_pools import _project_view


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOTS = (ROOT / "dswarm", ROOT / "apps")
RUNTIME_MODULES = (
    ROOT / "dswarm/solver/container_pool.py",
    ROOT / "dswarm/solver/container_runtime.py",
    ROOT / "dswarm/solver/runtime_cleanup.py",
    ROOT / "dswarm/solver/runtime_diagnostics.py",
    ROOT / "dswarm/solver/runtime_factory.py",
    ROOT / "dswarm/solver/runtime_policy.py",
    ROOT / "dswarm/solver/runtime_snapshot.py",
    ROOT / "dswarm/swarm/runtime.py",
    ROOT / "apps/web/routes/runtime_pools.py",
)


def _python_files(root: Path):
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _read_source(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(_read_source(path), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_production_shell_entry_does_not_call_legacy_container_facade():
    offenders: list[str] = []
    for root in PRODUCTION_ROOTS:
        for path in _python_files(root):
            if path == ROOT / "dswarm/solver/container_exec.py":
                continue
            tree = ast.parse(_read_source(path), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id in {"ensure_container", "ensure_container_legacy", "ensure_container_legacy_for_tests"}:
                        offenders.append(f"{path}:{node.lineno}:{node.func.id}")
    assert offenders == []


def test_runtime_operation_kinds_cover_every_spawn_entrypoint():
    expected = {
        "bootstrap", "ordinary", "review", "recon",
        "recovery", "standby", "resolve", "btw",
    }
    assert _RUNTIME_OPERATION_KINDS == expected
    assert runtime_operation_for_spawn(mode="recon") == "recon"
    assert runtime_operation_for_spawn(mode="review") == "review"
    assert runtime_operation_for_spawn(mode="explore") == "ordinary"
    assert runtime_operation_for_spawn(mode="solve") == "bootstrap"
    assert runtime_operation_for_spawn(mode="solve", requested="recovery") == "recovery"


def test_runtime_modules_have_no_graph_or_reason_dependency():
    forbidden_modules = {
        "dswarm.swarm.shared_graph",
        "dswarm.core.events",
        "dswarm.solver.reason",
        "dswarm.swarm.reason_scheduler",
        "dswarm.swarm.blackboard_delta_payload",
    }
    offenders = {
        str(path): sorted(_imported_modules(path) & forbidden_modules)
        for path in RUNTIME_MODULES
    }
    assert {path: modules for path, modules in offenders.items() if modules} == {}


def test_runtime_has_no_canonical_runtime_event_namespace():
    assert all(not name.startswith("RUNTIME_") for name in EventType.__members__)
    assert all(not str(value.value).startswith("runtime.") for value in EventType)


def test_run_finished_is_not_a_runtime_teardown_trigger():
    source = (ROOT / "apps/web/run_manager.py").read_text(encoding="utf-8")
    finished_branch = source[source.index("elif ev.event_type is EventType.RUN_FINISHED:"):]
    finished_branch = finished_branch[:finished_branch.index("elif ev.event_type is EventType.RUN_QUEUED:")]
    assert "pool_manager.close" not in finished_branch
    assert "pool_manager" not in finished_branch


def test_m5_usage_record_schema_remains_canonical():
    assert [field.name for field in fields(UsageRecord)] == [
        "usage_id", "producer", "record_kind", "provider_call_id", "invocation_id",
        "run_id", "challenge_id", "worker_instance_id", "solver_id", "profile_id",
        "configured_account_id", "billing_account_id", "call_outcome", "usage_status",
        "input_tokens", "output_tokens", "usd", "operation_kind",
    ]


def test_runtime_api_and_private_diagnostics_are_secret_free(tmp_path: Path):
    view = RuntimePoolView(
        pool_id="pool-v1::../../secret/path",
        state="ready",
        generation=1,
        pool_instance_id="instance-1",
        active_workers=2,
        waiting_workers=1,
        capacity=4,
        failure=RuntimeFailure("auth", "probe_denied"),
        recovery_episode=0,
    )
    projected = _project_view(view)
    assert set(projected) == {
        "pool_id", "state", "generation", "pool_instance_id", "active_workers",
        "waiting_workers", "capacity", "failure", "recovery_episode",
    }
    assert ".." not in projected["pool_id"]
    assert "/" not in projected["pool_id"]

    store = RuntimeDiagnosticsStore(run_root=tmp_path / "sessions/run-1", run_id="run-1")
    row = store.record_transition(view, error="/host/home/secret/api_key=raw")
    text = store.lifecycle_path(view.pool_id).read_text(encoding="utf-8")
    assert row["actor"] == ""
    assert "raw" not in text
    assert "api_key" not in text
    assert "/host/home" not in text
    assert "secret/path" not in text


def test_runtime_fixtures_are_not_imported_by_production():
    fixture_root = ROOT / "tests/integration/fixtures"
    fixture_paths = {str(path.resolve()) for path in fixture_root.rglob("*") if path.is_file()}
    offenders: list[str] = []
    for root in PRODUCTION_ROOTS:
        for path in _python_files(root):
            text = _read_source(path)
            if any(str(Path(candidate).resolve()) in text for candidate in fixture_paths):
                offenders.append(str(path))
    assert offenders == []


@pytest.mark.parametrize("name", ["runtime_diagnostics", "runtime_cleanup", "container_pool"])
def test_runtime_sidecars_do_not_write_shared_graph(name: str):
    source = (ROOT / "dswarm/solver" / f"{name}.py").read_text(encoding="utf-8")
    assert "SharedGraph" not in source
    assert "append_fact" not in source
    assert "add_evidence" not in source
