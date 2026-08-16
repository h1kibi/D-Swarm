from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from apps.web.run_manager import RunManager
from dswarm.swarm.route_telemetry import MetricsSink, RouteMetricRecord


def _record(
    index: int, *, kind: str = "fact_appended", actor: str = "cli-pi-2"
) -> RouteMetricRecord:
    return RouteMetricRecord(
        record_id=f"fact_write:{index}:fact:{40 + index}:base",
        kind=kind,
        challenge_id="chal",
        event_ts=1_755_300_000.0 + index,
        observed_at=1_755_300_000.2 + index,
        actor=actor,
        fact_seq=40 + index,
        route_hash="route-abc",
        route_lineage="inherited",
        lineage_reason="intent_product",
        intent_ids=("I-b", "I-a", "I-a"),
        verified=index % 2 == 0,
    )


def _json_rows(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_metrics_sink_uses_real_workspace_metrics_path_and_schema(tmp_path: Path) -> None:
    manager = RunManager(sessions_root=tmp_path / "sessions")
    workspace = manager.workspace_dir("run/../0001")
    sink = MetricsSink(workspace, run_id="run/../0001")

    assert sink.path == workspace / "metrics" / "route-telemetry.jsonl"
    assert sink.checkpoint_path == workspace / "metrics" / "route-telemetry.checkpoint.json"
    assert sink.max_bytes == 5 * 1024 * 1024
    assert sink.retention_generations == 3

    assert sink.append(_record(1)) is True
    rows = _json_rows(sink.path)
    assert rows == [
        {
            "actor": "cli-pi-2",
            "challenge_id": "chal",
            "event_ts": 1_755_300_001.0,
            "fact_seq": 41,
            "intent_ids": ["I-a", "I-b"],
            "kind": "fact_appended",
            "lineage_reason": "intent_product",
            "observed_at": 1_755_300_001.2,
            "record_id": "fact_write:1:fact:41:base",
            "record_seq": 1,
            "route_hash": "route-abc",
            "route_lineage": "inherited",
            "run_id": "run/../0001",
            "schema_version": 1,
            "verified": False,
        }
    ]


def test_metrics_sink_deduplicates_record_id_without_advancing_sequence(tmp_path: Path) -> None:
    sink = MetricsSink(tmp_path / "workspace", run_id="run-1")

    assert sink.append(_record(1)) is True
    assert sink.append(_record(1, actor="duplicate")) is False
    assert sink.append(_record(2)) is True

    rows = _json_rows(sink.path)
    assert [row["record_seq"] for row in rows] == [1, 2]
    assert [row["record_id"] for row in rows] == [
        "fact_write:1:fact:41:base",
        "fact_write:2:fact:42:base",
    ]
    assert sink.counters["records_total"] == 2


def test_metrics_sink_rotates_deterministically_and_caps_retention(tmp_path: Path) -> None:
    sink = MetricsSink(
        tmp_path / "workspace",
        run_id="run-1",
        max_bytes=1,
        retention_generations=3,
    )

    for index in range(1, 6):
        assert sink.append(_record(index)) is True

    metrics_dir = sink.path.parent
    assert sink.path.exists()
    assert sink.path.stat().st_size == 0
    assert [p.name for p in sorted(metrics_dir.glob("route-telemetry.jsonl.*"))] == [
        "route-telemetry.jsonl.1",
        "route-telemetry.jsonl.2",
        "route-telemetry.jsonl.3",
    ]
    retained_rows = []
    for generation in (3, 2, 1):
        retained_rows.extend(_json_rows(Path(f"{sink.path}.{generation}")))
    assert [row["record_seq"] for row in retained_rows] == [3, 4, 5]


def test_checkpoint_restart_does_not_repeat_aggregates(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    sink = MetricsSink(workspace, run_id="run-1")
    sink.append(_record(1))
    sink.append(_record(2, kind="dedupe_hit"))

    delta = sink.aggregate_delta()
    assert delta["records_total"] == 2
    assert delta["by_kind"] == {"dedupe_hit": 1, "fact_appended": 1}
    checkpoint = json.loads(sink.checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["last_record_id"] == "fact_write:2:fact:42:base"
    assert checkpoint["last_record_seq"] == 2
    assert checkpoint["counters"] == sink.counters

    reopened = MetricsSink(workspace, run_id="run-1")
    assert reopened.counters == sink.counters
    assert reopened.aggregate_delta() == {}


def test_restart_reconciles_only_records_after_lagging_checkpoint(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    sink = MetricsSink(workspace, run_id="run-1")
    sink.append(_record(1))
    assert sink.aggregate_delta()["records_total"] == 1
    sink.append(_record(2, kind="fact_promoted"))

    reopened = MetricsSink(workspace, run_id="run-1")
    assert reopened.counters["records_total"] == 2
    assert reopened.counters["by_kind"] == {"fact_appended": 1, "fact_promoted": 1}
    assert reopened.aggregate_delta() == {
        "records_total": 1,
        "by_kind": {"fact_promoted": 1},
        "by_lineage": {"inherited": 1},
        "by_route": {
            "route-abc": {
                "records_total": 1,
                "verified_total": 1,
                "by_kind": {"fact_promoted": 1},
            }
        },
        "verified_total": 1,
    }


def test_trailing_partial_line_is_ignored_and_counted_on_restart(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    sink = MetricsSink(workspace, run_id="run-1")
    sink.append(_record(1))
    sink.aggregate_delta()
    with sink.path.open("ab") as handle:
        handle.write(b'{"schema_version":1,"record_id":"partial"')

    reopened = MetricsSink(workspace, run_id="run-1")

    assert reopened.counters["records_total"] == 1
    assert reopened.partial_lines_ignored == 1
    assert reopened.aggregate_delta() == {}
    checkpoint = json.loads(reopened.checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["partial_lines_ignored"] == 1

    reopened_again = MetricsSink(workspace, run_id="run-1")
    assert reopened_again.partial_lines_ignored == 1
    assert reopened_again.append(_record(2)) is True
    assert [row["record_seq"] for row in _json_rows(reopened_again.path)] == [1, 2]


def test_malformed_checkpoint_counters_rebuild_from_retained_jsonl(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    sink = MetricsSink(workspace, run_id="run-1")
    sink.append(_record(1))
    sink.aggregate_delta()
    checkpoint = json.loads(sink.checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["counters"]["by_kind"] = "corrupt"
    sink.checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    reopened = MetricsSink(workspace, run_id="run-1")

    assert reopened.counters["records_total"] == 1
    assert reopened.counters["by_kind"] == {"fact_appended": 1}
    assert reopened.aggregate_delta()["records_total"] == 1


def test_duplicate_record_id_does_not_hide_the_highest_persisted_sequence(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    sink = MetricsSink(workspace, run_id="run-1")
    sink.append(_record(1))
    duplicate = _json_rows(sink.path)[0]
    duplicate["record_seq"] = 99
    with sink.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(duplicate) + "\n")

    reopened = MetricsSink(workspace, run_id="run-1")
    assert reopened.counters["records_total"] == 1
    assert reopened.append(_record(2)) is True
    assert [row["record_seq"] for row in _json_rows(reopened.path)] == [1, 99, 100]


def test_two_sink_instances_share_a_single_path_writer_lock(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    first = MetricsSink(workspace, run_id="run-1")
    second = MetricsSink(workspace, run_id="run-1")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda index: (first if index % 2 else second).append(_record(index)),
                range(1, 41),
            )
        )

    assert all(results)
    rows = _json_rows(first.path)
    assert len(rows) == 40
    assert sorted(int(row["record_seq"]) for row in rows) == list(range(1, 41))
    assert len({str(row["record_id"]) for row in rows}) == 40
