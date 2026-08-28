from __future__ import annotations

from pathlib import Path

from dswarm.models.solve_graph import Challenge
from dswarm.swarm.shared_graph import (
    EV_POC_REPRODUCTION_REGISTERED,
    EV_POC_SAVED,
    SQLiteSharedGraph,
)


def test_poc_lifecycle_projection_snapshot_survives_reopen(tmp_path: Path) -> None:
    challenge = Challenge(
        id="poc-snapshot",
        name="poc snapshot",
        category="web",
        mode="pentest",
    )
    db = tmp_path / "graph.db"
    graph = SQLiteSharedGraph.open(db_path=db, challenge=challenge)
    try:
        saved_seq = graph.save_poc(
            actor="worker-a",
            poc_id="poc-1",
            path="shared/objects/aa/bb/object-1",
            entry_command="python repro.py",
            artifact_id="sha256:object-1",
            name="repro.py",
            note="observable response",
        )
        registration = graph.register_poc_reproduction(
            actor="worker-a", poc_id="poc-1", indicator="  POC_OK  "
        )
        before = graph.get_poc_reproduction("poc-1")
        event_kinds = [
            event["kind"] for event in graph.events_since(0)
            if event["kind"] in {EV_POC_SAVED, EV_POC_REPRODUCTION_REGISTERED}
        ]
    finally:
        graph.close()

    reopened = SQLiteSharedGraph.open(db_path=db, challenge=challenge)
    try:
        assert saved_seq > 0
        assert event_kinds == [EV_POC_SAVED, EV_POC_REPRODUCTION_REGISTERED]
        assert before == reopened.get_poc_reproduction("poc-1")
        assert before == {
            "reproduction_id": registration["reproduction_id"],
            "poc_id": "poc-1",
            "intent_id": "",
            "artifact_id": "sha256:object-1",
            "command": "python repro.py",
            "indicator": "POC_OK",
            "registration_seq": registration["registration_seq"],
            "status": "registered",
            "verification_id": "",
            "started_seq": None,
            "terminal_seq": None,
            "worker_id": "",
            "finding_id": "",
            "pool_identity": "",
            "failure_reason": "",
            "exit_code": None,
            "observed_location": "",
            "provenance_artifact_ids": (),
            "diagnostics": "",
            "elapsed_ms": None,
            "path": "shared/objects/aa/bb/object-1",
            "name": "repro.py",
            "entry_command": "python repro.py",
        }
    finally:
        reopened.close()

