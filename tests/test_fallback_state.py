from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_fallback_fatal_failure_probes_default_before_switch(tmp_path, monkeypatch):
    from dswarm.models.solve_graph import Challenge
    from dswarm.sandbox.manager import SandboxManager
    from dswarm.swarm.swarm import Swarm

    swarm = Swarm(
        Challenge(id="fallback-fatal", name="fallback-fatal", category="web"),
        llm=None, sandbox=SandboxManager(root=tmp_path / "sandbox"),
        binding_snapshot={
            "roles": {"worker": {"provider": "override", "model": "bad"}},
            "fallback": {"worker": {"provider": "default", "model": "good", "base_url": "https://default.test/v1"}},
        },
        llm_profiles={"worker": {"provider": "override", "model": "bad"}},
    )
    called = []
    monkeypatch.setattr(swarm, "_fallback_probe", lambda binding: (called.append(binding) or True, {"ok": True}))
    assert await swarm.trigger_role_fallback("worker", reason="quota", is_fatal=True, failure_category="insufficient_quota")
    assert called[0]["model"] == "good"
    assert swarm._effective_role_bindings["worker"]["model"] == "good"


@pytest.mark.asyncio
async def test_fallback_probe_failure_blocks_role_without_switch(tmp_path, monkeypatch):
    from dswarm.models.solve_graph import Challenge
    from dswarm.sandbox.manager import SandboxManager
    from dswarm.swarm.swarm import Swarm

    swarm = Swarm(
        Challenge(id="fallback-block", name="fallback-block", category="web"),
        llm=None, sandbox=SandboxManager(root=tmp_path / "sandbox"),
        binding_snapshot={
            "roles": {"worker": {"provider": "override", "model": "bad"}},
            "fallback": {"worker": {"provider": "default", "model": "good", "base_url": "https://default.test/v1"}},
        },
    )
    monkeypatch.setattr(swarm, "_fallback_probe", lambda binding: (False, {"ok": False, "reason": "credential_missing"}))
    assert await swarm.trigger_role_fallback("worker", reason="auth", is_fatal=True, failure_category="auth_invalid") is False
    assert swarm._effective_role_bindings["worker"]["model"] == "bad"
    assert swarm._role_fallback_blocked["worker"] == "credential_missing"
    assert await swarm.trigger_role_fallback("worker", reason="again", is_fatal=True) is False
