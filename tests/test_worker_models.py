from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

from apps.web.worker_models import (
    WORKER_MODEL_OPTIONS,
    probe_worker_model,
    worker_model_options_payload,
)


def test_worker_model_options_are_static_and_custom_enabled() -> None:
    payload = worker_model_options_payload()

    assert payload["allow_custom"] is True
    assert {m["id"] for m in payload["models"]["pi"]} >= {
        "deepseek-v4-flash", "deepseek-v4-pro"}
    assert payload["models"] == WORKER_MODEL_OPTIONS


def test_probe_worker_model_injects_profile_model_and_account_env(tmp_path, monkeypatch) -> None:
    root = tmp_path / "_secrets" / "accounts" / "pi-main"
    root.mkdir(parents=True)
    (root / "API_KEY").write_text("deepseek-secret\n")
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["key"] = os.environ.get("DEEPSEEK_API_KEY")
        return subprocess.CompletedProcess(argv, 0, '{"type":"agent_settled"}\n', "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("MUTEKI_PI_PROVIDER", "deepseek")

    res = probe_worker_model(
        profile={
            "id": "pi-sub",
            "name": "pi-sub",
            "engine": "pi",
            "transport": "pi_cli",
            "credential_account": "pi-main",
            "runtime": "local",
        },
        model="deepseek-v4-pro",
        sessions_root=tmp_path,
        backend="local",
    )

    assert res["ok"] is True
    assert res["model"] == "deepseek-v4-pro"
    assert seen["key"] == "deepseek-secret"
    assert "--model" in seen["argv"]
    assert seen["argv"][seen["argv"].index("--model") + 1] == "deepseek-v4-pro"


def test_probe_worker_model_allows_local_system_login_without_registered_account(
    tmp_path, monkeypatch
) -> None:
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, '{"type":"agent_settled"}\n', "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    res = probe_worker_model(
        profile={
            "id": "pi-local",
            "engine": "pi",
            "transport": "pi_cli",
            "credential_account": "",
            "runtime": "local",
        },
        model="deepseek-v4-flash",
        sessions_root=tmp_path,
        backend="local",
    )

    assert res["ok"] is True
    assert "--model" in seen["argv"]


def test_probe_worker_model_does_not_default_local_pi_to_stale_account(
    tmp_path, monkeypatch
) -> None:
    # a host-installed pi (HOME ~/.pi sessions in the environment) must not leak
    # into a LOCAL probe that should use the run's own accounts — the probe's env
    # is exactly the account overlay, nothing from the host pi install.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["key"] = os.environ.get("DEEPSEEK_API_KEY")
        return subprocess.CompletedProcess(argv, 0, '{"type":"agent_settled"}\n', "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    res = probe_worker_model(
        profile={
            "id": "pi-local",
            "engine": "pi",
            "transport": "pi_cli",
            "credential_account": "",
            "runtime": "local",
        },
        model="deepseek-v4-flash",
        sessions_root=tmp_path,
        backend="local",
    )

    assert res["ok"] is True
    assert seen["key"] is None  # no stale host credential leaked in
    assert "--model" in seen["argv"]
    assert seen["argv"][seen["argv"].index("--model") + 1] == "deepseek-v4-flash"


def test_probe_worker_model_runs_real_worker_container_when_web_is_containerized(
    tmp_path, monkeypatch
) -> None:
    # In a compose deploy the WEB process runs inside a container that does NOT
    # ship the pi CLI — it lives only in the WORKER image. A container-backend
    # model probe must therefore run a real one-shot worker container and
    # complete the same minimal hello turn there.
    monkeypatch.setenv("MUTEKI_IN_CONTAINER", "1")
    root = tmp_path / "_secrets" / "accounts" / "pi-main"
    root.mkdir(parents=True)
    (root / "API_KEY").write_text("deepseek-secret\n")
    seen: dict[str, object] = {}

    def fake_docker(*args, timeout=30):
        if args[:2] == ("image", "inspect"):
            return subprocess.CompletedProcess(["docker", *args], 0, "", "")
        if args and args[0] == "run":
            seen["run_args"] = args
            return subprocess.CompletedProcess(
                ["docker", *args],
                0,
                '{"type":"agent_settled"}\n',
                "",
            )
        raise AssertionError(f"unexpected docker command: {args}")

    import apps.web.worker_models as worker_models
    monkeypatch.setattr(worker_models, "_docker", fake_docker)

    res = probe_worker_model(
        profile={
            "id": "pi-seat",
            "engine": "pi",
            "transport": "pi_cli",
            "credential_mode": "api_key",
            "credential_account": "pi-main",
            "runtime": "container",
        },
        model="deepseek-v4-flash",
        sessions_root=tmp_path,
        backend="container",
    )

    assert res["ok"] is True
    assert res["backend"] == "container"
    assert res["engine"] == "pi"
    assert res["model"] == "deepseek-v4-flash"
    run_args = seen["run_args"]
    assert run_args[0] == "run"
    assert "--entrypoint" in run_args and "bash" in run_args
    assert "--user" in run_args and "kali" in run_args
    assert any(str(a).startswith("type=bind") and "/run/muteki/accounts" in str(a) for a in run_args)
    assert "deepseek-v4-flash" in " ".join(str(a) for a in run_args)


