import pytest
from dswarm.solver.runtime_policy import RuntimePolicyError, build_runtime_policy


def test_docker_is_the_default_and_policy_is_frozen():
    policy = build_runtime_policy(env={})
    assert policy.mode == "docker"
    assert policy.max_pools_per_run == 32
    with pytest.raises(AttributeError):
        policy.mode = "local_dev"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("flag", "allowed"),
    [(False, False), (True, False), (False, True)],
)
def test_local_dev_requires_both_explicit_gates(flag: bool, allowed: bool):
    env = {"DSWARM_ALLOW_LOCAL_WORKERS": "1"} if allowed else {}
    with pytest.raises(RuntimePolicyError, match="local_worker_policy_denied"):
        build_runtime_policy(mode="local_dev", local_dev_cli_flag=flag, env=env)


def test_local_dev_accepts_both_gates_without_pytest_ambient_bypass():
    policy = build_runtime_policy(
        mode="local_dev",
        local_dev_cli_flag=True,
        env={"DSWARM_ALLOW_LOCAL_WORKERS": "1", "PYTEST_CURRENT_TEST": "must-not-matter"},
    )
    assert policy.local_workers_allowed is True


@pytest.mark.parametrize("value", [0, -1, 129])
def test_pool_cap_range_is_closed(value: int):
    with pytest.raises(RuntimePolicyError, match="invalid_max_pools_per_run"):
        build_runtime_policy(max_pools_per_run=value, env={})

@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "On"])
def test_documented_truthy_values_enable_environment_gate(raw: str):
    policy = build_runtime_policy(
        mode="local_dev",
        local_dev_cli_flag=True,
        env={"DSWARM_ALLOW_LOCAL_WORKERS": raw},
    )
    assert policy.local_dev_env_allowed is True


@pytest.mark.parametrize("raw", ["", "0", "false", "enabled", "2"])
def test_undocumented_environment_values_do_not_enable_local_workers(raw: str):
    with pytest.raises(RuntimePolicyError, match="local_worker_policy_denied"):
        build_runtime_policy(
            mode="local_dev",
            local_dev_cli_flag=True,
            env={"DSWARM_ALLOW_LOCAL_WORKERS": raw},
        )


def test_unknown_runtime_mode_is_rejected():
    with pytest.raises(RuntimePolicyError, match="invalid_runtime_mode"):
        build_runtime_policy(mode="legacy", env={})


@pytest.mark.parametrize("value", [0.0, -1.0, float("inf"), float("-inf"), float("nan")])
def test_probe_timeout_must_be_finite_and_positive(value: float):
    with pytest.raises(RuntimePolicyError, match="invalid_probe_timeout_seconds"):
        build_runtime_policy(probe_timeout_seconds=value, env={})


@pytest.mark.parametrize("value", [0, 2, -1])
def test_v1_recovery_attempts_are_fixed_to_one(value: int):
    with pytest.raises(RuntimePolicyError, match="invalid_recovery_attempts_per_episode"):
        build_runtime_policy(recovery_attempts_per_episode=value, env={})


@pytest.mark.parametrize("value", [0, -1])
def test_explicit_pool_worker_cap_must_be_positive(value: int):
    with pytest.raises(RuntimePolicyError, match="invalid_pool_worker_cap"):
        build_runtime_policy(pool_max_concurrent_workers_default=value, env={})


def test_policy_retains_valid_explicit_limits():
    policy = build_runtime_policy(
        env={},
        max_pools_per_run=128,
        pool_max_concurrent_workers_default=7,
        probe_timeout_seconds=12.5,
    )
    assert policy.pool_max_concurrent_workers_default == 7
    assert policy.probe_timeout_seconds == 12.5
    assert policy.recovery_attempts_per_episode == 1
    assert policy.snapshot_version == 1
