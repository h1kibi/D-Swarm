"""M9a invariants for Docker-first per-run profile/runtime pools."""

from __future__ import annotations

from pathlib import Path

import pytest

from dswarm.models.solve_graph import Challenge
from dswarm.solver import container_exec
from dswarm.solver.runtime_policy import build_runtime_policy
from dswarm.swarm.swarm import Swarm


_PRODUCTION_RUNTIME_PATHS = (
    "dswarm/swarm/swarm.py",
    "dswarm/swarm/worker_runtime_mixin.py",
    "dswarm/solver/btw.py",
    "apps/web/routes/btw.py",
    "apps/web/drivers.py",
)
_LEGACY_OWNERSHIP_TOKENS = (
    "allow_container_start(",
    "ensure_container(",
    "teardown_container(",
    "_container_handle",
    "_container_runtime_id",
    "_container_unavailable",
    "def _container_for_engine",
)


def _challenge() -> Challenge:
    return Challenge(
        id="m9a-invariants",
        name="m9a-invariants",
        category="web",
        description="lock runtime ownership invariants",
        flag_format=r"flag\{[^}]+\}",
    )


def test_swarm_has_no_run_global_container_fields() -> None:
    swarm = Swarm(_challenge(), [], llm=None, sandbox=None)

    for name in (
        "_container_handle",
        "_container_runtime_id",
        "_container_unavailable",
    ):
        assert not hasattr(swarm, name)


def test_production_runtime_paths_do_not_own_legacy_containers() -> None:
    for path in _PRODUCTION_RUNTIME_PATHS:
        source = Path(path).read_text(encoding="utf-8")
        for token in _LEGACY_OWNERSHIP_TOKENS:
            assert token not in source, f"{path} still contains legacy ownership token {token}"


def test_run_finished_does_not_close_or_teardown_pool_manager() -> None:
    source = Path("dswarm/swarm/swarm.py").read_text(encoding="utf-8")
    run_block = source[source.index("    async def run(self)") : source.index("    @staticmethod", source.index("    async def run(self)"))]

    assert "teardown_container" not in run_block
    assert "pool_manager.close" not in run_block


def test_legacy_container_facade_rejects_production_policy() -> None:
    policy = build_runtime_policy(env={})

    with pytest.raises(RuntimeError, match="legacy_container_disabled"):
        container_exec.ensure_container_legacy_for_tests(
            "run-production",
            "/workspace",
            policy=policy,
        )


def test_legacy_container_facade_requires_approved_local_dev(monkeypatch) -> None:
    denied = build_runtime_policy(env={})
    allowed = build_runtime_policy(
        mode="local_dev",
        local_dev_cli_flag=True,
        env={"DSWARM_ALLOW_LOCAL_WORKERS": "1"},
    )
    sentinel = object()
    monkeypatch.setattr(
        container_exec,
        "_ensure_container_legacy",
        lambda *args, **kwargs: sentinel,
        raising=False,
    )

    assert container_exec.legacy_container_allowed(allowed) is True
    assert container_exec.legacy_container_allowed(denied) is False
    assert container_exec.ensure_container_legacy_for_tests(
        "run-local-dev",
        "/workspace",
        policy=allowed,
    ) is sentinel


def test_reason_prompt_has_no_private_runtime_diagnostics() -> None:
    from dswarm.solver.reason import build_reason_prompt

    messages = build_reason_prompt("verified fact only")
    rendered = "\n".join(str(message.get("content", "")) for message in messages)

    for private_token in (
        "runtime_degraded",
        "runtime_unavailable",
        "pool_instance_id",
        "runtime_pool_id",
        "legacy_container_disabled",
    ):
        assert private_token not in rendered
