"""The web swarm driver must hand the frozen M9 runtime context to the Swarm.

Live regression this guards (2026-10-16, runs run-6450/run-6690): the driver
defined `runtime_context_kwargs(run)` but never used it, so the constructed
Swarm had `runtime_policy=None`. Every container-backend spawn then failed
closed with `RuntimePolicyError("runtime_policy_required")` in ~20ms — intent
dispatch died as `runtime_failure`, zero containers were ever created, and the
run rail still reported a live run. Wiring-level tests are the only cheap
detector for this: the failure needs a real Docker spawn to surface in
behavioral paths.
"""

from __future__ import annotations

import pytest

from apps.web import drivers


class _FakeBus:
    async def emit(self, *a, **k):  # noqa: ANN002, ANN003
        pass

    async def close(self):
        pass

    def add_sink(self, *a):  # noqa: ANN002
        pass


def _driver_for(body: dict):
    return drivers._swarm_driver(drivers._infer_challenge(body))


@pytest.mark.asyncio
async def test_swarm_driver_forwards_frozen_runtime_context(monkeypatch):
    """policy/snapshot/pool_manager frozen by RunManager reach the Swarm verbatim."""
    import dswarm.swarm.swarm as sw

    captured: dict = {}

    class FakeSwarm:
        def __init__(self, challenge, **kw):
            captured.update(kw)

        async def run(self):
            class _Out:
                flag = None

            return _Out()

    monkeypatch.setattr(sw, "Swarm", FakeSwarm)
    driver = _driver_for({"kind": "swarm", "challenge": {"description": "solve"}})

    policy = object()  # opaque: must be forwarded unchanged
    snapshot = object()
    pool = object()

    class FakeRun:
        run_id = "r-frozen"
        bus = _FakeBus()
        hitl = None
        worker_cmds = None
        cost = None
        flag = None
        runtime_policy = policy
        runtime_snapshot = snapshot
        pool_manager = pool

    await driver(FakeRun())

    assert captured.get("runtime_policy") is policy
    assert captured.get("runtime_snapshot") is snapshot
    assert captured.get("pool_manager") is pool


@pytest.mark.asyncio
async def test_swarm_driver_tolerates_run_without_frozen_context(monkeypatch):
    """A run with no frozen context still constructs (legacy/local-dev paths)."""
    import dswarm.swarm.swarm as sw

    captured: dict = {}

    class FakeSwarm:
        def __init__(self, challenge, **kw):
            captured.update(kw)
            captured["_constructed"] = True

        async def run(self):
            class _Out:
                flag = None

            return _Out()

    monkeypatch.setattr(sw, "Swarm", FakeSwarm)
    driver = _driver_for({"kind": "swarm", "challenge": {"description": "solve"}})

    class BareRun:
        run_id = "r-bare"
        bus = _FakeBus()
        hitl = None
        worker_cmds = None
        cost = None
        flag = None

    await driver(BareRun())

    assert captured.get("_constructed") is True
    assert captured.get("runtime_policy", None) is None
    assert captured.get("runtime_snapshot", None) is None
