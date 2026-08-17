from dataclasses import replace
import json

import pytest
from dswarm.solver.runtime_policy import (
    PoolSpec,
    RuntimeNetworkSpec,
    RuntimePolicyError,
    RuntimeResourceSpec,
    canonical_pool_payload,
    pool_id_for_spec,
    build_runtime_policy,
)


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



def pool_spec(**changes):
    base = PoolSpec(
        pool_id="",
        profile_id="pi-web",
        runtime_kind="pi",
        resolved_image_id="sha256:abc",
        requested_image_ref="ctf-swarm-pi-web:0.2.0",
        network=RuntimeNetworkSpec(kind="named", name="dswarm_net"),
        resources=RuntimeResourceSpec(
            cpus="2", memory="2g", pids_limit=256, tmpfs_bytes=67108864
        ),
        credential_binding_id="pi-web-main",
        provider_binding_id="deepseek",
        model="deepseek-chat",
        uid=1000,
        gid=1000,
        runtime_features=("rcp-v2", "tool-disabled-probe"),
        protocol_version=2,
        pool_max_concurrent_workers=8,
    )
    return replace(base, **changes)


def test_pool_key_is_stable_and_excludes_secret_version_and_generation():
    first = pool_id_for_spec(pool_spec())
    assert first.startswith("pool-v1::")
    assert first == pool_id_for_spec(pool_spec())
    assert len(first.removeprefix("pool-v1::")) == 40


def test_binding_identity_changes_pool_key_but_secret_version_is_not_a_pool_field():
    assert pool_id_for_spec(pool_spec()) != pool_id_for_spec(
        pool_spec(credential_binding_id="pi-web-secondary")
    )
    assert "credential_version" not in PoolSpec.__dataclass_fields__
    assert pool_id_for_spec(pool_spec()) == pool_id_for_spec(
        pool_spec(requested_image_ref="another-tag:latest")
    )


def test_named_network_requires_name_and_unknown_dict_fields_are_rejected():
    with pytest.raises(RuntimePolicyError, match="invalid_network"):
        RuntimeNetworkSpec(kind="named", name="")
    with pytest.raises(TypeError):
        PoolSpec(**{**pool_spec().__dict__, "raw_secret": "x"})


def test_runtime_features_are_normalized_before_hashing():
    unordered = pool_spec(runtime_features=("tool-disabled-probe", "rcp-v2", "rcp-v2"))
    assert unordered.runtime_features == ("rcp-v2", "tool-disabled-probe")
    assert pool_id_for_spec(unordered) == pool_id_for_spec(pool_spec())


def test_pool_spec_computed_id_is_frozen_and_mismatch_is_rejected():
    computed = PoolSpec.with_computed_id(**{k: v for k, v in pool_spec().__dict__.items() if k != "pool_id"})
    assert computed.pool_id == pool_id_for_spec(computed)
    with pytest.raises(RuntimePolicyError, match="pool_id_mismatch"):
        replace(computed, pool_id="pool-v1::" + "0" * 40)


def test_canonical_pool_payload_has_an_exact_secret_free_allowlist():
    payload = json.loads(canonical_pool_payload(pool_spec()))
    assert set(payload) == {
        "credential_binding_id",
        "gid",
        "model",
        "network",
        "pool_max_concurrent_workers",
        "profile_id",
        "protocol_version",
        "provider_binding_id",
        "resolved_image_id",
        "resources",
        "runtime_features",
        "runtime_kind",
        "uid",
    }
    assert "pool_id" not in payload
    assert b"secret" not in canonical_pool_payload(pool_spec()).lower()


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("profile_id", "bad profile", "invalid_profile_id"),
        ("credential_binding_id", "../secret", "invalid_credential_binding_id"),
        ("provider_binding_id", "", "invalid_provider_binding_id"),
        ("model", "model\nname", "invalid_model"),
        ("runtime_kind", "legacy", "invalid_runtime_kind"),
        ("uid", -1, "invalid_uid"),
        ("gid", -1, "invalid_gid"),
        ("protocol_version", 0, "invalid_protocol_version"),
        ("pool_max_concurrent_workers", 0, "invalid_pool_worker_cap"),
    ],
)
def test_pool_spec_rejects_invalid_identity_and_limits(field, value, error):
    with pytest.raises(RuntimePolicyError, match=error):
        pool_spec(**{field: value})


def test_resource_spec_rejects_non_finite_or_non_positive_limits():
    with pytest.raises(RuntimePolicyError, match="invalid_cpus"):
        RuntimeResourceSpec(cpus="nan", memory="2g", pids_limit=2, tmpfs_bytes=1)
    with pytest.raises(RuntimePolicyError, match="invalid_memory"):
        RuntimeResourceSpec(cpus="1", memory="0", pids_limit=2, tmpfs_bytes=1)
    with pytest.raises(RuntimePolicyError, match="invalid_pids_limit"):
        RuntimeResourceSpec(cpus="1", memory="2g", pids_limit=0, tmpfs_bytes=1)
