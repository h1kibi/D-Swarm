"""Shared concurrency gate for ordinary and review/verifier workers.

The gate is the single owner of run-local worker lane capacity.  Ordinary work
uses ``max_workers`` slots.  Reviewer and verifier work uses an independent
reserved lane so evidence review can still run while ordinary solving is full.
"""

from __future__ import annotations

import asyncio
from typing import Literal, Optional

WorkerLane = Literal["ordinary", "review"]


class WorkerLaneDisabled(RuntimeError):
    """Raised when a worker targets a lane configured with zero capacity."""


class WorkerLaneStopped(RuntimeError):
    """Raised when a stop request interrupts a pending lane acquisition."""


class WorkerLaneGate:
    """Cancellation-safe two-lane worker capacity gate.

    The two lane limits imply all three scheduler constraints::

        ordinary_active <= max_workers
        review_active <= review_max_concurrent
        total_active <= max_workers + review_max_concurrent

    A paused waiter does not consume a permit.  A stopped or cancelled waiter
    releases a permit if it won the semaphore race at the same instant.
    """

    def __init__(self, max_workers: int, review_max_concurrent: int) -> None:
        self.ordinary_limit = max(0, int(max_workers))
        self.review_limit = max(0, int(review_max_concurrent))
        self._semaphores: dict[WorkerLane, asyncio.Semaphore] = {
            "ordinary": asyncio.Semaphore(self.ordinary_limit),
            "review": asyncio.Semaphore(self.review_limit),
        }
        self._active: dict[WorkerLane, int] = {"ordinary": 0, "review": 0}

    @staticmethod
    def lane_for(*, mode: str = "", worker_class: str = "") -> WorkerLane:
        normalized_mode = str(mode or "").strip().lower()
        normalized_class = str(worker_class or "").strip().lower()
        if normalized_class in {"review", "verifier"}:
            return "review"
        if normalized_mode in {"review", "verify", "verifier"}:
            return "review"
        return "ordinary"

    def _limit_for(self, lane: WorkerLane) -> int:
        return self.review_limit if lane == "review" else self.ordinary_limit

    @staticmethod
    async def _cancel_task(task: Optional[asyncio.Task]) -> None:
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _wait_until_running(
        self,
        *,
        pause_event: Optional[asyncio.Event],
        stop_event: Optional[asyncio.Event],
    ) -> None:
        while pause_event is not None and not pause_event.is_set():
            if stop_event is not None and stop_event.is_set():
                raise WorkerLaneStopped("worker dispatch stopped while paused")
            pause_task = asyncio.create_task(pause_event.wait())
            stop_task = (
                asyncio.create_task(stop_event.wait())
                if stop_event is not None else None
            )
            try:
                wait_set = {pause_task}
                if stop_task is not None:
                    wait_set.add(stop_task)
                await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)
                if stop_event is not None and stop_event.is_set():
                    raise WorkerLaneStopped("worker dispatch stopped while paused")
            finally:
                await self._cancel_task(pause_task)
                await self._cancel_task(stop_task)

    async def _acquire_semaphore(
        self,
        semaphore: asyncio.Semaphore,
        *,
        stop_event: Optional[asyncio.Event],
    ) -> None:
        if stop_event is None:
            await semaphore.acquire()
            return
        if stop_event.is_set():
            raise WorkerLaneStopped("worker dispatch stopped before capacity became available")

        permit_task = asyncio.create_task(semaphore.acquire())
        stop_task = asyncio.create_task(stop_event.wait())

        def _task_acquired_permit(task: asyncio.Task) -> bool:
            return (
                task.done()
                and not task.cancelled()
                and task.exception() is None
                and bool(task.result())
            )

        try:
            await asyncio.wait(
                {permit_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_event.is_set():
                raise WorkerLaneStopped(
                    "worker dispatch stopped before capacity became available"
                )
            await permit_task
        except BaseException:
            if not permit_task.done():
                permit_task.cancel()
            if not stop_task.done():
                stop_task.cancel()
            await asyncio.gather(permit_task, stop_task, return_exceptions=True)
            # Cancellation and capacity release can win in the same loop turn.
            # Inspect only after cleanup has settled the acquire task, otherwise a
            # late successful acquire would silently leak its semaphore permit.
            if _task_acquired_permit(permit_task):
                semaphore.release()
            raise
        else:
            if not stop_task.done():
                stop_task.cancel()
            try:
                await asyncio.gather(stop_task, return_exceptions=True)
            except BaseException:
                # The caller never received the lane, so return the permit if
                # cancellation interrupts stop-waiter cleanup.
                semaphore.release()
                raise

    async def acquire(
        self,
        *,
        mode: str = "",
        worker_class: str = "",
        stop_event: Optional[asyncio.Event] = None,
        pause_event: Optional[asyncio.Event] = None,
    ) -> WorkerLane:
        lane = self.lane_for(mode=mode, worker_class=worker_class)
        if self._limit_for(lane) <= 0:
            raise WorkerLaneDisabled(f"{lane} worker lane is disabled")

        semaphore = self._semaphores[lane]
        while True:
            if stop_event is not None and stop_event.is_set():
                raise WorkerLaneStopped("worker dispatch stopped")
            await self._wait_until_running(
                pause_event=pause_event,
                stop_event=stop_event,
            )
            await self._acquire_semaphore(semaphore, stop_event=stop_event)
            if stop_event is not None and stop_event.is_set():
                semaphore.release()
                raise WorkerLaneStopped("worker dispatch stopped")
            if pause_event is not None and not pause_event.is_set():
                semaphore.release()
                continue
            self._active[lane] += 1
            return lane

    def release(self, lane: WorkerLane) -> None:
        normalized = str(lane or "").strip().lower()
        if normalized not in self._active:
            raise ValueError(f"unknown worker lane: {lane}")
        typed_lane: WorkerLane = normalized  # type: ignore[assignment]
        if self._active[typed_lane] <= 0:
            raise RuntimeError(f"worker lane released without an active permit: {typed_lane}")
        self._active[typed_lane] -= 1
        self._semaphores[typed_lane].release()

    def snapshot(self) -> dict[str, int]:
        return {
            "ordinary_active": self._active["ordinary"],
            "review_active": self._active["review"],
        }
