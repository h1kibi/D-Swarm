"""Engine/runtime degradation announcements."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from dswarm.core.events import Event, EventType, blackboard_delta_payload
from dswarm.solver.types import SolveOutcome


class RuntimeDegradationMixin:
    def _note_engine_degraded(self, engine: str, reason: str, *, role: str) -> None:
        reason = (reason or "health check failed")[:300]
        if self._degraded_engines.get(engine) == reason:
            return
        self._degraded_engines[engine] = reason
        payload = {
            "engine": engine,
            "status": "degraded",
            "reason": reason,
            "role": role,
        }
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._emit_engine_degraded(payload))
        except RuntimeError:
            pass

    def _note_engine_recovered(self, engine: str) -> None:
        if engine not in self._degraded_engines:
            return
        self._degraded_engines.pop(engine, None)
        payload = {"engine": engine, "status": "recovered", "reason": ""}
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._emit_engine_degraded(payload))
        except RuntimeError:
            pass

    async def _emit_engine_degraded(self, payload: dict[str, Any]) -> None:
        if self.bus is None:
            return
        try:
            await self.bus.emit(Event(
                event_type=EventType.BLACKBOARD_DELTA,
                run_id=self.run_id,
                challenge_id=self.challenge.id,
                payload=blackboard_delta_payload(
                    "engine_degraded", actor="coordinator", **payload),
            ))
        except Exception:
            pass

    async def _emit_runtime_degraded(self, payload: dict[str, Any]) -> None:
        if self.bus is None:
            return
        try:
            await self.bus.emit(Event(
                event_type=EventType.BLACKBOARD_DELTA,
                run_id=self.run_id,
                challenge_id=self.challenge.id,
                payload=blackboard_delta_payload(
                    "runtime_degraded", actor="coordinator", **payload),
            ))
        except Exception:
            pass

    def _record_runtime_degraded(
        self,
        *,
        engine: str,
        profile: "Optional[dict]",
        reason: str,
        requested_backend: str,
        fallback_backend: str = "local",
    ) -> None:
        runtime = self._runtime_for_engine(engine, profile) or {}
        payload = {
            "engine": engine,
            "profile": (profile or {}).get("name") or (profile or {}).get("id") or "",
            "runtime": runtime.get("id") or "",
            "requested_backend": requested_backend,
            "backend": fallback_backend,
            "status": "degraded",
            "reason": reason[:300],
        }
        self._runtime_degraded.append(payload)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._emit_runtime_degraded(payload))
        except RuntimeError:
            pass

    def _runtime_metadata_for(self, outcome: "Optional[SolveOutcome]" = None) -> dict[str, Any]:
        engine = getattr(outcome, "engine", "") if outcome is not None else ""
        profile = self._profile_for_engine(engine, advance=False) if engine else None
        runtime = self._runtime_for_engine(engine, profile) if engine else None
        return {
            "backend": "local" if self._runtime_degraded else self.worker_backend,
            "runtime": (runtime or {}).get("id") or "",
            "runtime_degraded": list(self._runtime_degraded),
        }
