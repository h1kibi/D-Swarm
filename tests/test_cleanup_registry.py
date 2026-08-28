from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from dswarm.models.solve_graph import Challenge
from dswarm.solver.cli_solver import CliSolver
from dswarm.swarm.blackboard_bridge import BlackboardBridgeMixin
from dswarm.swarm.cleanup_registry import parse_cleanup_marker, validate_cleanup_action
from dswarm.swarm.shared_graph import SQLiteSharedGraph
from dswarm.swarm.swarm import Swarm


def _challenge() -> Challenge:
    return Challenge(id="cleanup-test", name="cleanup", category="web")


def test_cleanup_marker_is_typed_and_rejects_shell_or_escape() -> None:
    assert parse_cleanup_marker("remove_artifact:workers/w1/output.txt") == (
        "remove_artifact",
        "workers/w1/output.txt",
    )
    assert parse_cleanup_marker("close_session:session:42") == (
        "close_session",
        "session:42",
    )

    for marker in (
        "rm -rf /:workers/w1/output.txt",
        "unknown:workers/w1/output.txt",
        "remove_artifact:/tmp/output.txt",
        "remove_artifact:../outside.txt",
        "remove_artifact:shared/output.txt",
        "remove_artifact:workers/w1/out;whoami",
    ):
        with pytest.raises(ValueError):
            parse_cleanup_marker(marker)


def test_cleanup_graph_is_append_only_idempotent_and_rebuildable(tmp_path: Path) -> None:
    db = tmp_path / "graph.db"
    graph = SQLiteSharedGraph.open(db_path=db, challenge=_challenge())
    first = graph.register_cleanup_action(
        actor="worker-1",
        action_type="remove_artifact",
        target="workers/worker-1/output.txt",
        intent_id="intent-1",
        idempotency_key="cleanup-1",
    )
    same = graph.register_cleanup_action(
        actor="worker-1",
        action_type="remove_artifact",
        target="workers/worker-1/output.txt",
        intent_id="intent-1",
        idempotency_key="cleanup-1",
    )
    assert same["action_id"] == first["action_id"]
    assert [event["kind"] for event in graph.events()] == [
        "cleanup_action_registered",
    ]

    failed_seq = graph.cleanup_action_failed(
        actor="coordinator", action_id=first["action_id"], reason="adapter unavailable",
    )
    assert failed_seq > first["registration_seq"]
    executed_seq = graph.cleanup_action_executed(
        actor="coordinator", action_id=first["action_id"], result="removed",
    )
    assert executed_seq > failed_seq
    assert graph.cleanup_action_executed(
        actor="coordinator", action_id=first["action_id"], result="ignored retry",
    ) == executed_seq
    assert [event["kind"] for event in graph.events()] == [
        "cleanup_action_registered", "cleanup_failed", "cleanup_executed",
    ]
    graph.close()

    rebuilt = SQLiteSharedGraph.open(db_path=db, challenge=_challenge())
    action = rebuilt.cleanup_actions()[0]
    assert action["status"] == "executed"
    assert action["failure_reason"] is None
    assert action["result"] == "removed"
    rebuilt.close()


class _MarkerGraph:
    def __init__(self) -> None:
        self.actions: list[dict] = []

    def register_cleanup_action(self, **kwargs):
        self.actions.append(kwargs)
        return {"action_id": "cleanup-marker-1", "status": "registered"}


def test_cli_cleanup_marker_registers_workspace_relative_artifact(tmp_path: Path) -> None:
    worker_cwd = tmp_path / "workers" / "worker-1"
    worker_cwd.mkdir(parents=True)
    graph = _MarkerGraph()
    solver = CliSolver(
        None,
        _challenge(),
        shared_graph=graph,
        workdir=str(worker_cwd),
        kb=False,
    )
    solver._current_workdir = worker_cwd
    events = []

    async def emit(kind, **fields):
        events.append((kind, fields))

    solver._emit_bb = emit
    asyncio.run(solver._stream_markers("CLEANUP=remove_artifact:output.txt"))

    assert graph.actions[0]["target"] == "workers/worker-1/output.txt"
    assert events[-1][0] == "cleanup_action_registered"
    assert "target" not in events[-1][1]
    assert events[-1][1]["target_length"] == len("workers/worker-1/output.txt")


