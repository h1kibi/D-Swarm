"""Core container plumbing probe for credential accounts.

The web account-test endpoint and the profile-health kernel both need to verify
that a projected credential is readable inside the worker image and that the
worker CLI can launch, without spending model quota.  Keeping that docker-run
implementation here avoids a ``dswarm`` -> ``apps`` dependency.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from dswarm.solver.credential_accounts import (
    CONTAINER_ACCOUNTS_ROOT,
    project_account_root,
)
from dswarm.solver.docker import docker_run

# in-container worker binary per engine — mirrors container_exec._CONTAINER_BIN.
_CONTAINER_BIN = {
    "pi": "pi",
}


def _result(ok: bool, detail: str, layer: Optional[str] = None) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": ok, "detail": detail}
    if layer:
        out["layer"] = layer
    return out


_docker = docker_run

# Cold container starts on Windows/WSL2 with a full-Kali image can outrun a
# fixed 60s wall (disk IO races when several profiles probe at once), which
# surfaced as rotating "容器探测超时（>60s）" profile_unhealthy failures during
# the startup smoke. Env-tunable so operators on slow disks can widen it.
PROBE_TIMEOUT_S = float(os.environ.get("DSWARM_CONTAINER_PROBE_TIMEOUT", "60"))


def _probe_container(*, engine: str, account_id: str, root: Path) -> dict[str, Any]:
    """Real one-shot ``docker run --rm`` test of the container plumbing.

    Mounts only the account projection (never the bench tree) + a throwaway empty
    workspace, then runs the engine's in-container liveness probe. Layers:
      image  -> worker image missing / docker unavailable
      mount  -> container uid can't read the projected credential
      cli    -> engine binary won't launch in the container
    """
    from dswarm.solver.container_exec import (
        WORKER_IMAGE,
        _CATEGORY_IMAGES,
        CONTAINER_WORKSPACE,
        _HOST_DATA_ROOT,
        _mount_source,
    )

    image = WORKER_IMAGE
    try:
        for cand in _CATEGORY_IMAGES.values():
            r = _docker("image", "inspect", cand, timeout=20)
            if r.returncode == 0:
                image = cand
                break
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    try:
        r = _docker("image", "inspect", image, timeout=20)
    except FileNotFoundError:
        return _result(False, "docker 不可用（未安装或 daemon 未运行）", layer="image")
    except subprocess.TimeoutExpired:
        return _result(False, "docker image inspect 超时", layer="image")
    if r.returncode != 0:
        return _result(False, f"镜像缺失或不可用: {image}", layer="image")

    _tmp_base = None
    if _HOST_DATA_ROOT:
        _tmp_base = os.path.join(os.environ.get("DSWARM_CONTAINER_DATA_ROOT") or _HOST_DATA_ROOT,
                                 "_tmp", "account-tests")
        try:
            os.makedirs(_tmp_base, exist_ok=True)
        except OSError:
            _tmp_base = None
    with tempfile.TemporaryDirectory(prefix="dswarm-acct-test-", dir=_tmp_base) as td:
        workspace = os.path.join(td, "ws")
        projection = os.path.join(td, "accounts")
        os.makedirs(workspace, exist_ok=True)
        try:
            project_account_root(root, projection)
        except OSError as exc:
            return _result(False, f"凭据投影失败: {str(exc)[:120]}", layer="mount")

        bin_path = _CONTAINER_BIN.get(engine, engine)
        cred_path = f"{CONTAINER_ACCOUNTS_ROOT}/{account_id}"
        script = (
            f"test -r {cred_path} || {{ echo DSWARM_MOUNT_UNREADABLE; exit 71; }}; "
            f"{bin_path} --version >/dev/null 2>&1 || {{ echo DSWARM_CLI_FAIL; exit 72; }}; "
            "echo DSWARM_OK"
        )
        run_cmd = [
            "run", "--rm", "--init",
            "--network", "none",
            "--entrypoint", "bash",
            "--mount",
            f"type=bind,source={_mount_source(workspace)},target={CONTAINER_WORKSPACE}",
            "--mount",
            f"type=bind,source={_mount_source(projection)},target={CONTAINER_ACCOUNTS_ROOT}",
            image, "-lc", script,
        ]
        try:
            run = _docker(*run_cmd, timeout=PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return _result(False, f"容器探测超时（>{PROBE_TIMEOUT_S:.0f}s，可调 DSWARM_CONTAINER_PROBE_TIMEOUT）", layer="cli")
        out = (run.stdout or "") + (run.stderr or "")
        if "DSWARM_MOUNT_UNREADABLE" in out or run.returncode == 71:
            return _result(False, "容器内无法读取凭据（uid 不匹配或挂载失败）", layer="mount")
        if "DSWARM_CLI_FAIL" in out or run.returncode == 72:
            return _result(False, f"容器内 {engine} CLI 无法启动", layer="cli")
        if run.returncode != 0:
            return _result(False, f"容器探测失败: {out.strip()[:160]}", layer="cli")
        return _result(True, "容器内凭据可读、CLI 可启动（已验证镜像+挂载+HOME隔离）")
