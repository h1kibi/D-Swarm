from __future__ import annotations

import asyncio
from pathlib import Path

from dswarm.models.solve_graph import Challenge
from dswarm.swarm.review_flow import ReviewFlowMixin
from dswarm.swarm.shared_graph import SQLiteSharedGraph


class _ReviewHost(ReviewFlowMixin):
    def __init__(self, graph: SQLiteSharedGraph, challenge: Challenge):
        self.shared_graph = graph
        self.challenge = challenge
        self.review_policy = {"enabled": True, "max_challenges_per_cycle": 8}
        self._last_review_proposal_seq = 0
        self._queued_review_requests = []
        self.barren_limit = 0


def _challenge() -> Challenge:
    return Challenge(
        id="m9-review",
        name="m9 review",
        category="web",
        mode="pentest",
        goal="find and prove the issue",
    )


def _graph(tmp_path: Path) -> SQLiteSharedGraph:
    graph = SQLiteSharedGraph.open(db_path=tmp_path / "review.db", challenge=_challenge())
    graph.save_poc(
        actor="worker-1",
        poc_id="poc-1",
        path=str(tmp_path / "poc.py"),
        entry_command="python3 poc.py",
        artifact_id="artifact-1",
    )
    graph.register_poc_reproduction(actor="worker-1", poc_id="poc-1", indicator="vulnerable")
    return graph


def _finding_id(graph: SQLiteSharedGraph, seq: int) -> str:
    event = next(event for event in graph.events() if int(event["seq"]) == seq)
    return str(event["payload"]["finding_id"])


def _finding(graph: SQLiteSharedGraph, *, severity: str = "blocker", poc_id: str | None = "poc-1") -> str:
    seq = graph.add_review_finding(
        actor="reviewer",
        kind="confirmed_issue",
        severity=severity,
        summary=f"reproduction-backed finding {severity} {poc_id}",
        poc_id=poc_id,
    )
    return _finding_id(graph, seq)


def test_eligible_poc_verification_rejects_missing_reproduction(tmp_path: Path):
    challenge = _challenge()
    graph = SQLiteSharedGraph.open(db_path=tmp_path / "missing.db", challenge=challenge)
    graph.save_poc(
        actor="worker-1", poc_id="poc-1", path=str(tmp_path / "poc.py"),
        entry_command="python3 poc.py", artifact_id="artifact-1",
    )
    finding_id = _finding(graph)
    host = _ReviewHost(graph, challenge)

    assert host.eligible_poc_verification(finding_id) is None
    graph.close()


def test_eligible_poc_verification_rejects_unknown_or_ambiguous_poc(tmp_path: Path):
    graph = _graph(tmp_path)
    unknown = _finding(graph, poc_id="does-not-exist")
    assert _ReviewHost(graph, _challenge()).eligible_poc_verification(unknown) is None

    seq = graph.add_review_finding(
        actor="reviewer", kind="confirmed_issue", severity="blocker",
        summary="ambiguous references", poc_id="poc-1", poc_ids=["poc-1", "poc-2"],
    )
    ambiguous = _finding_id(graph, seq)
    assert _ReviewHost(graph, _challenge()).eligible_poc_verification(ambiguous) is None
    graph.close()


def test_eligible_poc_verification_rejects_non_blocker_and_terminal(tmp_path: Path):
    graph = _graph(tmp_path)
    host = _ReviewHost(graph, _challenge())
    non_blocker = _finding(graph, severity="warn")
    assert host.eligible_poc_verification(non_blocker) is None

    terminal = _finding(graph)
    reproduction = graph.get_poc_reproduction("poc-1")
    assert reproduction is not None
    assert graph.begin_poc_verification(
        actor="verifier", poc_id="poc-1", verification_id="v-1",
        reproduction_id=reproduction["reproduction_id"], worker_id="verifier",
    )
    graph.append_poc_verification_terminal(
        actor="verifier", poc_id="poc-1", verification_id="v-1", verified=False,
    )
    assert host.eligible_poc_verification(terminal) is None
    graph.close()


def test_eligible_poc_verification_returns_bounded_metadata(tmp_path: Path):
    graph = _graph(tmp_path)
    finding_id = _finding(graph)
    eligible = _ReviewHost(graph, _challenge()).eligible_poc_verification(finding_id)

    assert eligible is not None
    assert eligible["worker_class"] == "verifier"
    assert eligible["reproduction_id"] == graph.get_poc_reproduction("poc-1")["reproduction_id"]
    assert eligible["source_finding_id"] == finding_id
    assert eligible["poc_id"] == "poc-1"
    assert eligible["goal"] == (
        "Run the saved PoC entrypoint and confirm the registered reproduction indicator "
        "from real verifier output."
    )
    assert "command" not in eligible
    assert "entry_command" not in eligible
    graph.close()


def test_review_finding_creates_one_verifier_intent_and_duplicate_trigger_is_idempotent(
    tmp_path: Path,
):
    graph = _graph(tmp_path)
    host = _ReviewHost(graph, _challenge())
    graph.add_review_proposal(
        actor="reviewer", marker="REVIEW_FINDING",
        payload={"kind": "confirmed_issue", "severity": "blocker",
                 "summary": "reproduction-backed finding", "poc_id": "poc-1"},
    )
    emitted: list[tuple[str, dict]] = []

    async def emit_bb(kind: str, **fields):
        emitted.append((kind, fields))

    assert asyncio.run(host._drain_review_proposals(emit_bb=emit_bb)) == 1
    assert asyncio.run(host._drain_review_proposals(emit_bb=emit_bb)) == 0

    intents = [row for row in graph.dispatchable_intents() if row["worker_class"] == "verifier"]
    assert len(intents) == 1
    intent = intents[0]
    finding_event = next(event for event in graph.events() if event["kind"] == "review_finding")
    finding_id = finding_event["payload"]["finding_id"]
    assert intent["reproduction_id"] == graph.get_poc_reproduction("poc-1")["reproduction_id"]
    assert intent["source_finding_id"] == finding_id
    assert "command" not in intent
    assert "entry_command" not in intent
    assert any(kind == "intent_proposed" and fields.get("worker_class") == "verifier"
               for kind, fields in emitted)
    graph.close()
