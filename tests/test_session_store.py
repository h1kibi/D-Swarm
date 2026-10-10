"""Finding B: SessionStore.summary() must carry multi-flag fields through and compute
`solved` by mode, so a rehydrated multi-flag run isn't flattened to a single-flag
look-alike and a partial multi-flag run isn't falsely marked solved."""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from dswarm.core.events import Event, EventType
from dswarm.core.session_store import SessionStore
from dswarm.core.storage import safe_run_storage_key


def _write(root: Path, run_id: str, events: list[dict]) -> None:
    path = root / f"{run_id}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for i, ev in enumerate(events, 1):
            ev.setdefault("seq", i)
            ev.setdefault("ts", float(i))
            ev.setdefault("run_id", run_id)
            f.write(json.dumps(ev) + "\n")


def test_summary_multi_flag_partial_not_solved(tmp_path: Path) -> None:
    """1/3 flags collected, run.finished solved=false → solved stays False, flags are
    preserved, and the multi-flag mode survives (expected_flags=3, multi_flag=True).
    The old summary() dropped these fields and FlagFound forced solved=True."""
    store = SessionStore(root=tmp_path)
    _write(tmp_path, "run-mf", [
        {"event_type": "run.started",
         "payload": {"challenge": {"name": "triple", "category": "web",
                                   "expected_flags": 3, "multi_flag": True}}},
        {"event_type": "insight.event",
         "payload": {"kind": "FlagFound", "flag": "flag{one}"}},
        {"event_type": "run.finished",
         "payload": {"solved": False, "flags": ["flag{one}"],
                     "expected_flags": 3, "multi_flag": True}},
    ])
    s = store.summary("run-mf")
    assert s["solved"] is False, "a 1/3 multi-flag run is NOT solved"
    assert s["flags"] == ["flag{one}"]
    assert s["flag"] == "flag{one}"
    assert s["expected_flags"] == 3
    assert s["multi_flag"] is True


def test_storage_key_contains_windows_path_id_inside_root(tmp_path: Path) -> None:
    store = SessionStore(root=tmp_path)
    hostile = r"C:\Users\Public\..\outside"
    path = store._path(hostile)

    assert path.parent == tmp_path
    assert path.name == f"{safe_run_storage_key(hostile)}.jsonl"
    assert path.resolve().is_relative_to(tmp_path.resolve())


async def test_multiple_store_instances_append_complete_jsonl_records(
    tmp_path: Path,
) -> None:
    """Separate backend components must not interleave writes to one run log."""
    stores = [SessionStore(root=tmp_path), SessionStore(root=tmp_path)]
    events = [
        Event(
            event_type=EventType.TEXT_MESSAGE_DELTA,
            run_id="shared-run",
            seq=index,
            payload={"index": index},
        )
        for index in range(100)
    ]

    await asyncio.gather(*[
        stores[index % 2].append(event)
        for index, event in enumerate(events)
    ])

    persisted = stores[0].read_events("shared-run")
    assert len(persisted) == len(events)
    assert {event.payload["index"] for event in persisted} == set(range(100))


def test_replay_ignores_only_a_torn_final_record(tmp_path: Path) -> None:
    store = SessionStore(root=tmp_path)
    path = store._path("torn-run")
    valid = Event(
        event_type=EventType.RUN_STARTED,
        run_id="torn-run",
        payload={},
    ).model_dump_json()
    path.write_text(valid + '\n{"event_type":"run.finished"', encoding="utf-8")

    assert [event.event_type for event in asyncio.run(
        _collect_events(store.replay("torn-run"))
    )] == [EventType.RUN_STARTED]


async def _collect_events(events):
    return [event async for event in events]


def test_replay_rejects_newline_terminated_corruption(tmp_path: Path) -> None:
    store = SessionStore(root=tmp_path)
    path = store._path("corrupt-run")
    path.write_text('{"not_an_event":true}\n', encoding="utf-8")

    with pytest.raises(ValidationError):
        asyncio.run(_collect_events(store.replay("corrupt-run")))