def test_probe_worker_model_container_maps_provider_key_and_base_url(
    tmp_path, monkeypatch
) -> None:
    # A custom-endpoint pi account must reach the worker container as the
    # provider key + base URL env (pi reads standard provider keys, not a CLI
    # config seed).
    monkeypatch.setenv("MUTEKI_IN_CONTAINER", "1")
    root = tmp_path / "_secrets" / "accounts" / "pi-main"
    root.mkdir(parents=True)
    (root / "API_KEY").write_text("deepseek-secret\n")
    (root / "BASE_URL").write_text("https://api.deepseek.example/v1\n")
    seen: dict[str, object] = {}

    def fake_docker(*args, timeout=30):
        if args[:2] == ("image", "inspect"):
            return subprocess.CompletedProcess(["docker", *args], 0, "", "")
        if args and args[0] == "run":
            seen["run_args"] = args
            return subprocess.CompletedProcess(
                ["docker", *args],
                0,
                '{"type":"agent_settled"}\n',
                "",
            )
        raise AssertionError(f"unexpected docker command: {args}")

    import apps.web.worker_models as worker_models
    monkeypatch.setattr(worker_models, "_docker", fake_docker)

    res = probe_worker_model(
        profile={
            "id": "pi-seat",
            "engine": "pi",
            "transport": "pi_cli",
            "credential_account": "pi-main",
            "runtime": "container",
        },
        model="deepseek-v4-flash",
        sessions_root=tmp_path,
        backend="container",
    )

    assert res["ok"] is True
    joined = " ".join(str(a) for a in seen["run_args"])
    assert "ANTHROPIC_BASE_URL=https://api.deepseek.example/v1" in joined
    assert "ANTHROPIC_API_KEY_FILE=/run/muteki/accounts/pi-main/API_KEY" in joined


def test_probe_worker_model_container_reports_model_rejection(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("MUTEKI_IN_CONTAINER", "1")
    root = tmp_path / "_secrets" / "accounts" / "pi-main"
    root.mkdir(parents=True)
    (root / "API_KEY").write_text("deepseek-secret\n")

    def fake_docker(*args, timeout=30):
        if args[:2] == ("image", "inspect"):
            return subprocess.CompletedProcess(["docker", *args], 0, "", "")
        if args and args[0] == "run":
            return subprocess.CompletedProcess(
                ["docker", *args],
                1,
                "",
                "unknown model: deepseek-v4-nope\n",
            )
        raise AssertionError(f"unexpected docker command: {args}")

    import apps.web.worker_models as worker_models
    monkeypatch.setattr(worker_models, "_docker", fake_docker)

    res = probe_worker_model(
        profile={
            "id": "pi-seat",
            "engine": "pi",
            "transport": "pi_cli",
            "credential_account": "pi-main",
            "runtime": "container",
        },
        model="deepseek-v4-nope",
        sessions_root=tmp_path,
        backend="container",
    )

    assert res["ok"] is False
    assert res["backend"] == "container"
    assert res["layer"] == "auth"
    assert "unknown model" in res["detail"]


def test_probe_worker_model_container_pi_endpoint_uses_custom_model(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("MUTEKI_IN_CONTAINER", "1")
    root = tmp_path / "_secrets" / "accounts" / "pi-main"
    root.mkdir(parents=True)
    (root / "API_KEY").write_text("deepseek-key\n")
    (root / "BASE_URL").write_text("https://api.deepseek.example/anthropic\n")
    seen: dict[str, object] = {}

    def fake_docker(*args, timeout=30):
        if args[:2] == ("image", "inspect"):
            return subprocess.CompletedProcess(["docker", *args], 0, "", "")
        if args and args[0] == "run":
            seen["run_args"] = args
            return subprocess.CompletedProcess(["docker", *args], 0, '{"type":"agent_settled"}\n', "")
        raise AssertionError(f"unexpected docker command: {args}")

    import apps.web.worker_models as worker_models
    monkeypatch.setattr(worker_models, "_docker", fake_docker)

    res = probe_worker_model(
        profile={
            "id": "pi-ds",
            "engine": "pi",
            "transport": "pi_cli",
            "credential_account": "pi-main",
            "credential_mode": "api_key",
            "base_url": "https://api.deepseek.example/anthropic",
            "runtime": "container",
        },
        model="deepseek-v4-pro",
        sessions_root=tmp_path,
        backend="container",
    )

    assert res["ok"] is True
    joined = " ".join(str(a) for a in seen["run_args"])
    assert "--model deepseek-v4-pro" in joined
    assert "ANTHROPIC_BASE_URL=https://api.deepseek.example/anthropic" in joined


def test_probe_worker_model_still_probes_host_for_local_backend_in_container(
    tmp_path, monkeypatch
) -> None:
    # The defer is gated on backend == "container". An explicit local-backend
    # probe (operator chose host semantics) must still shell the host CLI even
    # if MUTEKI_IN_CONTAINER happens to be set — the guard must not over-reach.
    monkeypatch.setenv("MUTEKI_IN_CONTAINER", "1")
    root = tmp_path / "_secrets" / "accounts" / "pi-main"
    root.mkdir(parents=True)
    (root / "API_KEY").write_text("deepseek-secret\n")
    ran: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        ran["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, '{"type":"agent_settled"}\n', "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    res = probe_worker_model(
        profile={
            "id": "pi-sub",
            "engine": "pi",
            "transport": "pi_cli",
            "credential_account": "pi-main",
            "runtime": "local",
        },
        model="deepseek-v4-pro",
        sessions_root=tmp_path,
        backend="local",
    )

    assert res["ok"] is True
    assert ran.get("argv") is not None  # host probe DID run
