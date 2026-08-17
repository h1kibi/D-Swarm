from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess



_REPO_ROOT = Path(__file__).resolve().parents[1]


def _posix_path(path: Path) -> str:
    resolved = path.resolve()
    if os.name != "nt":
        return resolved.as_posix()
    drive = resolved.drive.rstrip(":").lower()
    tail = resolved.as_posix().split(":", 1)[1]
    return f"/mnt/{drive}{tail}"


def _write_shell_shims(fake_bin: Path) -> None:
    fake_bin.mkdir(parents=True, exist_ok=True)
    for name, target in (("bash", "/bin/bash"), ("dirname", "/usr/bin/dirname")):
        shim = fake_bin / name
        shim.write_text(
            f"#!/bin/sh\nexec {target} \"$@\"\n",
            encoding="utf-8",
            newline="\n",
        )
        shim.chmod(0o755)


def _write_fake_docker(fake_bin: Path) -> None:
    _write_shell_shims(fake_bin)
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -eu
{
  printf 'CALL'
  for arg in \"$@\"; do printf '\\t%s' \"$arg\"; done
  printf '\\n'
} >> \"$DSWARM_COMMAND_LOG\"
if [ \"${1:-}\" = compose ] && [ \"${2:-}\" = version ]; then
  exit \"${DSWARM_FAKE_COMPOSE_EXIT:-0}\"
fi
if [ \"${1:-}\" = info ]; then
  exit \"${DSWARM_FAKE_DAEMON_EXIT:-0}\"
fi
if [ \"${1:-}\" = compose ] && [ \"${2:-}\" = up ]; then
  printf 'ENV\\t%s\\t%s\\t%s\\t%s\\n' \\
    \"${DSWARM_WEB_PUBLISH_HOST:-}\" \\
    \"${DSWARM_WEB_PORT:-}\" \\
    \"${DSWARM_UI_PORT:-}\" \\
    \"${DSWARM_RUNTIME_MODE:-}\" >> \"$DSWARM_COMMAND_LOG\"
  exit \"${DSWARM_FAKE_UP_EXIT:-0}\"
fi
exit 0
""",
        encoding="utf-8",
        newline="\n",
    )
    docker.chmod(0o755)


def _run_web(
    tmp_path: Path,
    *args: str,
    env: dict[str, str] | None = None,
    provide_docker: bool = True,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    fake_bin = tmp_path / "bin"
    _write_shell_shims(fake_bin)
    if provide_docker:
        _write_fake_docker(fake_bin)
    log = tmp_path / "commands.log"
    path_value = _posix_path(fake_bin)
    shell_env = {
        "PATH": path_value,
        "HOME": "/tmp/dswarm-run-sh-home",
        "DSWARM_COMMAND_LOG": _posix_path(log),
        "DSWARM_HOST_DATA_ROOT": "/tmp/dswarm-test-data",
    }
    shell_env.update(env or {})
    assignments = [
        f"{key}={shlex.quote(value)}"
        for key, value in shell_env.items()
    ]
    command = " ".join(
        [
            f"cd {shlex.quote(_posix_path(_REPO_ROOT))}",
            "&&",
            *assignments,
            "./run.sh",
            "web",
            *(shlex.quote(arg) for arg in args),
        ]
    )
    process_env = os.environ.copy()
    result = subprocess.run(
        ["bash", "-lc", command],
        cwd=_REPO_ROOT,
        env=process_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls


def test_web_defaults_to_compose(tmp_path: Path) -> None:
    result, calls = _run_web(tmp_path)

    assert result.returncode == 0, result.stderr
    assert calls[-2:] == [
        "CALL\tcompose\tup\t--build\tweb-api\tui",
        "ENV\t127.0.0.1\t8000\t3001\tdocker",
    ]
    assert "uvicorn" not in "\n".join(calls)


def test_web_backend_only_starts_only_api(tmp_path: Path) -> None:
    result, calls = _run_web(tmp_path, "--backend-only")

    assert result.returncode == 0, result.stderr
    assert "CALL\tcompose\tup\t--build\tweb-api" in calls
    assert all(not line.endswith("\tui") for line in calls if "compose\tup" in line)


def test_web_rebuild_ui_is_compatible_with_always_build_compose(tmp_path: Path) -> None:
    result, calls = _run_web(tmp_path, "--rebuild-ui")

    assert result.returncode == 0, result.stderr
    assert "CALL\tcompose\tup\t--build\tweb-api\tui" in calls


def test_web_ports_and_publish_host_become_compose_environment(tmp_path: Path) -> None:
    result, calls = _run_web(
        tmp_path,
        "--port", "8123",
        "--ui-port=3456",
        "--host", "0.0.0.0",
        env={"DSWARM_WEB_PASSWORD": "test-password"},
    )

    assert result.returncode == 0, result.stderr
    assert "ENV\t0.0.0.0\t8123\t3456\tdocker" in calls


def test_web_compose_exit_code_is_propagated(tmp_path: Path) -> None:
    result, calls = _run_web(tmp_path, env={"DSWARM_FAKE_UP_EXIT": "37"})

    assert result.returncode == 37
    assert "CALL\tcompose\tup\t--build\tweb-api\tui" in calls
    assert "exec docker compose up" in (_REPO_ROOT / "run.sh").read_text(encoding="utf-8")


def test_web_rejects_missing_docker_before_host_backend(tmp_path: Path) -> None:
    result, calls = _run_web(tmp_path, provide_docker=False)

    assert result.returncode != 0
    assert "docker_unavailable" in result.stderr
    assert calls == []
    assert "uvicorn" not in result.stderr


def test_web_rejects_missing_compose_plugin(tmp_path: Path) -> None:
    result, calls = _run_web(
        tmp_path, env={"DSWARM_FAKE_COMPOSE_EXIT": "1"}
    )

    assert result.returncode != 0
    assert "docker_compose_unavailable" in result.stderr
    assert "CALL\tcompose\tversion" in calls
    assert all("compose\tup" not in line for line in calls)


def test_web_rejects_unavailable_docker_daemon(tmp_path: Path) -> None:
    result, calls = _run_web(
        tmp_path, env={"DSWARM_FAKE_DAEMON_EXIT": "1"}
    )

    assert result.returncode != 0
    assert "docker_daemon_unavailable" in result.stderr
    assert "CALL\tinfo" in calls
    assert all("compose\tup" not in line for line in calls)


def test_local_dev_requires_cli_and_environment_gate_before_uv(tmp_path: Path) -> None:
    result, calls = _run_web(tmp_path, "--local-dev")

    assert result.returncode != 0
    assert "local_worker_policy_denied" in result.stderr
    assert calls == []
    assert "uvicorn" not in result.stderr


def test_compose_publishes_only_operator_ports_on_loopback() -> None:
    compose = (_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert (
        '"${DSWARM_WEB_PUBLISH_HOST:-127.0.0.1}:'
        '${DSWARM_WEB_PORT:-8000}:8000"'
    ) in compose
    assert (
        '"${DSWARM_WEB_PUBLISH_HOST:-127.0.0.1}:'
        '${DSWARM_UI_PORT:-3001}:3001"'
    ) in compose
    assert "9100:" not in compose
    assert "9101:" not in compose


def test_compose_socket_and_data_mount_are_control_plane_only() -> None:
    compose = (_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert compose.count("/var/run/docker.sock:/var/run/docker.sock") == 1
    assert (
        "${DSWARM_HOST_DATA_ROOT:?set DSWARM_HOST_DATA_ROOT to an absolute host path}:"
        "${DSWARM_HOST_DATA_ROOT}"
    ) in compose
    assert 'DSWARM_RUNTIME_MODE: "docker"' in compose