def test_summary_multi_flag_complete_is_solved(tmp_path: Path) -> None:
    """3/3 flags collected with run.finished solved=true → solved True, all flags kept."""
    store = SessionStore(root=tmp_path)
    _write(tmp_path, "run-done", [
        {"event_type": "run.started",
         "payload": {"challenge": {"expected_flags": 3, "multi_flag": True}}},
        {"event_type": "insight.event", "payload": {"kind": "FlagFound", "flag": "f1"}},
        {"event_type": "insight.event", "payload": {"kind": "FlagFound", "flag": "f2"}},
        {"event_type": "insight.event", "payload": {"kind": "FlagFound", "flag": "f3"}},
        {"event_type": "run.finished",
         "payload": {"solved": True, "flags": ["f1", "f2", "f3"],
                     "expected_flags": 3, "multi_flag": True}},
    ])
    s = store.summary("run-done")
    assert s["solved"] is True
    assert s["flags"] == ["f1", "f2", "f3"]
    assert s["expected_flags"] == 3
    assert s["multi_flag"] is True


def test_summary_single_flag_ghost_run_stays_solved(tmp_path: Path) -> None:
    """Ghost run: a FlagFound but NO run.finished (killed before emitting it). For a
    single-flag run a found flag IS a win — solved must remain True after restart,
    otherwise the rail would wrongly flip a solved run back to unsolved."""
    store = SessionStore(root=tmp_path)
    _write(tmp_path, "run-ghost", [
        {"event_type": "run.started",
         "payload": {"challenge": {"name": "single", "category": "crypto"}}},
        {"event_type": "insight.event",
         "payload": {"kind": "FlagFound", "flag": "flag{got_it}"}},
        # no run.finished
    ])
    s = store.summary("run-ghost")
    assert s["solved"] is True
    assert s["flag"] == "flag{got_it}"
    assert s["flags"] == ["flag{got_it}"]
    assert s["expected_flags"] == 1
    assert s["multi_flag"] is False


def test_summary_multi_flag_ghost_partial_not_solved(tmp_path: Path) -> None:
    """Ghost multi-flag run (no run.finished) with only 1/2 flags must NOT be solved —
    a partial multi-flag set is not a win even without a terminal event."""
    store = SessionStore(root=tmp_path)
    _write(tmp_path, "run-mfg", [
        {"event_type": "run.started",
         "payload": {"challenge": {"expected_flags": 2, "multi_flag": True}}},
        {"event_type": "insight.event", "payload": {"kind": "FlagFound", "flag": "a"}},
        # no run.finished, only 1 of 2
    ])
    s = store.summary("run-mfg")
    assert s["solved"] is False
    assert s["flags"] == ["a"]
    assert s["expected_flags"] == 2
    assert s["multi_flag"] is True


def test_summary_finished_verdict_overrides_flagfound(tmp_path: Path) -> None:
    """An explicit run.finished solved=False is authoritative even if a FlagFound was
    emitted earlier (single-flag): the run was reopened / flag invalidated."""
    store = SessionStore(root=tmp_path)
    _write(tmp_path, "run-inv", [
        {"event_type": "run.started", "payload": {"challenge": {}}},
        {"event_type": "insight.event", "payload": {"kind": "FlagFound", "flag": "x"}},
        {"event_type": "run.finished", "payload": {"solved": False}},
    ])
    s = store.summary("run-inv")
    assert s["solved"] is False


def test_summary_resolve_reopen_preserves_prior_flags(tmp_path: Path) -> None:
    """A resolve/continue reopen makes the run active again but must not erase the
    already recovered multi-flag results from durable history."""
    store = SessionStore(root=tmp_path)
    _write(tmp_path, "run-resolve", [
        {"event_type": "run.started",
         "payload": {"challenge": {"expected_flags": 3, "multi_flag": True}}},
        {"event_type": "run.finished",
         "payload": {"solved": True, "flags": ["flag{a}", "flag{b}"],
                     "expected_flags": 3, "multi_flag": True}},
        {"event_type": "run.reopened", "payload": {"reason": "resolve"}},
    ])
    s = store.summary("run-resolve")
    assert s["finished"] is False
    assert s["solved"] is False
    assert s["flags"] == ["flag{a}", "flag{b}"]
    assert s["flag"] == "flag{a}"


def test_summary_folds_role_scoped_provider_block_health(tmp_path: Path) -> None:
    """After a restart the rail must reproduce the live health classification:
    a Reason/planner provider block is reason_blocked (planner loop halted), a
    Worker provider block is only degraded — not the other way round."""
    store = SessionStore(root=tmp_path)
    _write(tmp_path, "run-health", [
        {"event_type": "blackboard.delta", "payload": {
            "kind": "provider_recovery_blocked", "role": "reason",
            "reason": "probe_failed", "attempt": 2}},
        {"event_type": "blackboard.delta", "payload": {
            "kind": "runtime_degraded", "engine": "pi", "reason": "provider_timeout"}},
    ])
    s = store.summary("run-health")
    assert s["reason_blocked"] is True
    assert s["reason_blocked_reason"] == "probe_failed"
    assert s["degraded"] is True
    assert s["degraded_reason"] == "provider_timeout"

    _write(tmp_path, "run-health-worker", [
        {"event_type": "blackboard.delta", "payload": {
            "kind": "provider_recovery_blocked", "role": "worker",
            "blocked_reason": "no_healthy_candidate"}},
    ])
    w = store.summary("run-health-worker")
    assert w["reason_blocked"] is False
    assert w["degraded"] is True
    assert w["degraded_reason"] == "no_healthy_candidate"


