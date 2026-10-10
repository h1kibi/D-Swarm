import json
import threading
from pathlib import Path

import pytest

from dswarm.solver import workspace as workspace_mod
from dswarm.solver.workspace import (
    cleanup_worker_scratch,
    ensure_workspace,
    link_shared_into_worker,
    materialize_input,
    materialize_shared_artifact,
    write_manifest,
)


def test_manifest_writes_are_serialized_and_leave_valid_json(tmp_path):
    workspace = tmp_path / "workspace"
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def writer(i: int) -> None:
        try:
            barrier.wait()
            write_manifest(workspace, runtime={"writer": i})
        except BaseException as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == 1
    assert manifest["runtime"]["writer"] in range(8)


def test_manifest_replace_retries_windows_permission_error(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    calls = {"n": 0}
    real_replace = workspace_mod.os.replace

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(5, "access denied")
        return real_replace(src, dst)

    monkeypatch.setattr(workspace_mod.os, "replace", flaky_replace)
    write_manifest(workspace)

    assert calls["n"] == 2
    assert json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))["version"] == 1
def test_workspace_inputs_are_content_addressed_and_manifested(tmp_path):
    workspace = tmp_path / "workspace"
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("same bytes")
    b.write_text("same bytes")

    ma = materialize_input(workspace, a, name="a.txt")
    mb = materialize_input(workspace, b, name="b.txt")

    assert ma["sha256"] == mb["sha256"]
    assert ma["object"] == mb["object"]
    assert ma["object"].read_text() == "same bytes"
    assert (workspace / "inputs" / "by-name" / "a.txt").resolve() == ma["object"]
    assert (workspace / "inputs" / "by-name" / "b.txt").resolve() == ma["object"]

    manifest = json.loads((workspace / "manifest.json").read_text())
    assert manifest["artifact_truth"] == "shared_graph.events"
    assert sorted(i["name"] for i in manifest["inputs"]) == ["a.txt", "b.txt"]




def test_shared_artifact_links_use_multisegment_relative_targets(tmp_path):
    workspace = tmp_path / "workspace"
    worker = workspace / "workers" / "cli-claude-1"
    worker.mkdir(parents=True)
    payload = tmp_path / "payload.py"
    payload.write_text("print('hello')")

    art = materialize_shared_artifact(workspace, payload, name="payload.py", kind="poc")
    link = link_shared_into_worker(workspace, worker, "payload.py", art["sha256"])

    assert link.is_symlink()
    assert not link.readlink().is_absolute()
    assert "shared/objects" in link.readlink().as_posix()
    assert link.resolve() == art["object"]
    index_rows = (workspace / "shared" / "index.jsonl").read_text().splitlines()
    assert len(index_rows) == 1
    assert json.loads(index_rows[0])["kind"] == "poc"


def test_cleanup_worker_scratch_preserves_winner_only(tmp_path):
    workspace = ensure_workspace(tmp_path / "workspace")
    workers = workspace / "workers"
    for name in ("cli-claude-1", "cli-codex-2", "_homes"):
        (workers / name).mkdir(parents=True)
    (workspace / "shared" / "objects" / "aa" / "bb").mkdir(parents=True)
    (workspace / "graph" / "shared_graph.db").write_text("db")

    removed = cleanup_worker_scratch(workers, keep=["cli-claude-1"])

    assert workers / "cli-codex-2" in removed
    assert (workers / "cli-claude-1").exists()
    assert (workers / "_homes").exists()
    assert (workspace / "shared").exists()
    assert (workspace / "graph" / "shared_graph.db").exists()
