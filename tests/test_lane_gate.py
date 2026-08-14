from __future__ import annotations

import asyncio

import pytest

from dswarm.swarm.lane_gate import (
    WorkerLaneDisabled,
    WorkerLaneGate,
    WorkerLaneStopped,
)


def test_lane_classification_includes_verifier_and_review() -> None:
    assert WorkerLaneGate.lane_for(mode="review") == "review"
    assert WorkerLaneGate.lane_for(mode="explore", worker_class="review") == "review"
    assert WorkerLaneGate.lane_for(mode="explore", worker_class="verifier") == "review"
    assert WorkerLaneGate.lane_for(mode="recon", worker_class="shell_agent") == "ordinary"
    assert WorkerLaneGate.lane_for(mode="recovery", worker_class="code") == "ordinary"


async def test_lane_gate_enforces_ordinary_and_reserved_review_capacity() -> None:
    gate = WorkerLaneGate(max_workers=2, review_max_concurrent=1)

    ordinary_a = await gate.acquire(mode="explore")
    ordinary_b = await gate.acquire(mode="bootstrap")
    review_a = await gate.acquire(mode="review")
    assert gate.snapshot() == {"ordinary_active": 2, "review_active": 1}

    ordinary_waiter = asyncio.create_task(gate.acquire(mode="recovery"))
    review_waiter = asyncio.create_task(
        gate.acquire(mode="explore", worker_class="verifier")
    )
    await asyncio.sleep(0)
    assert ordinary_waiter.done() is False
    assert review_waiter.done() is False

    gate.release(ordinary_a)
    gate.release(review_a)
    assert await asyncio.wait_for(ordinary_waiter, timeout=1.0) == "ordinary"
    assert await asyncio.wait_for(review_waiter, timeout=1.0) == "review"
    assert gate.snapshot() == {"ordinary_active": 2, "review_active": 1}

    gate.release(ordinary_b)
    gate.release("ordinary")
    gate.release("review")
    assert gate.snapshot() == {"ordinary_active": 0, "review_active": 0}


async def test_zero_review_capacity_is_disabled_not_coerced_to_one() -> None:
    gate = WorkerLaneGate(max_workers=1, review_max_concurrent=0)

    with pytest.raises(WorkerLaneDisabled):
        await gate.acquire(mode="review")

    assert gate.snapshot() == {"ordinary_active": 0, "review_active": 0}


async def test_stop_interrupts_capacity_wait_without_leaking_permit() -> None:
    gate = WorkerLaneGate(max_workers=1, review_max_concurrent=1)
    held = await gate.acquire(mode="explore")
    stop_event = asyncio.Event()
    waiter = asyncio.create_task(
        gate.acquire(mode="explore", stop_event=stop_event)
    )
    await asyncio.sleep(0)

    stop_event.set()
    with pytest.raises(WorkerLaneStopped):
        await asyncio.wait_for(waiter, timeout=1.0)

    assert gate.snapshot() == {"ordinary_active": 1, "review_active": 0}
    gate.release(held)
    again = await asyncio.wait_for(gate.acquire(mode="explore"), timeout=1.0)
    gate.release(again)
    assert gate.snapshot() == {"ordinary_active": 0, "review_active": 0}


async def test_cancelled_capacity_waiter_does_not_consume_future_permit() -> None:
    gate = WorkerLaneGate(max_workers=1, review_max_concurrent=1)
    held = await gate.acquire(mode="explore")
    waiter = asyncio.create_task(gate.acquire(mode="explore"))
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    gate.release(held)
    again = await asyncio.wait_for(gate.acquire(mode="explore"), timeout=1.0)
    gate.release(again)
    assert gate.snapshot() == {"ordinary_active": 0, "review_active": 0}


class _AcquireCompletesDuringCancellation:
    """Semaphore double whose acquire wins while cancellation is being cleaned up."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release_calls = 0

    async def acquire(self) -> bool:
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            return True
        return True

    def release(self) -> None:
        self.release_calls += 1


async def test_cancel_race_returns_permit_acquired_during_cleanup() -> None:
    gate = WorkerLaneGate(max_workers=1, review_max_concurrent=1)
    semaphore = _AcquireCompletesDuringCancellation()
    gate._semaphores["ordinary"] = semaphore  # type: ignore[assignment]
    stop_event = asyncio.Event()
    waiter = asyncio.create_task(
        gate.acquire(mode="explore", stop_event=stop_event)
    )
    await semaphore.started.wait()

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert semaphore.release_calls == 1
    assert gate.snapshot() == {"ordinary_active": 0, "review_active": 0}


async def test_paused_waiter_does_not_start_until_resume() -> None:
    gate = WorkerLaneGate(max_workers=1, review_max_concurrent=1)
    held = await gate.acquire(mode="explore")
    pause_event = asyncio.Event()
    waiter = asyncio.create_task(
        gate.acquire(mode="explore", pause_event=pause_event)
    )
    await asyncio.sleep(0)

    gate.release(held)
    await asyncio.sleep(0)
    assert waiter.done() is False
    assert gate.snapshot() == {"ordinary_active": 0, "review_active": 0}

    pause_event.set()
    lane = await asyncio.wait_for(waiter, timeout=1.0)
    gate.release(lane)
    assert gate.snapshot() == {"ordinary_active": 0, "review_active": 0}