def test_summary_recovery_events_clear_block_and_activate_fallback(tmp_path: Path) -> None:
    """Append-only health history must fold to the latest effective state: a
    recovered Reason clears the block, and an activated fallback supersedes a
    previous degradation instead of leaving both latched."""
    store = SessionStore(root=tmp_path)
    _write(tmp_path, "run-recover", [
        {"event_type": "blackboard.delta", "payload": {
            "kind": "reason_blocked", "stage": "reason", "reason": "planner unavailable"}},
        {"event_type": "blackboard.delta", "payload": {
            "kind": "provider_recovery_activated", "role": "reason",
            "from": "deepseek-v4-pro", "to": "deepseek-v4-flash",
            "reason": "default_binding_verified"}},
    ])
    s = store.summary("run-recover")
    assert s["reason_blocked"] is True  # history retained
    assert s["fallback_active"] is True
    assert s["fallback_reason"] == "default_binding_verified"
    assert s["degraded"] is False

    _write(tmp_path, "run-recovered", [
        {"event_type": "blackboard.delta", "payload": {
            "kind": "reason_blocked", "stage": "reason", "reason": "planner unavailable"}},
        {"event_type": "blackboard.delta", "payload": {
            "kind": "reason_recovered", "reason": "planner healthy"}},
    ])
    r = store.summary("run-recovered")
    assert r["reason_blocked"] is False
    assert r["reason_blocked_reason"] == ""


def test_summary_false_positive_reopen_drops_only_target_flag(tmp_path: Path) -> None:
    """A per-flag false-positive reopen removes only the targeted bad flag when the
    rail is rebuilt from JSONL after a server restart."""
    store = SessionStore(root=tmp_path)
    _write(tmp_path, "run-fp", [
        {"event_type": "run.started",
         "payload": {"challenge": {"expected_flags": 3, "multi_flag": True}}},
        {"event_type": "run.finished",
         "payload": {"solved": True, "flags": ["flag{a}", "flag{b}", "flag{c}"],
                     "expected_flags": 3, "multi_flag": True}},
        {"event_type": "run.reopened", "payload": {"flag": "flag{b}"}},
    ])
    s = store.summary("run-fp")
    assert s["finished"] is False
    assert s["solved"] is False
    assert s["flags"] == ["flag{a}", "flag{c}"]
    assert s["flag"] == "flag{a}"


def test_summary_empty_run_defaults(tmp_path: Path) -> None:
    """A run with no events yet returns safe defaults including the new fields."""
    store = SessionStore(root=tmp_path)
    _write(tmp_path, "run-empty", [])
    s = store.summary("run-empty")
    assert s["flags"] == []
    assert s["expected_flags"] == 1
    assert s["multi_flag"] is False
    assert s["solved"] is False


def test_replay_monotonic_repairs_seq_reset(tmp_path: Path) -> None:
    """Durable SSE replay must repair historical raw seq resets.

    A backend restart/continue path once produced JSONL like 1,2,3,1,2. The
    browser uses Last-Event-ID as a stream cursor, so replay must expose that
    as 1,2,3,4,5 without rewriting the file.
    """
    store = SessionStore(root=tmp_path)
    _write(tmp_path, "run-reset", [
        {"event_type": "run.started", "seq": 1, "payload": {}},
        {"event_type": "reasoning.delta", "seq": 2, "payload": {"text": "before"}},
        {"event_type": "run.finished", "seq": 3, "payload": {}},
        {"event_type": "run.reopened", "seq": 1, "payload": {"reason": "resolve"}},
        {"event_type": "reasoning.delta", "seq": 2, "payload": {"text": "after"}},
    ])

    async def _collect():
        return [ev async for ev in store.replay_monotonic("run-reset", after_seq=3)]

    replayed = asyncio.run(_collect())
    assert store.last_stream_seq("run-reset") == 5
    assert [ev.seq for ev in replayed] == [4, 5]
    assert [ev.event_type.value for ev in replayed] == ["run.reopened", "reasoning.delta"]
