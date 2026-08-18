"""Run-scoped long-lived container pool ownership and worker leases."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
import re
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Literal, Mapping, Protocol

from dswarm.solver.container_runtime import ContainerRuntimeError
from dswarm.solver.runtime_credentials import (
    CredentialMode,
    CredentialProjectionCleanupError,
    CredentialProjectionError,
)
from dswarm.solver.runtime_policy import PoolSpec, RuntimeSnapshot


_FAILURE_CATEGORIES = frozenset(
    {"infrastructure", "identity", "auth", "configuration", "capacity", "worker"}
)
_FAILURE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_POOL_STATES = frozenset(
    {"new", "starting", "probing", "ready", "recovering", "degraded", "stopping", "stopped"}
)
_ALLOWED_TRANSITIONS = {
    "new": frozenset({"starting", "degraded", "stopping"}),
    "starting": frozenset({"probing", "degraded", "stopping"}),
    "probing": frozenset({"ready", "degraded", "stopping"}),
    "ready": frozenset({"recovering", "degraded", "stopping"}),
    "recovering": frozenset({"starting", "degraded", "stopping"}),
    "degraded": frozenset({"stopping"}),
    "stopping": frozenset({"stopped"}),
    "stopped": frozenset(),
}


@dataclass(frozen=True)
class RuntimeFailure(RuntimeError):
    """Machine-safe runtime failure suitable for events and waiter propagation."""

    category: Literal[
        "infrastructure", "identity", "auth", "configuration", "capacity", "worker"
    ]
    code: str

    def __post_init__(self) -> None:
        if self.category not in _FAILURE_CATEGORIES:
            raise ValueError("invalid_failure_category")
        if not isinstance(self.code, str) or _FAILURE_CODE_RE.fullmatch(self.code) is None:
            raise ValueError("invalid_failure_code")
        RuntimeError.__init__(self, self.code)

    def snapshot(self) -> dict[str, str]:
        return {"category": self.category, "code": self.code}


@dataclass(frozen=True)
class RuntimeProbeResult:
    ready: bool
    probe_id: str
    failure: RuntimeFailure | None
    cache_identity: str

    def __post_init__(self) -> None:
        def safe_token(value: object, *, allow_empty: bool) -> bool:
            if not isinstance(value, str) or len(value) > 256:
                return False
            if not value:
                return allow_empty
            return all(0x21 <= ord(char) <= 0x7E for char in value)

        if not isinstance(self.ready, bool):
            raise ValueError("invalid_probe_result")
        if not safe_token(self.probe_id, allow_empty=False):
            raise ValueError("invalid_probe_result")
        if not safe_token(self.cache_identity, allow_empty=not self.ready):
            raise ValueError("invalid_probe_result")
        if self.ready:
            if self.failure is not None:
                raise ValueError("invalid_probe_result")
        elif not isinstance(self.failure, RuntimeFailure):
            raise ValueError("invalid_probe_result")


class RuntimeProbeProtocol(Protocol):
    async def run(
        self,
        *,
        executor: Any,
        pool_spec: PoolSpec,
        credential_projection: Any,
        generation: int,
        timeout: float,
    ) -> RuntimeProbeResult: ...


@dataclass(frozen=True)
class RuntimePoolView:
    pool_id: str
    state: str
    generation: int
    pool_instance_id: str
    active_workers: int
    waiting_workers: int
    capacity: int
    failure: RuntimeFailure | None
    recovery_episode: int


@dataclass(frozen=True)
class PoolCloseReport:
    closed: bool
    pool_count: int
    failures: tuple[RuntimeFailure, ...]


@dataclass
class WorkerRuntimeLease:
    """One capacity permit plus one private credential projection."""

    pool_id: str
    pool_instance_id: str
    generation: int
    worker_instance_id: str
    executor: Any
    credential_projection: Any
    worker_env: Mapping[str, str]
    _release_once: Callable[[], Awaitable[None]]
    _released: bool = field(default=False, init=False, repr=False)
    _release_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.worker_env = MappingProxyType(dict(self.worker_env))

    @property
    def released(self) -> bool:
        return self._released

    async def release(self) -> None:
        async with self._release_lock:
            if self._released:
                return
            try:
                await self._release_once()
            finally:
                # The manager release callback relinquishes ownership in its own
                # finally block, even when credential cleanup reports a residual.
                self._released = True


@dataclass
class _ContainerPoolEntry:
    pool_spec: PoolSpec
    semaphore: asyncio.Semaphore
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    unavailable: asyncio.Event = field(default_factory=asyncio.Event)
    state: str = "new"
    generation: int = 0
    executor: Any = None
    startup_task: asyncio.Task[Any] | None = None
    recovery_task: asyncio.Task[Any] | None = None
    active_workers: int = 0
    waiting_workers: int = 0
    active_leases: dict[int, WorkerRuntimeLease] = field(default_factory=dict)
    failure: RuntimeFailure | None = None
    recovery_episode: int = 0
    recovery_source_instance: str = ""
    probe_cache_identity: str = ""


class ContainerPoolManager:
    """Own all frozen runtime pools for one run."""

    def __init__(
        self,
        *,
        run_id: str,
        snapshot: RuntimeSnapshot,
        executor_factory: Any,
        probe: RuntimeProbeProtocol,
        credential_projector: Any,
        credential_modes: Mapping[str, CredentialMode] | None = None,
        transition_callback: Callable[[RuntimePoolView, str | None], None] | None = None,
    ) -> None:
        if run_id != snapshot.run_id:
            raise RuntimeFailure(category="configuration", code="snapshot_run_mismatch")
        if len(snapshot.pools) > snapshot.runtime_policy.max_pools_per_run:
            raise RuntimeFailure(category="configuration", code="max_pools_per_run_exceeded")
        self.run_id = run_id
        self.snapshot = snapshot
        self.executor_factory = executor_factory
        self.probe = probe
        self.credential_projector = credential_projector
        self.transition_callback = transition_callback
        self._transition_count = 0
        self._pools = {pool.pool_id: pool for pool in snapshot.pools}
        self._entries = {
            pool.pool_id: _ContainerPoolEntry(
                pool_spec=pool,
                semaphore=asyncio.Semaphore(pool.pool_max_concurrent_workers),
            )
            for pool in snapshot.pools
        }
        modes: dict[str, CredentialMode] = {
            pool.pool_id: "gateway" for pool in snapshot.pools
        }
        for pool_id, mode in dict(credential_modes or {}).items():
            if pool_id not in self._pools or mode not in {"gateway", "direct", "custom"}:
                raise RuntimeFailure(category="configuration", code="invalid_credential_mode")
            modes[pool_id] = mode
        self._credential_modes = MappingProxyType(modes)
        self._manager_lock = asyncio.Lock()
        self._closed = False
        self._close_task: asyncio.Task[PoolCloseReport] | None = None

    async def acquire(
        self,
        *,
        pool_id: str,
        worker_instance_id: str,
        operation_kind: str,
    ) -> WorkerRuntimeLease:
        del operation_kind
        entry = self._entries.get(pool_id)
        if entry is None:
            raise RuntimeFailure(category="configuration", code="unknown_pool")
        if self._closed:
            raise RuntimeFailure(category="infrastructure", code="manager_closed")

        await self._reserve_capacity(entry)
        projection = None
        permit_owned = True
        try:
            executor = await self._ready_executor(entry)
            projection = await self._project(
                entry=entry,
                worker_instance_id=worker_instance_id,
            )
            lease_holder: dict[str, WorkerRuntimeLease] = {}
            released = False
            release_lock = asyncio.Lock()

            async def release_once() -> None:
                nonlocal released
                async with release_lock:
                    if released:
                        return
                    cleanup_failure = None
                    try:
                        await asyncio.to_thread(projection.close)
                    except Exception as exc:
                        cleanup_failure = self._failure_from_exception(exc)
                    finally:
                        async with entry.lock:
                            lease = lease_holder.get("lease")
                            if lease is not None:
                                entry.active_leases.pop(id(lease), None)
                            if entry.active_workers > 0:
                                entry.active_workers -= 1
                        entry.semaphore.release()
                        released = True
                    if cleanup_failure is not None:
                        raise cleanup_failure

            async with entry.lock:
                if self._closed or entry.state != "ready" or entry.executor is not executor:
                    raise self._entry_unavailable_failure(entry)
                lease = WorkerRuntimeLease(
                    pool_id=pool_id,
                    pool_instance_id=executor.pool_instance_id,
                    generation=entry.generation,
                    worker_instance_id=worker_instance_id,
                    executor=executor,
                    credential_projection=projection,
                    worker_env=dict(projection.env),
                    _release_once=release_once,
                )
                lease_holder["lease"] = lease
                entry.active_workers += 1
                entry.active_leases[id(lease)] = lease
            permit_owned = False
            return lease
        except BaseException:
            if projection is not None:
                try:
                    await asyncio.to_thread(projection.close)
                except Exception:
                    pass
            if permit_owned:
                entry.semaphore.release()
            raise

    async def _reserve_capacity(self, entry: _ContainerPoolEntry) -> None:
        async with entry.lock:
            if self._closed or entry.unavailable.is_set():
                raise self._entry_unavailable_failure(entry)
            entry.waiting_workers += 1

        permit_task = asyncio.create_task(entry.semaphore.acquire())
        unavailable_task = asyncio.create_task(entry.unavailable.wait())
        permit_acquired = False
        try:
            done, _pending = await asyncio.wait(
                {permit_task, unavailable_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if permit_task in done:
                permit_acquired = bool(permit_task.result())
            if unavailable_task in done or self._closed:
                if permit_acquired:
                    entry.semaphore.release()
                    permit_acquired = False
                raise self._entry_unavailable_failure(entry)
            unavailable_task.cancel()
            with suppress(asyncio.CancelledError):
                await unavailable_task
            async with entry.lock:
                if self._closed or entry.unavailable.is_set():
                    if permit_acquired:
                        entry.semaphore.release()
                        permit_acquired = False
                    raise self._entry_unavailable_failure(entry)
        except BaseException:
            if permit_acquired:
                entry.semaphore.release()
                permit_acquired = False
            elif permit_task.done() and not permit_task.cancelled():
                with suppress(BaseException):
                    if permit_task.result():
                        entry.semaphore.release()
            elif not permit_task.done():
                permit_task.cancel()
                with suppress(asyncio.CancelledError):
                    await permit_task
            if not unavailable_task.done():
                unavailable_task.cancel()
                with suppress(asyncio.CancelledError):
                    await unavailable_task
            raise
        finally:
            async with entry.lock:
                entry.waiting_workers -= 1

    async def _ready_executor(self, entry: _ContainerPoolEntry) -> Any:
        async with entry.lock:
            if self._closed or entry.unavailable.is_set():
                raise self._entry_unavailable_failure(entry)
            if entry.state == "ready" and entry.executor is not None:
                return entry.executor
            if entry.startup_task is None:
                entry.generation += 1
                self._apply_transition(entry, "starting")
                entry.startup_task = asyncio.create_task(
                    self._start_generation(entry, entry.generation)
                )
            task = entry.startup_task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            raise self._entry_unavailable_failure(entry)

    async def _start_generation(self, entry: _ContainerPoolEntry, generation: int) -> Any:
        executor = None
        projection = None
        try:
            executor = await self.executor_factory(
                run_id=self.run_id,
                pool_spec=entry.pool_spec,
                generation=generation,
            )
            async with entry.lock:
                if self._closed or entry.generation != generation or entry.state != "starting":
                    raise self._entry_unavailable_failure(entry)
                self._apply_transition(entry, "probing")
            projection = await self._project(
                entry=entry,
                worker_instance_id=f"probe-{generation}",
            )
            result = await self.probe.run(
                executor=executor,
                pool_spec=entry.pool_spec,
                credential_projection=projection,
                generation=generation,
                timeout=self.snapshot.runtime_policy.probe_timeout_seconds,
            )
            if not result.ready:
                raise result.failure or RuntimeFailure(
                    category="infrastructure", code="probe_failed"
                )
            async with entry.lock:
                if self._closed or entry.generation != generation or entry.state != "probing":
                    raise self._entry_unavailable_failure(entry)
                entry.executor = executor
                entry.probe_cache_identity = result.cache_identity
                entry.failure = None
                self._apply_transition(entry, "ready")
            return executor
        except asyncio.CancelledError:
            if executor is not None:
                cleanup_failure = await self._terminate_with_failure(executor)
                if cleanup_failure is not None:
                    async with entry.lock:
                        if entry.generation == generation:
                            entry.executor = executor
                            entry.failure = cleanup_failure
                            entry.unavailable.set()
            raise
        except BaseException as exc:
            failure = self._failure_from_exception(exc)
            residual_executor = None
            if executor is not None:
                cleanup_failure = await self._terminate_with_failure(executor)
                if cleanup_failure is not None:
                    failure = cleanup_failure
                    residual_executor = executor
            async with entry.lock:
                if entry.generation == generation:
                    if residual_executor is not None:
                        entry.executor = residual_executor
                    entry.failure = failure
                    if entry.state not in {"stopping", "stopped"}:
                        self._apply_transition(entry, "degraded")
                    entry.unavailable.set()
            raise failure
        finally:
            if projection is not None:
                try:
                    await asyncio.to_thread(projection.close)
                except Exception:
                    pass

    async def _project(
        self, *, entry: _ContainerPoolEntry, worker_instance_id: str
    ) -> Any:
        try:
            return await asyncio.to_thread(
                self.credential_projector.project,
                run_id=self.run_id,
                pool_id=entry.pool_spec.pool_id,
                worker_instance_id=worker_instance_id,
                binding_id=entry.pool_spec.credential_binding_id,
                credential_mode=self._credential_modes[entry.pool_spec.pool_id],
            )
        except CredentialProjectionError as exc:
            raise RuntimeFailure(category="auth", code=exc.code) from exc

    async def mark_failure(
        self,
        *,
        pool_instance_id: str,
        failure: RuntimeFailure,
        pool_id: str | None = None,
    ) -> bool:
        """Mark a current generation failure and recover infrastructure once."""

        if pool_id is None:
            matches = [
                candidate
                for candidate in self._entries.values()
                if (
                    candidate.executor is not None
                    and str(candidate.executor.pool_instance_id) == pool_instance_id
                )
                or candidate.recovery_source_instance == pool_instance_id
            ]
            if not matches:
                return False
            if len(matches) != 1:
                raise RuntimeFailure(
                    category="identity", code="ambiguous_pool_instance"
                )
            entry = matches[0]
        else:
            entry = self._entries.get(pool_id)
            if entry is None:
                raise RuntimeFailure(category="configuration", code="unknown_pool")
        if not isinstance(failure, RuntimeFailure):
            raise RuntimeFailure(category="configuration", code="invalid_runtime_failure")
        if failure.category in {"worker", "capacity"}:
            return False

        recovery_task: asyncio.Task[Any] | None = None
        async with entry.lock:
            if self._closed or entry.state in {"stopping", "stopped"}:
                raise RuntimeFailure(category="infrastructure", code="manager_closed")
            current_instance = (
                str(entry.executor.pool_instance_id) if entry.executor is not None else ""
            )
            if (
                entry.recovery_task is not None
                and not entry.recovery_task.done()
                and getattr(entry, "recovery_source_instance", "") == pool_instance_id
            ):
                recovery_task = entry.recovery_task
            elif current_instance != pool_instance_id:
                return False
            elif failure.category == "infrastructure":
                if entry.state != "ready":
                    return False
                entry.failure = failure
                entry.recovery_episode += 1
                self._apply_transition(entry, "recovering")
                entry.unavailable.set()
                entry.recovery_source_instance = pool_instance_id
                entry.recovery_task = asyncio.create_task(
                    self._recover_generation(entry, entry.executor)
                )
                recovery_task = entry.recovery_task
            else:
                if entry.state != "ready":
                    return False
                entry.failure = failure
                self._apply_transition(entry, "degraded")
                entry.unavailable.set()
                return True

        if recovery_task is not None:
            with suppress(RuntimeFailure):
                await asyncio.shield(recovery_task)
            return True
        return False

    async def _recover_generation(self, entry: _ContainerPoolEntry, old_executor: Any) -> Any:
        async with entry.lock:
            leases = tuple(entry.active_leases.values())
        for lease in leases:
            try:
                await lease.release()
            except Exception:
                pass

        try:
            await old_executor.terminate(require_proof=True)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            failure = self._failure_from_exception(exc)
            async with entry.lock:
                if entry.state not in {"stopping", "stopped"}:
                    entry.failure = failure
                    self._apply_transition(entry, "degraded")
                    entry.unavailable.set()
            raise failure

        async with entry.lock:
            if self._closed or entry.state != "recovering" or entry.executor is not old_executor:
                raise self._entry_unavailable_failure(entry)
            entry.executor = None
            entry.startup_task = None
            entry.generation += 1
            self._apply_transition(entry, "starting")
            generation = entry.generation

        executor = await self._start_generation(entry, generation)
        async with entry.lock:
            if entry.state == "ready" and entry.executor is executor:
                entry.unavailable.clear()
        return executor
    async def close(self) -> PoolCloseReport:
        async with self._manager_lock:
            if self._close_task is None:
                self._closed = True
                self._close_task = asyncio.create_task(self._close_impl())
            task = self._close_task
        return await asyncio.shield(task)

    async def _close_impl(self) -> PoolCloseReport:
        failures: list[RuntimeFailure] = []
        tasks: list[asyncio.Task[Any]] = []
        leases: list[WorkerRuntimeLease] = []
        executors: dict[int, Any] = {}
        for entry in self._entries.values():
            async with entry.lock:
                if entry.state != "stopped":
                    self._apply_transition(entry, "stopping")
                entry.unavailable.set()
                for task in (entry.startup_task, entry.recovery_task):
                    if task is not None and not task.done():
                        task.cancel()
                        tasks.append(task)
                leases.extend(entry.active_leases.values())
                if entry.executor is not None:
                    executors[id(entry.executor)] = entry.executor

        for lease in leases:
            try:
                await lease.release()
            except Exception as exc:
                failures.append(self._failure_from_exception(exc))
        for task in tasks:
            with suppress(BaseException):
                await task
        # A cancelled startup may discover that its generation cannot be proven
        # stopped. Re-scan after joining tasks so close owns that residual too.
        for entry in self._entries.values():
            async with entry.lock:
                if entry.executor is not None:
                    executors[id(entry.executor)] = entry.executor
        termination_failures: dict[int, RuntimeFailure] = {}
        for executor_id, executor in executors.items():
            try:
                await executor.terminate(require_proof=True)
            except Exception as exc:
                failure = self._failure_from_exception(exc)
                termination_failures[executor_id] = failure
                failures.append(failure)
        for entry in self._entries.values():
            async with entry.lock:
                residual_failure = (
                    termination_failures.get(id(entry.executor))
                    if entry.executor is not None
                    else None
                )
                entry.active_leases.clear()
                entry.active_workers = 0
                if residual_failure is not None:
                    # A failed proof is not equivalent to termination.  Retain the
                    # executor identity and stopping state so the reopen barrier can
                    # discover and clean the residual generation later.
                    entry.failure = residual_failure
                    callback = self.transition_callback
                    if callback is not None:
                        try:
                            callback(self._view_for_entry(entry), residual_failure.code)
                        except Exception:
                            pass
                    continue
                entry.executor = None
                if entry.state != "stopped":
                    self._apply_transition(entry, "stopped")
        return PoolCloseReport(
            closed=True,
            pool_count=len(self._entries),
            failures=tuple(failures),
        )

    def snapshot_view(self) -> tuple[RuntimePoolView, ...]:
        return tuple(
            RuntimePoolView(
                pool_id=entry.pool_spec.pool_id,
                state=entry.state,
                generation=entry.generation,
                pool_instance_id=(
                    str(entry.executor.pool_instance_id) if entry.executor is not None else ""
                ),
                active_workers=entry.active_workers,
                waiting_workers=entry.waiting_workers,
                capacity=entry.pool_spec.pool_max_concurrent_workers,
                failure=entry.failure,
                recovery_episode=entry.recovery_episode,
            )
            for entry in sorted(
                self._entries.values(), key=lambda item: item.pool_spec.pool_id
            )
        )

    @property
    def transition_count(self) -> int:
        return self._transition_count

    def _view_for_entry(self, entry: _ContainerPoolEntry) -> RuntimePoolView:
        return RuntimePoolView(
            pool_id=entry.pool_spec.pool_id,
            state=entry.state,
            generation=entry.generation,
            pool_instance_id=(
                str(entry.executor.pool_instance_id)
                if entry.executor is not None
                else ""
            ),
            active_workers=entry.active_workers,
            waiting_workers=entry.waiting_workers,
            capacity=entry.pool_spec.pool_max_concurrent_workers,
            failure=entry.failure,
            recovery_episode=entry.recovery_episode,
        )

    @staticmethod
    def _transition(entry: _ContainerPoolEntry, target: str) -> None:
        if target not in _POOL_STATES:
            raise RuntimeFailure(category="configuration", code="invalid_pool_state")
        if target == entry.state:
            return
        if target not in _ALLOWED_TRANSITIONS[entry.state]:
            raise RuntimeFailure(category="configuration", code="invalid_pool_transition")
        entry.state = target

    def _apply_transition(self, entry: _ContainerPoolEntry, target: str) -> None:
        previous = entry.state
        self._transition(entry, target)
        if previous == entry.state:
            return
        self._transition_count += 1
        callback = self.transition_callback
        if callback is not None:
            try:
                callback(self._view_for_entry(entry), None)
            except Exception:
                # Diagnostics are a private best-effort sidecar.  They must never
                # alter pool state, capacity, or scheduler behavior.
                pass

    def _entry_unavailable_failure(self, entry: _ContainerPoolEntry) -> RuntimeFailure:
        if self._closed or entry.state in {"stopping", "stopped"}:
            return RuntimeFailure(category="infrastructure", code="manager_closed")
        return entry.failure or RuntimeFailure(
            category="infrastructure", code="pool_unavailable"
        )

    @staticmethod
    def _failure_from_exception(exc: BaseException) -> RuntimeFailure:
        if isinstance(exc, RuntimeFailure):
            return exc
        if isinstance(exc, CredentialProjectionCleanupError):
            return RuntimeFailure(category="infrastructure", code=exc.code)
        if isinstance(exc, CredentialProjectionError):
            return RuntimeFailure(category="auth", code=exc.code)
        if isinstance(exc, ContainerRuntimeError):
            category = "identity" if exc.code == "runtime_identity_mismatch" else "infrastructure"
            return RuntimeFailure(category=category, code=exc.code)
        return RuntimeFailure(category="infrastructure", code="runtime_operation_failed")

    @classmethod
    async def _terminate_with_failure(cls, executor: Any) -> RuntimeFailure | None:
        try:
            await executor.terminate(require_proof=True)
        except BaseException as exc:
            return cls._failure_from_exception(exc)
        return None


__all__ = [
    "ContainerPoolManager",
    "PoolCloseReport",
    "RuntimeFailure",
    "RuntimePoolView",
    "RuntimeProbeProtocol",
    "RuntimeProbeResult",
    "WorkerRuntimeLease",
]