"""Replayable usage ledger and spawn readiness gate.

The UsageJournal is the crash-recovery write-ahead record.  This module folds
canonical ``USAGE_RECORDED`` events (plus journal terminals that have not yet
made it to the event log) into deterministic run-scoped projections.  It is
intentionally independent from the legacy CostController so the existing
COST_UPDATE consumer contract remains unchanged.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Iterable

from dswarm.core.events import Event, EventType
from dswarm.core.usage_journal import UsageJournal, UsageRecord


class LedgerNotReady(RuntimeError):
    """A provider call or worker spawn was attempted before the ledger was ready."""


@dataclass
class LedgerTotals:
    calls: int = 0
    unknown_calls: int = 0
    estimated_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tokens: int = 0
    usd: float = 0.0

    def add(self, record: UsageRecord) -> None:
        self.calls += 1
        if record.usage_status == "unknown":
            self.unknown_calls += 1
        elif record.usage_status == "estimated":
            self.estimated_calls += 1
        if record.input_tokens is not None:
            self.input_tokens += record.input_tokens
        if record.output_tokens is not None:
            self.output_tokens += record.output_tokens
        self.tokens = self.input_tokens + self.output_tokens
        if record.usd is not None:
            self.usd += record.usd

    def snapshot(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "unknown_calls": self.unknown_calls,
            "estimated_calls": self.estimated_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tokens": self.tokens,
            "usd": round(self.usd, 6),
        }


@dataclass
class UsageLedger:
    """A deterministic, idempotent projection of canonical usage events."""

    run_id: str
    state: str = "ready"
    ledger_error: str | None = None
    records: dict[str, UsageRecord] = field(default_factory=dict)
    budget_actions: list[dict[str, Any]] = field(default_factory=list)
    recovery_records: dict[str, UsageRecord] = field(default_factory=dict)
    _global: LedgerTotals = field(default_factory=LedgerTotals, init=False)
    _challenge: dict[str, LedgerTotals] = field(default_factory=dict, init=False)
    _solver: dict[str, LedgerTotals] = field(default_factory=dict, init=False)
    _profile: dict[str, LedgerTotals] = field(default_factory=dict, init=False)
    _account: dict[str, LedgerTotals] = field(default_factory=dict, init=False)

    def mark_rebuilding(self) -> None:
        self.state = "rebuilding"
        self.ledger_error = None

    def mark_failed(self, error: str) -> None:
        self.state = "failed"
        self.ledger_error = str(error)

    def mark_ready(self) -> None:
        self.state = "ready"
        self.ledger_error = None

    def _reset_projection(self) -> None:
        self.records.clear()
        self.budget_actions.clear()
        self.recovery_records.clear()
        self._global = LedgerTotals()
        self._challenge.clear()
        self._solver.clear()
        self._profile.clear()
        self._account.clear()

    @staticmethod
    def _payload_record(event: Event) -> UsageRecord:
        try:
            return UsageRecord(**dict(event.payload or {}))
        except Exception as exc:
            raise LedgerNotReady(f"invalid usage event: {exc}") from exc

    @staticmethod
    def _add(mapping: dict[str, LedgerTotals], key: str | None, record: UsageRecord) -> None:
        if key:
            mapping.setdefault(key, LedgerTotals()).add(record)

    def apply_record(self, record: UsageRecord) -> bool:
        """Apply one record; return False for an identical idempotent replay."""
        if record.run_id != self.run_id:
            raise ValueError("usage record belongs to another run")
        previous = self.records.get(record.usage_id)
        if previous is not None:
            if previous != record:
                raise LedgerNotReady(f"conflicting usage_id: {record.usage_id}")
            return False
        self.records[record.usage_id] = record
        self._global.add(record)
        self._add(self._challenge, record.challenge_id, record)
        self._add(self._solver, record.solver_id, record)
        self._add(self._profile, record.profile_id, record)
        self._add(self._account, record.billing_account_id, record)
        return True

    def apply_event(self, event: Event) -> bool:
        if event.run_id != self.run_id:
            return False
        if event.event_type is EventType.USAGE_RECORDED:
            return self.apply_record(self._payload_record(event))
        if event.event_type is EventType.BUDGET_ACTION:
            self.budget_actions.append(dict(event.payload or {}))
            return True
        return False

    def rebuild(
        self,
        events: Iterable[Event],
        *,
        journal: UsageJournal | None = None,
    ) -> int:
        """Replay canonical events and reconcile journal terminals.

        Returns the number of journal records recovered into the projection.
        Rebuild is deliberately all-or-fail: a malformed event or journal leaves
        the ledger in ``failed`` so callers can stop new provider calls/spawns.
        """
        self.mark_rebuilding()
        self._reset_projection()
        try:
            for event in events:
                self.apply_event(event)
            recovered = 0
            if journal is not None:
                pending = journal.reconcile(set(self.records))
                for record in pending:
                    self.apply_record(record)
                    self.recovery_records[record.usage_id] = record
                    recovered += 1
            self.mark_ready()
            return recovered
        except Exception as exc:
            self.mark_failed(str(exc))
            if isinstance(exc, LedgerNotReady):
                raise
            raise LedgerNotReady(str(exc)) from exc

    def pending_recovery_records(self) -> tuple[UsageRecord, ...]:
        return tuple(self.recovery_records.values())

    def mark_reconciled(self, usage_ids: Iterable[str]) -> None:
        for usage_id in usage_ids:
            self.recovery_records.pop(usage_id, None)

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "state": self.state,
            "ledger_state": self.state,
            "ledger_error": self.ledger_error,
            "global": self._global.snapshot(),
            "challenge": {key: value.snapshot() for key, value in self._challenge.items()},
            "solver": {key: value.snapshot() for key, value in self._solver.items()},
            "profile": {key: value.snapshot() for key, value in self._profile.items()},
            "account": {key: value.snapshot() for key, value in self._account.items()},
            "records": len(self.records),
            "actions": list(self.budget_actions),
        }


class SpawnGuard:
    """Shared readiness gate injected into every run's spawn path."""

    def __init__(self, *, timeout: float = 10.0) -> None:
        self.timeout = max(0.001, float(timeout))
        self.state = "ready"
        self.ledger_error: str | None = None
        self._ready = asyncio.Event()
        self._ready.set()

    @property
    def ledger_state(self) -> str:
        return self.state

    def mark_rebuilding(self) -> None:
        self.state = "rebuilding"
        self.ledger_error = None
        self._ready.clear()

    def mark_ready(self) -> None:
        self.state = "ready"
        self.ledger_error = None
        self._ready.set()

    def mark_failed(self, error: str) -> None:
        self.state = "failed"
        self.ledger_error = str(error)
        self._ready.set()

    def check_now(self, *, operation: str = "spawn") -> None:
        if operation in {"stop", "finalize"}:
            return
        if self.state == "failed":
            raise LedgerNotReady(self.ledger_error or "ledger_failed")
        if self.state != "ready":
            raise LedgerNotReady("ledger_rebuilding")

    async def ensure_ready(
        self,
        run_id: str | None = None,
        *,
        timeout: float | None = None,
        operation: str = "spawn",
    ) -> None:
        if operation in {"stop", "finalize"}:
            return
        if self.state == "ready":
            return
        if self.state == "failed":
            suffix = f" for {run_id}" if run_id else ""
            raise LedgerNotReady(f"{self.ledger_error or 'ledger_failed'}{suffix}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self.timeout if timeout is None else timeout)
        except asyncio.TimeoutError as exc:
            self.mark_failed("ledger_rebuild_timeout")
            raise LedgerNotReady("ledger_rebuild_timeout") from exc
        self.check_now(operation=operation)
