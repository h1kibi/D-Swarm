from __future__ import annotations

from types import SimpleNamespace

import pytest

from dswarm.solver.reason import Intent
from dswarm.swarm.agents import AgentProfile, DispatchDecision
from dswarm.swarm.runtime import SwarmWorkerRuntime


def test_verifier_intent_payload_preserves_reproduction_linkage() -> None:
    intent = Intent(
        intent_id="intent-verifier",
        goal="Run the saved PoC entrypoint",
        worker_class="verifier",
        reproduction_id="poc-repro::artifact::digest",
        source_finding_id="finding-1",
    )

    payload = intent.to_payload()

    assert payload["reproduction_id"] == "poc-repro::artifact::digest"
    assert payload["source_finding_id"] == "finding-1"


@pytest.mark.asyncio
async def test_runtime_forwards_verifier_linkage_to_worker_factory() -> None:
    seen: list[dict[str, object]] = []

    class _LaneGate:
        @staticmethod
        def lane_for(*, mode: str, worker_class: str) -> str:
            return "review"

    class _Worker:
        async def run(self):
            return SimpleNamespace(solved=False)

    class _Swarm:
        challenge = SimpleNamespace(category="web")
        _worker_lane_gate = _LaneGate()
        shared_graph = None
        runtime_snapshot = None
        pool_manager = None

        @staticmethod
        def _healthy_matches(_engine: str, _healthy: list[str]) -> bool:
            return True

        @staticmethod
        def _make_cli_worker(engine: str, **kwargs: object):
            seen.append({"engine": engine, **kwargs})
            return _Worker()

        @staticmethod
        def _release_worker_account(_worker: object) -> None:
            return None

    decision = DispatchDecision(
        intent_id="intent-verifier",
        profile="pi-review",
        goal="Run the saved PoC entrypoint",
        worker_class="verifier",
        mode="review",
        reproduction_id="poc-repro::artifact::digest",
        source_finding_id="finding-1",
    )

    runtime = SwarmWorkerRuntime(_Swarm(), healthy=["pi-review"])
    await runtime.run(
        decision,
        AgentProfile(id="pi-review", worker_profile="pi-review", mode="review"),
    )

    assert seen[0]["reproduction_id"] == "poc-repro::artifact::digest"
    assert seen[0]["source_finding_id"] == "finding-1"
