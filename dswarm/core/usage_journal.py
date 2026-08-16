"""Crash-recovery journal for provider usage calls.

The journal is deliberately not the canonical cost ledger.  It records a
run-scoped, fsync-backed started/finished lifecycle so a later ledger rebuild can
recover calls that were billed before their canonical usage event committed.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from dswarm.core.events import Event, EventType

_HEADER = {"format": "usage-journal", "version": 1}
_PROVIDERS = frozenset({"internal", "gateway"})
_PRODUCERS = frozenset({"internal", "gateway", "fallback"})
_RECORD_KINDS = frozenset({"provider_call", "invocation_aggregate"})
_CALL_OUTCOMES = frozenset({
    "succeeded",
    "provider_error",
    "transport_error",
    "timeout",
    "cancelled",
    "interrupted",
})
_USAGE_STATUSES = frozenset({"measured", "estimated", "unknown"})
_PROVIDER_USAGE_STATUSES = frozenset({"measured", "unknown"})
_FALLBACK_USAGE_STATUSES = frozenset({"estimated", "unknown"})

_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.Lock] = {}


def _path_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


def _provider_usage_id(run_id: str, producer: str, provider_call_id: str) -> str:
    return f"usage::{run_id}::{producer}::{provider_call_id}"


def _invocation_usage_id(run_id: str, invocation_id: str) -> str:
    return f"usage::{run_id}::fallback::{invocation_id}"


def _validate_account_ids(
    configured_account_id: str | None, billing_account_id: str | None
) -> None:
    for value in (configured_account_id, billing_account_id):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("account ids must be None or non-empty strings")


class AccountingUnavailable(RuntimeError):
    """Preflight journal durability failed, so an upstream call must not start."""

    status_code = 503
    code = "accounting_unavailable"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.alert_payload = {
            "level": "error",
            "category": self.code,
            "detail": detail,
        }


class UsageJournalCorrupt(RuntimeError):
    """The append-only journal cannot be deterministically folded."""


@dataclass(frozen=True)
class UsageCall:
    """Immutable identity and claims snapshot captured before a provider call."""

    provider_call_id: str
    producer: str
    run_id: str
    challenge_id: str | None
    worker_instance_id: str | None
    solver_id: str | None
    profile_id: str | None
    configured_account_id: str | None
    billing_account_id: str | None

    def __post_init__(self) -> None:
        if not self.provider_call_id:
            raise ValueError("provider_call_id is required")
        if self.producer not in _PROVIDERS:
            raise ValueError(f"journal producer must be one of {sorted(_PROVIDERS)}")
        if not self.run_id:
            raise ValueError("run_id is required")
        _validate_account_ids(
            self.configured_account_id, self.billing_account_id
        )

    @property
    def usage_id(self) -> str:
        return _provider_usage_id(self.run_id, self.producer, self.provider_call_id)


@dataclass(frozen=True)
class InvocationCall:
    """Immutable identity for a non-gateway CLI invocation aggregate."""

    invocation_id: str
    run_id: str
    challenge_id: str | None
    worker_instance_id: str | None
    solver_id: str | None
    profile_id: str | None
    configured_account_id: str | None
    billing_account_id: str | None
    producer: str = "fallback"

    def __post_init__(self) -> None:
        if not self.invocation_id:
            raise ValueError("invocation_id is required")
        if self.producer != "fallback":
            raise ValueError("invocation calls must use fallback producer")
        if not self.run_id:
            raise ValueError("run_id is required")
        _validate_account_ids(
            self.configured_account_id, self.billing_account_id
        )

    @property
    def usage_id(self) -> str:
        return _invocation_usage_id(self.run_id, self.invocation_id)


@dataclass(frozen=True)
class UsageRecord:
    """Terminal canonical usage payload defined by the M5 RFC."""

    usage_id: str
    producer: str
    record_kind: str
    provider_call_id: str | None
    invocation_id: str | None
    run_id: str
    challenge_id: str | None
    worker_instance_id: str | None
    solver_id: str | None
    profile_id: str | None
    configured_account_id: str | None
    billing_account_id: str | None
    call_outcome: str
    usage_status: str
    input_tokens: int | None
    output_tokens: int | None
    usd: float | None

    def __post_init__(self) -> None:
        if self.producer not in {"internal", "gateway", "fallback"}:
            raise ValueError("invalid usage producer")
        if self.record_kind not in _RECORD_KINDS:
            raise ValueError("invalid record_kind")
        if self.call_outcome not in _CALL_OUTCOMES:
            raise ValueError("invalid call_outcome")
        if self.usage_status not in _USAGE_STATUSES:
            raise ValueError("invalid usage_status")
        _validate_account_ids(
            self.configured_account_id, self.billing_account_id
        )

        if self.record_kind == "provider_call":
            if self.producer not in _PROVIDERS:
                raise ValueError("provider_call requires internal or gateway producer")
            if self.usage_status not in _PROVIDER_USAGE_STATUSES:
                raise ValueError("usage_status is incompatible with producer")
            if not self.provider_call_id or self.invocation_id is not None:
                raise ValueError("provider_call requires only provider_call_id")
            expected_id = _provider_usage_id(
                self.run_id, self.producer, self.provider_call_id
            )
        else:
            if self.producer != "fallback":
                raise ValueError("invocation_aggregate must use fallback producer")
            if self.usage_status not in _FALLBACK_USAGE_STATUSES:
                raise ValueError("usage_status is incompatible with producer")
            if not self.invocation_id or self.provider_call_id is not None:
                raise ValueError("invocation_aggregate requires only invocation_id")
            expected_id = _invocation_usage_id(self.run_id, self.invocation_id)
        if self.usage_id != expected_id:
            raise ValueError("usage_id does not match its canonical identity")

        if self.usage_status == "unknown" and any(
            value is not None
            for value in (self.input_tokens, self.output_tokens, self.usd)
        ):
            raise ValueError("unknown usage must keep tokens and usd as None")
        for value in (self.input_tokens, self.output_tokens):
            if value is not None and value < 0:
                raise ValueError("token counts cannot be negative")
        if self.usd is not None and self.usd < 0:
            raise ValueError("usd cannot be negative")

    @classmethod
    def from_call(
        cls,
        call: UsageCall | InvocationCall,
        *,
        call_outcome: str,
        usage_status: str,
        input_tokens: int | None,
        output_tokens: int | None,
        usd: float | None,
    ) -> UsageRecord:
        return cls(
            usage_id=call.usage_id,
            producer=call.producer,
            record_kind="provider_call",
            provider_call_id=call.provider_call_id,
            invocation_id=None,
            run_id=call.run_id,
            challenge_id=call.challenge_id,
            worker_instance_id=call.worker_instance_id,
            solver_id=call.solver_id,
            profile_id=call.profile_id,
            configured_account_id=call.configured_account_id,
            billing_account_id=call.billing_account_id,
            call_outcome=call_outcome,
            usage_status=usage_status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usd=usd,
        )

    @classmethod
    def from_invocation(
        cls,
        invocation: InvocationCall,
        *,
        call_outcome: str,
        usage_status: str,
        input_tokens: int | None,
        output_tokens: int | None,
        usd: float | None,
    ) -> UsageRecord:
        return cls(
            usage_id=invocation.usage_id,
            producer="fallback",
            record_kind="invocation_aggregate",
            provider_call_id=None,
            invocation_id=invocation.invocation_id,
            run_id=invocation.run_id,
            challenge_id=invocation.challenge_id,
            worker_instance_id=invocation.worker_instance_id,
            solver_id=invocation.solver_id,
            profile_id=invocation.profile_id,
            configured_account_id=invocation.configured_account_id,
            billing_account_id=invocation.billing_account_id,
            call_outcome=call_outcome,
            usage_status=usage_status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usd=usd,
        )


class UsageJournal:
    """Run-scoped fsync-backed JSONL journal shared by all producers."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = _path_lock(self.path)

    def append_started(self, call: UsageCall | InvocationCall) -> None:
        row = {"phase": "started", "ts": time.time(), "usage_id": call.usage_id}
        if isinstance(call, InvocationCall):
            row.update({"record_kind": "invocation_aggregate", **asdict(call)})
        else:
            row.update(asdict(call))
        try:
            self._append_row(row)
        except OSError as exc:
            raise AccountingUnavailable(str(exc)) from exc

    def append_finished(self, record: UsageRecord) -> None:
        row = {"phase": "finished", "ts": time.time(), **asdict(record)}
        self._append_row(row)

    def _append_row(self, row: dict[str, Any]) -> None:
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        header = json.dumps(_HEADER, separators=(",", ":")) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            needs_header = not self.path.exists() or self.path.stat().st_size == 0
            with self.path.open("a", encoding="utf-8") as handle:
                if needs_header:
                    handle.write(header)
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())

    def reconcile(self, canonical_usage_ids: set[str]) -> list[UsageRecord]:
        """Fold journal lifecycles missing from the canonical usage ledger."""
        rows = self._read_rows()
        if not rows:
            return []

        starts: dict[str, UsageCall | InvocationCall] = {}
        terminals: dict[str, UsageRecord] = {}
        order: list[str] = []
        for row in rows:
            phase = row.get("phase")
            if phase == "started":
                call = self._call_from_row(row)
                key = self._identity_key(call)
                previous = starts.get(key)
                if previous is not None and previous != call:
                    raise UsageJournalCorrupt("conflicting duplicate started record")
                if previous is None:
                    starts[key] = call
                    order.append(key)
            elif phase == "finished":
                terminal = self._record_from_row(row)
                key = self._identity_key(terminal)
                previous = terminals.get(key)
                if previous is not None and previous != terminal:
                    raise UsageJournalCorrupt("conflicting duplicate finished record")
                terminals.setdefault(key, terminal)
            else:
                raise UsageJournalCorrupt(f"unknown journal phase: {phase!r}")

        orphaned = set(terminals).difference(starts)
        if orphaned:
            raise UsageJournalCorrupt("finished record has no matching started record")

        pending: list[UsageRecord] = []
        for key in order:
            call = starts[key]
            if call.usage_id in canonical_usage_ids:
                continue
            terminal = terminals.get(key)
            if terminal is None:
                if isinstance(call, InvocationCall):
                    terminal = UsageRecord.from_invocation(
                        call,
                        call_outcome="interrupted",
                        usage_status="unknown",
                        input_tokens=None,
                        output_tokens=None,
                        usd=None,
                    )
                else:
                    terminal = UsageRecord.from_call(
                        call,
                        call_outcome="interrupted",
                        usage_status="unknown",
                        input_tokens=None,
                        output_tokens=None,
                        usd=None,
                    )
            else:
                if isinstance(call, InvocationCall):
                    expected = UsageRecord.from_invocation(
                        call,
                        call_outcome=terminal.call_outcome,
                        usage_status=terminal.usage_status,
                        input_tokens=terminal.input_tokens,
                        output_tokens=terminal.output_tokens,
                        usd=terminal.usd,
                    )
                else:
                    expected = UsageRecord.from_call(
                        call,
                        call_outcome=terminal.call_outcome,
                        usage_status=terminal.usage_status,
                        input_tokens=terminal.input_tokens,
                        output_tokens=terminal.output_tokens,
                        usd=terminal.usd,
                    )
                if terminal != expected:
                    raise UsageJournalCorrupt(
                        "finished record identity does not match started claims"
                    )
            pending.append(terminal)
        return pending

    def _read_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            if not self.path.exists() or self.path.stat().st_size == 0:
                return []
            with self.path.open("r", encoding="utf-8") as handle:
                raw_lines = [line.strip() for line in handle if line.strip()]
        if not raw_lines:
            return []
        try:
            header = json.loads(raw_lines[0])
        except json.JSONDecodeError as exc:
            raise UsageJournalCorrupt("invalid journal header") from exc
        if header != _HEADER:
            raise UsageJournalCorrupt("unsupported usage journal format")

        rows: list[dict[str, Any]] = []
        for raw in raw_lines[1:]:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise UsageJournalCorrupt("invalid journal row") from exc
            if not isinstance(value, dict):
                raise UsageJournalCorrupt("journal row must be an object")
            rows.append(value)
        return rows

    @staticmethod
    def _identity_key(value: UsageCall | InvocationCall | UsageRecord) -> str:
        if isinstance(value, InvocationCall):
            return value.invocation_id
        if isinstance(value, UsageCall):
            return value.provider_call_id
        if value.record_kind == "invocation_aggregate":
            if not value.invocation_id:
                raise UsageJournalCorrupt("invocation aggregate missing invocation_id")
            return value.invocation_id
        if not value.provider_call_id:
            raise UsageJournalCorrupt("provider call missing provider_call_id")
        return value.provider_call_id

    @staticmethod
    def _call_from_row(row: dict[str, Any]) -> UsageCall | InvocationCall:
        try:
            if row.get("record_kind") == "invocation_aggregate":
                call: UsageCall | InvocationCall = InvocationCall(
                    invocation_id=str(row["invocation_id"]),
                    run_id=str(row["run_id"]),
                    challenge_id=row.get("challenge_id"),
                    worker_instance_id=row.get("worker_instance_id"),
                    solver_id=row.get("solver_id"),
                    profile_id=row.get("profile_id"),
                    configured_account_id=row.get("configured_account_id"),
                    billing_account_id=row.get("billing_account_id"),
                )
            else:
                call = UsageCall(
                    provider_call_id=str(row["provider_call_id"]),
                    producer=str(row["producer"]),
                    run_id=str(row["run_id"]),
                    challenge_id=row.get("challenge_id"),
                    worker_instance_id=row.get("worker_instance_id"),
                    solver_id=row.get("solver_id"),
                    profile_id=row.get("profile_id"),
                    configured_account_id=row.get("configured_account_id"),
                    billing_account_id=row.get("billing_account_id"),
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise UsageJournalCorrupt("invalid started usage record") from exc
        if row.get("usage_id") != call.usage_id:
            raise UsageJournalCorrupt("started usage_id does not match identity")
        return call

    @staticmethod
    def _record_from_row(row: dict[str, Any]) -> UsageRecord:
        fields = {
            name: row.get(name)
            for name in UsageRecord.__dataclass_fields__
        }
        try:
            return UsageRecord(**fields)
        except (TypeError, ValueError) as exc:
            raise UsageJournalCorrupt("invalid finished usage record") from exc


@dataclass(frozen=True)
class UsageContext:
    """Immutable identity used by an internal provider-call producer."""

    run_id: str
    challenge_id: str | None = None
    worker_instance_id: str | None = None
    solver_id: str | None = None
    profile_id: str | None = None
    configured_account_id: str | None = None
    billing_account_id: str | None = None
    producer: str = "internal"

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id is required")
        if self.producer not in _PRODUCERS:
            raise ValueError("UsageContext producer must be internal, gateway, or fallback")
        _validate_account_ids(self.configured_account_id, self.billing_account_id)


class UsageWriter:
    """Durable started/finished writer and canonical event bridge.

    The journal write is the crash-recovery boundary.  When a bus is supplied,
    the terminal record is then published through ``emit_checked`` so consumers
    never observe a usage record that was not durably written first.
    """

    def __init__(
        self,
        journal: UsageJournal,
        *,
        bus: Any = None,
        context: UsageContext | None = None,
    ) -> None:
        self.journal = journal
        self.bus = bus
        self.context = context

    async def start(
        self,
        *,
        context: UsageContext | None = None,
        provider_call_id: str | None = None,
    ) -> UsageCall | InvocationCall:
        ctx = context or self.context
        if ctx is None:
            raise ValueError("UsageWriter.start requires a UsageContext")
        if ctx.producer == "fallback":
            call: UsageCall | InvocationCall = InvocationCall(
                invocation_id=provider_call_id or uuid.uuid4().hex,
                run_id=ctx.run_id,
                challenge_id=ctx.challenge_id,
                worker_instance_id=ctx.worker_instance_id,
                solver_id=ctx.solver_id,
                profile_id=ctx.profile_id,
                configured_account_id=ctx.configured_account_id,
                billing_account_id=ctx.billing_account_id,
            )
        else:
            call = UsageCall(
                provider_call_id=provider_call_id or uuid.uuid4().hex,
                producer=ctx.producer,
                run_id=ctx.run_id,
                challenge_id=ctx.challenge_id,
                worker_instance_id=ctx.worker_instance_id,
                solver_id=ctx.solver_id,
                profile_id=ctx.profile_id,
                configured_account_id=ctx.configured_account_id,
                billing_account_id=ctx.billing_account_id,
            )
        self.journal.append_started(call)
        return call

    async def finish(
        self,
        call: UsageCall,
        *,
        call_outcome: str,
        usage_status: str,
        usage: dict[str, Any] | None = None,
    ) -> UsageRecord:
        usage = usage or {}
        if usage_status == "unknown":
            input_tokens = output_tokens = usd = None
        else:
            input_tokens = usage.get("prompt_tokens")
            output_tokens = usage.get("completion_tokens")
            usd = usage.get("usd")
        if isinstance(call, InvocationCall):
            record = UsageRecord.from_invocation(
                call,
                call_outcome=call_outcome,
                usage_status=usage_status,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                usd=usd,
            )
        else:
            record = UsageRecord.from_call(
                call,
                call_outcome=call_outcome,
                usage_status=usage_status,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                usd=usd,
            )
        self.journal.append_finished(record)
        if self.bus is not None:
            await self.bus.emit_checked(
                Event(
                    event_type=EventType.USAGE_RECORDED,
                    run_id=record.run_id,
                    challenge_id=record.challenge_id,
                    solver_id=record.solver_id,
                    payload=asdict(record),
                )
            )
        return record