def test_blackboard_bridge_redacts_cleanup_target_and_details() -> None:
    bridge = BlackboardBridgeMixin()
    target = "workers/cli-pi/private-output.txt"
    reason = "adapter secret token leaked in internal reason"
    registered = bridge._graph_event_to_bb({
        "seq": 1,
        "kind": "cleanup_action_registered",
        "actor": "cli-pi",
        "payload": {
            "action_id": "cleanup-1",
            "action_type": "remove_artifact",
            "actor": "cli-pi",
            "intent_id": "I-cleanup",
            "target": target,
        },
    })[0][1]
    failed = bridge._graph_event_to_bb({
        "seq": 2,
        "kind": "cleanup_failed",
        "actor": "coordinator",
        "payload": {"action_id": "cleanup-1", "reason": reason},
    })[0][1]
    executed = bridge._graph_event_to_bb({
        "seq": 3,
        "kind": "cleanup_executed",
        "actor": "coordinator",
        "payload": {"action_id": "cleanup-1", "result": "adapter returned private output"},
    })[0][1]

    assert target not in repr(registered)
    assert registered["target_length"] == len(target)
    assert reason not in repr(failed)
    assert failed["reason_length"] == len(reason)
    assert "adapter returned private output" not in repr(executed)
    assert executed["result_length"] == len("adapter returned private output")

def test_swarm_cleanup_runs_reverse_order_and_isolates_failures(tmp_path: Path) -> None:
    class Graph:
        def __init__(self):
            self.executed = []
            self.failed = []

        def cleanup_actions(self, *, include_terminal):
            return [
                {"action_id": "a1", "action_type": "close_session", "target": "s1", "actor": "w1", "owner_key": "w1"},
                {"action_id": "a2", "action_type": "stop_listener", "target": "l2", "actor": "w1", "owner_key": "w1"},
                {"action_id": "a3", "action_type": "revoke_credential", "target": "c3", "actor": "w1", "owner_key": "w1"},
            ]

        def cleanup_action_executed(self, **kwargs):
            self.executed.append(kwargs["action_id"])

        def cleanup_action_failed(self, **kwargs):
            self.failed.append((kwargs["action_id"], kwargs["reason"]))

    graph = Graph()
    seen = []

    def execute(action):
        seen.append(action["action_id"])
        if action["action_id"] == "a2":
            raise RuntimeError("adapter failed")
        return "ok"

    swarm = object.__new__(Swarm)
    swarm.shared_graph = graph
    swarm.workspace_root = tmp_path
    swarm.cleanup_executor = execute
    asyncio.run(swarm._execute_registered_cleanups())

    assert seen == ["a3", "a2", "a1"]
    assert graph.executed == ["a3", "a1"]
    assert graph.failed[0][0] == "a2"


def test_swarm_artifact_cleanup_is_bounded_and_idempotent(tmp_path: Path) -> None:
    workers = tmp_path / "workers" / "worker-1"
    workers.mkdir(parents=True)
    artifact = workers / "output.txt"
    artifact.write_text("x", encoding="utf-8")

    swarm = object.__new__(Swarm)
    swarm.workspace_root = tmp_path
    swarm.shared_graph = None
    swarm.cleanup_executor = None

    result = asyncio.run(swarm._execute_one_cleanup_action({
        "action_type": "remove_artifact",
        "target": "workers/worker-1/output.txt",
        "actor": "worker-1",
        "owner_key": "worker-1",
    }))
    assert result == "removed"
    assert not artifact.exists()
    assert asyncio.run(swarm._execute_one_cleanup_action({
        "action_type": "remove_artifact",
        "target": "workers/worker-1/output.txt",
        "actor": "worker-1",
        "owner_key": "worker-1",
    })) == "already absent"

    with pytest.raises(RuntimeError):
        asyncio.run(swarm._execute_one_cleanup_action({
            "action_type": "remove_artifact",
            "target": "workers/../outside.txt",
            "actor": "worker-1",
            "owner_key": "worker-1",
        }))
