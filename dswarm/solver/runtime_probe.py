from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dswarm.core.usage_journal import UsageContext
from dswarm.solver.cli_driver import PiDriver
from dswarm.solver.container_pool import RuntimeFailure, RuntimeProbeResult
from dswarm.solver.runtime_policy import PoolSpec


PROBE_CONTRACT_VERSION = "m9-runtime-probe-v1"


class RuntimeProbeError(RuntimeError):
    """A preflight or accounting error that prevents a runtime probe."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class RuntimeProbeCacheKey:
    pool_id: str
    pool_instance_id: str
    generation: int
    resolved_image_id: str
    model: str
    provider_binding_id: str
    credential_binding_id: str
    credential_version_digest: str
    probe_contract_version: str = PROBE_CONTRACT_VERSION

    def identity(self) -> str:
        payload = json.dumps(
            {
                "pool_id": self.pool_id,
                "pool_instance_id": self.pool_instance_id,
                "generation": self.generation,
                "resolved_image_id": self.resolved_image_id,
                "model": self.model,
                "provider_binding_id": self.provider_binding_id,
                "credential_binding_id": self.credential_binding_id,
                "credential_version_digest": self.credential_version_digest,
                "probe_contract_version": self.probe_contract_version,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class ProbeFailureClass:
    AUTH = "auth"
    CONFIGURATION = "configuration"
    INFRASTRUCTURE = "infrastructure"
    TIMEOUT = "timeout"
    PROTOCOL = "protocol"


class RuntimeProbe:
    """Accounted, challenge-free readiness probe for a long-lived pool.

    A successful result is cached per pool generation and credential/image
    identity.  Failures are deliberately never cached: recovery may replace a
    broken container generation and must get a fresh probe.
    """

    def __init__(self, *, usage_writer: Any, budget_gate: Any) -> None:
        self.usage_writer = usage_writer
        self.budget_gate = budget_gate
        self._cache: dict[RuntimeProbeCacheKey, RuntimeProbeResult] = {}
        self._inflight: dict[RuntimeProbeCacheKey, asyncio.Task[RuntimeProbeResult]] = {}
        self._lock = asyncio.Lock()

    async def run(
        self,
        *,
        executor: Any,
        pool_spec: PoolSpec,
        credential_projection: Any,
        generation: int,
        timeout: float,
    ) -> RuntimeProbeResult:
        pool_instance_id = str(getattr(executor, "pool_instance_id", ""))
        credential_binding_id = str(
            getattr(credential_projection, "binding_id", "")
            or pool_spec.credential_binding_id
        )
        credential_version_digest = str(
            getattr(credential_projection, "credential_version_digest", "") or ""
        )
        key = RuntimeProbeCacheKey(
            pool_id=pool_spec.pool_id,
            pool_instance_id=pool_instance_id,
            generation=generation,
            resolved_image_id=pool_spec.resolved_image_id,
            model=pool_spec.model,
            provider_binding_id=pool_spec.provider_binding_id,
            credential_binding_id=credential_binding_id,
            credential_version_digest=credential_version_digest,
        )

        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(
                    self._run_singleflight(
                        key=key,
                        executor=executor,
                        pool_spec=pool_spec,
                        credential_projection=credential_projection,
                        generation=generation,
                        timeout=timeout,
                    ),
                    name=f"runtime-probe:{pool_spec.pool_id}:{generation}",
                )
                self._inflight[key] = task

        # A waiter cannot cancel the shared paid probe by cancelling its own wait.
        return await asyncio.shield(task)

    async def _run_singleflight(
        self,
        *,
        key: RuntimeProbeCacheKey,
        executor: Any,
        pool_spec: PoolSpec,
        credential_projection: Any,
        generation: int,
        timeout: float,
    ) -> RuntimeProbeResult:
        try:
            result = await self._probe_once_or_recover(
                key=key,
                executor=executor,
                pool_spec=pool_spec,
                credential_projection=credential_projection,
                generation=generation,
                timeout=timeout,
            )
            if result.ready:
                async with self._lock:
                    self._cache[key] = result
            return result
        finally:
            async with self._lock:
                current = self._inflight.get(key)
                if current is asyncio.current_task():
                    self._inflight.pop(key, None)

    async def _probe_once_or_recover(
        self,
        *,
        key: RuntimeProbeCacheKey,
        executor: Any,
        pool_spec: PoolSpec,
        credential_projection: Any,
        generation: int,
        timeout: float,
    ) -> RuntimeProbeResult:
        probe_id = str(uuid.uuid4())
        worker_instance_id = f"probe-{uuid.uuid4()}"
        last_failure: RuntimeFailure | None = None

        for attempt in range(2):
            call = await self._start_accounting(
                pool_spec=pool_spec,
                run_id=str(getattr(executor, "run_id", "")),
                worker_instance_id=worker_instance_id,
                probe_id=probe_id,
                attempt=attempt,
            )
            try:
                result = await self._execute(
                    executor=executor,
                    pool_spec=pool_spec,
                    credential_projection=credential_projection,
                    worker_instance_id=worker_instance_id,
                    probe_id=probe_id,
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                await self._finish(call, call_outcome="timeout", usage_status="unknown")
                last_failure = RuntimeFailure("infrastructure", "timeout")
            except asyncio.CancelledError:
                await self._finish(call, call_outcome="cancelled", usage_status="unknown")
                raise
            except Exception as exc:  # noqa: BLE001 - classify sanitized exception text
                failure = self._classify_exception(exc)
                await self._finish(call, call_outcome=self._outcome_for(failure), usage_status="unknown")
                last_failure = failure
            else:
                failure, usage_status, usage = self._classify_result(result)
                await self._finish(
                    call,
                    call_outcome="succeeded" if failure is None else self._outcome_for(failure),
                    usage_status=usage_status,
                    usage=usage,
                )
                if failure is None:
                    return RuntimeProbeResult(
                        ready=True,
                        probe_id=probe_id,
                        failure=None,
                        cache_identity=key.identity(),
                    )
                last_failure = failure

            if last_failure is None or last_failure.category != "infrastructure":
                break

        assert last_failure is not None
        return RuntimeProbeResult(
            ready=False,
            probe_id=probe_id,
            failure=last_failure,
            cache_identity="",
        )

    async def _start_accounting(
        self, *, pool_spec: PoolSpec, run_id: str, worker_instance_id: str, probe_id: str, attempt: int
    ) -> Any:
        verdict = self.budget_gate.authorize(
            profile_id=pool_spec.profile_id,
            account_id=pool_spec.provider_binding_id,
        )
        if hasattr(verdict, "__await__"):
            verdict = await verdict
        if not getattr(verdict, "allowed", False):
            raise RuntimeProbeError(str(getattr(verdict, "reason", None) or "budget_denied"))

        context = UsageContext(
            run_id=run_id or "runtime-probe",
            worker_instance_id=worker_instance_id,
            solver_id="runtime-probe",
            profile_id=pool_spec.profile_id,
            configured_account_id=pool_spec.provider_binding_id,
            billing_account_id=pool_spec.provider_binding_id,
            operation_kind="runtime_probe",
        )
        try:
            return await self.usage_writer.start(
                context=context,
                provider_call_id=f"probe::{probe_id}::attempt-{attempt}",
            )
        except RuntimeProbeError:
            raise
        except Exception as exc:  # noqa: BLE001 - do not leak journal details
            raise RuntimeProbeError("accounting_unavailable") from exc

    async def _finish(
        self,
        call: Any,
        *,
        call_outcome: str,
        usage_status: str,
        usage: dict[str, Any] | None = None,
    ) -> None:
        try:
            await self.usage_writer.finish(
                call,
                call_outcome=call_outcome,
                usage_status=usage_status,
                usage=usage or {},
            )
        except Exception as exc:  # noqa: BLE001 - terminal accounting is fail-closed
            raise RuntimeProbeError("accounting_unavailable") from exc

    async def _execute(
        self,
        *,
        executor: Any,
        pool_spec: PoolSpec,
        credential_projection: Any,
        worker_instance_id: str,
        probe_id: str,
        timeout: float,
    ) -> Any:
        root = Path(getattr(executor, "run_root", Path.cwd()))
        host_cwd = root / "workspace" / "probe" / probe_id
        host_cwd.mkdir(parents=True, exist_ok=True)
        session_dir = f"/home/kali/workspace/probe/{probe_id}/sessions"
        driver = PiDriver()
        spec = driver.probe_spec(model=pool_spec.model, session_dir=session_dir)
        env = dict(getattr(credential_projection, "env", {}) or {})
        # The runtime agent's baseEnv HOME (/home/kali) has NO pi provider
        # config — only the image's own user home and the run-materialized
        # workspace homes do. Without an explicit HOME the probe hello ran the
        # bare CLI (no ctf-gateway/dswarm-worker provider) and failed with the
        # same "Unknown provider / model not found" symptom as a mis-bound
        # credential. Materialize a probe HOME exactly like the worker spawn
        # path does (worker_runtime_mixin), inside the bind-mounted workspace.
        try:
            from dswarm.solver.container_exec import (
                CONTAINER_WORKSPACE,
                _chown_tree_to_worker,
            )
            from dswarm.swarm._bootstrap_assets import (
                _ensure_pi_config_links,
                _materialize_runtime_pi_config,
            )

            probe_label = f"probe-{probe_id}"
            workspace_root = root / "workspace"
            probe_home = workspace_root / "homes" / probe_label
            probe_home.mkdir(parents=True, exist_ok=True)
            runtime_config = _materialize_runtime_pi_config(workspace_root)
            _ensure_pi_config_links(
                probe_home,
                config_target_root=f"{CONTAINER_WORKSPACE}/.dswarm_runtime/pi-config",
                copy_source=runtime_config,
            )
            _chown_tree_to_worker(str(probe_home))
            env["HOME"] = f"{CONTAINER_WORKSPACE}/homes/{probe_label}"
            env["PI_CODING_AGENT_DIR"] = f'{env["HOME"]}/.pi/agent'
        except Exception:  # noqa: BLE001 - HOME prep is best-effort preflight
            pass
        if getattr(credential_projection, "credential_mode", "") == "gateway":
            # The readiness probe runs BEFORE any worker spawn, so no task token
            # exists yet — but its pi call goes through the ctf-gateway provider,
            # which requires one. Mint a probe-scoped token and inject the same
            # env block the worker spawn path uses (worker_runtime_mixin).
            from dswarm.solver.modelgateway import ModelGateway, WorkerClaims

            gateway = ModelGateway.instance()
            token = gateway.issue_worker(WorkerClaims(
                run_id=str(getattr(executor, "run_id", "") or ""),
                challenge_id=None,
                worker_instance_id=worker_instance_id,
                solver_id=None,
                profile_id=str(pool_spec.profile_id or ""),
                configured_account_id=(
                    str(pool_spec.credential_binding_id or "").strip() or None
                ),
                token_scope="worker",
            ))
            import os as _os
            gateway_url = _os.environ.get(
                "DSWARM_GATEWAY_URL",
                "http://host.docker.internal:"
                f"{_os.environ.get('DSWARM_MODEL_GATEWAY_PORT', '9101')}/v1",
            )
            env.update({
                "DEEPSEEK_API_KEY": token,
                "DSWARM_TASK_TOKEN": token,
                "DSWARM_GATEWAY_URL": gateway_url,
                "DSWARM_PI_PROVIDER": "ctf-gateway",
                "DSWARM_WORKER_MODEL": str(pool_spec.model or "deepseek-v4-flash"),
            })
        return await asyncio.wait_for(
            executor.run(
                driver,
                list(spec.argv),
                host_cwd=str(host_cwd),
                timeout=max(1, int(timeout)),
                env=env,
                worker_instance_id=worker_instance_id,
                operation_kind="runtime_probe",
            ),
            timeout=timeout,
        )

    @staticmethod
    def _classify_result(result: Any) -> tuple[RuntimeFailure | None, str, dict[str, Any]]:
        runtime = getattr(result, "runtime_status", {}) or {}
        if bool(getattr(result, "timed_out", False)) or bool(runtime.get("timed_out")):
            return RuntimeFailure("infrastructure", "timeout"), "unknown", {}
        if bool(getattr(result, "cancelled", False)) or bool(runtime.get("cancelled")):
            return RuntimeFailure("infrastructure", "cancelled"), "unknown", {}
        text = str(getattr(result, "text", "") or "").strip()
        rc = runtime.get("rc")
        if isinstance(rc, int) and rc != 0:
            return RuntimeFailure("infrastructure", "nonzero_exit"), "unknown", {}
        if not text:
            return RuntimeFailure("worker", "empty_probe_reply"), "unknown", {}
        input_tokens = getattr(result, "input_tokens", None)
        output_tokens = getattr(result, "output_tokens", None)
        if (
            isinstance(input_tokens, int)
            and not isinstance(input_tokens, bool)
            and input_tokens >= 0
            and isinstance(output_tokens, int)
            and not isinstance(output_tokens, bool)
            and output_tokens >= 0
        ):
            return None, "measured", {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
            }
        return None, "unknown", {}

    @staticmethod
    def _classify_exception(exc: Exception) -> RuntimeFailure:
        text = str(exc).lower()
        if any(token in text for token in ("401", "403", "unauthorized", "forbidden", "api key", "authentication")):
            return RuntimeFailure("auth", "auth_failed")
        if any(token in text for token in ("model not found", "configuration", "config", "unsupported model", "invalid option")):
            return RuntimeFailure("configuration", "model_or_config_failed")
        if any(token in text for token in ("timeout", "timed out", "deadline")):
            return RuntimeFailure("infrastructure", "timeout")
        if any(token in text for token in ("connection", "connect", "transport", "reset", "network", "eof", "runtime_")):
            return RuntimeFailure("infrastructure", "transport_error")
        return RuntimeFailure("infrastructure", "probe_failed")

    @staticmethod
    def _outcome_for(failure: RuntimeFailure) -> str:
        if failure.code == "timeout":
            return "timeout"
        if failure.category == "auth" or failure.category == "configuration":
            return "provider_error"
        if failure.category == "infrastructure":
            return "transport_error"
        return "provider_error"
