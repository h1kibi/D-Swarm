"""TUI (Textual) headless tests (Sprint 1.3 acceptance).

Drives a mock solve through the EventBus and asserts: the transcript renders the
stream live (incl. the flag line), the status bar reflects cost, and a typed
command reaches the HITL sink. Uses Textual's headless run_test().
"""

import asyncio

import pytest

from apps.tui import __main__ as tui_main
from apps.tui.app import DSwarmTUI, format_event
from examples.mock_solver import run_mock_solve
from dswarm.core.cost import CostController
from dswarm.core.event_bus import EventBus
from dswarm.core.events import Event, EventType


def test_format_event_covers_key_types() -> None:
    f = format_event(Event(event_type=EventType.RUN_STARTED, run_id="r",
                           payload={"challenge": {"name": "x", "category": "web"}}))
    assert f and "RUN" in f
    flag = format_event(Event(event_type=EventType.SOLVE_GRAPH_DELTA, run_id="r",
                              payload={"kind": "flag", "flag": "flag{y}"}))
    assert flag and "flag{y}" in flag
    # noisy/empty reasoning is skipped
    assert format_event(Event(event_type=EventType.REASONING_DELTA, run_id="r",
                              payload={"text": "   "})) is None


async def test_tui_renders_mock_solve_and_captures_flag() -> None:
    bus = EventBus()
    cost = CostController(bus=bus)
    app = DSwarmTUI(bus, lineup="mock-flash", stop_on_finish=False)

    async with app.run_test() as pilot:
        # give the subscriber a moment to attach, then drive the solve
        await pilot.pause()
        await run_mock_solve(bus, cost, run_id="tui-mock")
        # let events flush into the transcript
        for _ in range(20):
            await pilot.pause()
            if app.finished:
                break
        assert app.finished is True
        assert "FLAG" in app.last_line or "FINISHED" in app.last_line
        # cost was tracked into the status bar
        assert app._usd > 0


async def test_tui_hitl_command_reaches_sink() -> None:
    bus = EventBus()
    got: list[tuple] = []

    async def sink(target: str, action: str, text: str) -> None:
        got.append((target, action, text))

    app = DSwarmTUI(bus, hitl=sink, lineup="mock")
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#cmd")
        inp.value = "/hint try LFI on the include param"
        await pilot.press("enter")
        await pilot.pause()
    assert got == [("global", "hint", "try LFI on the include param")]


async def test_tui_escape_sends_interrupt() -> None:
    bus = EventBus()
    got: list[tuple] = []

    async def sink(target: str, action: str, text: str) -> None:
        got.append((target, action, text))

    app = DSwarmTUI(bus, hitl=sink)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert ("global", "interrupt", "") in got


@pytest.mark.asyncio
async def test_owned_swarm_closes_pool_after_sandbox_shutdown() -> None:
    calls: list[str] = []

    class Manager:
        async def close(self) -> None:
            calls.append("close")

    class Sandbox:
        async def shutdown_all(self) -> None:
            calls.append("shutdown")

    class OwnedSwarm:
        pool_manager = Manager()

        async def run(self) -> None:
            calls.append("run")

    await tui_main._run_owned_swarm(OwnedSwarm(), Sandbox())

    assert calls == ["run", "shutdown", "close"]


@pytest.mark.asyncio
async def test_owned_swarm_preserves_run_failure_and_still_closes_pool() -> None:
    calls: list[str] = []

    class Manager:
        async def close(self) -> None:
            calls.append("close")

    class Sandbox:
        async def shutdown_all(self) -> None:
            calls.append("shutdown")

    class OwnedSwarm:
        pool_manager = Manager()

        async def run(self) -> None:
            calls.append("run")
            raise RuntimeError("run_failed")

    with pytest.raises(RuntimeError, match="run_failed"):
        await tui_main._run_owned_swarm(OwnedSwarm(), Sandbox())

    assert calls == ["run", "shutdown", "close"]


@pytest.mark.asyncio
async def test_owned_swarm_closes_pool_when_sandbox_shutdown_fails() -> None:
    calls: list[str] = []

    class Manager:
        async def close(self) -> None:
            calls.append("close")

    class Sandbox:
        async def shutdown_all(self) -> None:
            calls.append("shutdown")
            raise RuntimeError("shutdown_failed")

    class OwnedSwarm:
        pool_manager = Manager()

        async def run(self) -> None:
            calls.append("run")

    with pytest.raises(RuntimeError, match="shutdown_failed"):
        await tui_main._run_owned_swarm(OwnedSwarm(), Sandbox())

    assert calls == ["run", "shutdown", "close"]


@pytest.mark.asyncio
async def test_owned_swarm_propagates_cancellation_after_cleanup() -> None:
    calls: list[str] = []

    class Manager:
        async def close(self) -> None:
            calls.append("close")

    class Sandbox:
        async def shutdown_all(self) -> None:
            calls.append("shutdown")

    class OwnedSwarm:
        pool_manager = Manager()

        async def run(self) -> None:
            calls.append("run")
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await tui_main._run_owned_swarm(OwnedSwarm(), Sandbox())

    assert calls == ["run", "shutdown", "close"]


@pytest.mark.asyncio
async def test_owned_swarm_allows_legacy_swarm_without_pool_manager() -> None:
    calls: list[str] = []

    class Sandbox:
        async def shutdown_all(self) -> None:
            calls.append("shutdown")

    class OwnedSwarm:
        async def run(self) -> None:
            calls.append("run")

    await tui_main._run_owned_swarm(OwnedSwarm(), Sandbox())

    assert calls == ["run", "shutdown"]
