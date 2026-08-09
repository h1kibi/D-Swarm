"""Legacy run archive-and-cleanup safety tests."""

from __future__ import annotations

import tarfile

from apps.web.run_manager import RunManager


def test_archive_legacy_runs_is_dry_run_then_verifies_before_delete(tmp_path):
    root = tmp_path / "sessions"
    old = root / "old-run"
    graph = old / "graph"
    graph.mkdir(parents=True)
    (graph / "shared_graph.db").write_bytes(b"sqlite")
    (root / "old-run.jsonl").write_text('{"event_type":"run.started","ts":1}\n')

    manager = RunManager(sessions_root=root)
    dry = manager.archive_legacy_runs(dry_run=True)
    assert dry["archived"] == ["old-run"]
    assert (old / "graph" / "shared_graph.db").exists()

    result = manager.archive_legacy_runs()
    assert result["archived"] == ["old-run"]
    assert not old.exists()
    assert not (root / "old-run.jsonl").exists()
    archive = root / "_archive" / "old-run.tar.gz"
    assert archive.exists()
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
    assert "old-run/graph/shared_graph.db" in names


def test_archive_legacy_runs_skips_active_runs(tmp_path):
    root = tmp_path / "sessions"
    run = root / "live"
    run.mkdir(parents=True)
    (run / "graph").mkdir()
    (run / "graph" / "shared_graph.db").write_bytes(b"sqlite")

    manager = RunManager(sessions_root=root)
    live = manager.create("live")
    live.started = True
    live.finished = False
    result = manager.archive_legacy_runs()
    assert result["archived"] == []
    assert result["skipped"] == ["live:active"]
    assert (run / "graph" / "shared_graph.db").exists()
