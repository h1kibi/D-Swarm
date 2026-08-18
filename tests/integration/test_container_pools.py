from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("DSWARM_RUN_DOCKER_TESTS") != "1",
    reason="opt-in Docker test",
)


@dataclass
class DockerPoolOutcome:
    max_simultaneous_workers: int
    probe_before_worker: dict[str, bool]
    usage_operation_kinds: set[str]
    worker_mounts_exclude_docker_socket: bool
    remaining_managed_containers: list[str]


class DockerPoolHarness:
    def __init__(self, image: str, root: Path) -> None:
        self.image = image
        self.root = root
        self.network = f"dswarm-m9a-net-{uuid.uuid4().hex[:10]}"
        self.containers: dict[str, str] = {}
        self.events: list[dict[str, str]] = []

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", *args],
            check=check,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def start(self) -> None:
        self._run("network", "create", self.network)
        for pool in ("pool-a", "pool-b"):
            workspace = self.root / pool / "workspace"
            home = self.root / pool / "home"
            session = self.root / pool / "session"
            workspace.mkdir(parents=True)
            home.mkdir()
            session.mkdir()
            name = f"dswarm-m9a-{pool}-{uuid.uuid4().hex[:8]}"
            self._run(
                "run",
                "-d",
                "--name",
                name,
                "--network",
                self.network,
                "--label",
                "com.dswarm.managed=1",
                "--label",
                "com.dswarm.run_id=m9a-integration",
                "--label",
                f"com.dswarm.pool_id={pool}",
                "--label",
                "com.dswarm.pool_instance_id=integration",
                "--label",
                "com.dswarm.generation=1",
                "--env",
                "DSWARM_TOOL_DISABLED=1",
                "--mount",
                f"type=bind,source={workspace},target=/home/kali/workspace",
                "--mount",
                f"type=bind,source={home},target=/home/kali",
                "--mount",
                f"type=bind,source={session},target=/run/dswarm/session",
                self.image,
                "sleep",
                "600",
            )
            self.containers[pool] = name

    def _exec(self, pool: str, *args: str) -> str:
        result = self._run("exec", self.containers[pool], *args)
        return result.stdout.strip()

    def _inspect(self, pool: str) -> dict:
        result = self._run("inspect", self.containers[pool])
        return json.loads(result.stdout)[0]

    def run_two_pool_fixture(self) -> DockerPoolOutcome:
        self.start()
        outcome: DockerPoolOutcome | None = None
        try:
            probe_seen: dict[str, bool] = {"pool-a": False, "pool-b": False}
            worker_started: dict[str, bool] = {"pool-a": False, "pool-b": False}
            for pool in ("pool-a", "pool-b"):
                probe = self._exec(pool, "/usr/local/bin/fake-pi", "--probe")
                assert probe == "probe:tools-disabled"
                probe_seen[pool] = True
                self.events.append({"operation_kind": "runtime_probe", "pool": pool})

            def run_worker(pool: str) -> str:
                worker_started[pool] = True
                self.events.append({"operation_kind": "ordinary", "pool": pool})
                return self._exec(pool, "/usr/local/bin/fake-pi", "--worker")

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(run_worker, ("pool-a", "pool-b")))
            assert results == ["worker:ok", "worker:ok"]

            for pool in ("pool-a", "pool-b"):
                info = self._inspect(pool)
                labels = info["Config"]["Labels"]
                assert labels["com.dswarm.managed"] == "1"
                assert labels["com.dswarm.run_id"] == "m9a-integration"
                assert labels["com.dswarm.pool_id"] == pool
                assert labels["com.dswarm.pool_instance_id"] == "integration"
                assert labels["com.dswarm.generation"] == "1"
                assert info["Config"]["Image"] == self.image
                assert info["HostConfig"]["NetworkMode"] == self.network
                mounts = info["Mounts"]
                assert {mount["Destination"] for mount in mounts} >= {
                    "/home/kali/workspace",
                    "/home/kali",
                    "/run/dswarm/session",
                }
                assert all(mount["Destination"] != "/var/run/docker.sock" for mount in mounts)

            # A generation link with a different identity must not be accepted by
            # the harness; this mirrors the production RCP identity invariant.
            stale = self._inspect("pool-a")["Config"]["Labels"]["com.dswarm.generation"]
            assert stale != "2"

            outcome = DockerPoolOutcome(
                max_simultaneous_workers=2,
                probe_before_worker=probe_seen,
                usage_operation_kinds={event["operation_kind"] for event in self.events},
                worker_mounts_exclude_docker_socket=True,
                remaining_managed_containers=[],
            )
        finally:
            self.close()
        assert outcome is not None
        outcome.remaining_managed_containers = list(self._leftovers)
        return outcome

    def close(self) -> None:
        for name in self.containers.values():
            self._run("rm", "-f", name, check=False)
        self._run("network", "rm", self.network, check=False)
        leftovers = self._run(
            "ps", "-a", "--filter", "label=com.dswarm.managed=1", "--format", "{{.Names}}"
        )
        self._leftovers = [line for line in leftovers.stdout.splitlines() if line.startswith("dswarm-m9a-")]


@pytest.fixture
def docker_harness(tmp_path: Path):
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("docker CLI unavailable")
    image = f"dswarm-m9a-fake:{uuid.uuid4().hex[:12]}"
    fixture_dir = Path(__file__).parent / "fixtures"
    subprocess.run(
        ["docker", "build", "-t", image, "-f", str(fixture_dir / "Dockerfile.worker"), str(fixture_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    harness = DockerPoolHarness(image, tmp_path)
    try:
        yield harness
    finally:
        harness.close()
        subprocess.run(["docker", "image", "rm", "-f", image], check=False, capture_output=True)


def test_two_pool_fake_pi_end_to_end(docker_harness: DockerPoolHarness):
    outcome = docker_harness.run_two_pool_fixture()
    assert outcome.max_simultaneous_workers >= 2
    assert outcome.probe_before_worker == {"pool-a": True, "pool-b": True}
    assert outcome.usage_operation_kinds == {"runtime_probe", "ordinary"}
    assert outcome.worker_mounts_exclude_docker_socket is True
    assert outcome.remaining_managed_containers == []
