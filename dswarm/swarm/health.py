"""Worker health probe cache and per-profile probe helpers."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

from dswarm.solver.credential_accounts import runtime_env_for_engine
from dswarm.solver.worker_profiles import base_engine_for_profile

_HEALTH_PROBE_CACHE: dict[tuple, "tuple[float, bool, str]"] = {}
_HEALTH_FAILURE_TTL_FRACTION = 0.25


def _health_cache_get(key: tuple, ttl: float, now: float) -> "tuple[bool, str] | None":
    """Return the cached (ok, detail) for `key` if still fresh, else None."""
    hit = _HEALTH_PROBE_CACHE.get(key)
    if hit is None:
        return None
    stamped, ok, detail = hit
    horizon = ttl if ok else ttl * _HEALTH_FAILURE_TTL_FRACTION
    if now - stamped > horizon:
        return None
    return ok, detail


def _health_cache_put(key: tuple, ok: bool, detail: str, now: float) -> None:
    _HEALTH_PROBE_CACHE[key] = (now, ok, detail)


def _health_cache_clear() -> None:
    _HEALTH_PROBE_CACHE.clear()


class HealthMixin:
    def _health_probe_key(self, name: str, role: str) -> tuple:
        try:
            profile = self._profile_for_engine(name, role=role, advance=False)
        except Exception:  # noqa: BLE001
            profile = None
        base = base_engine_for_profile(profile) if profile else name
        profile_id = str((profile or {}).get("id") or "")
        account = str((profile or {}).get("credential_account") or "")
        return (base, profile_id, account, str(self.credential_accounts_root or ""))

    def _probe_engine_health(self, name: str, role: str) -> "tuple[bool, str]":
        from dswarm.core.runtime_env import is_web_container
        from dswarm.solver.cli_driver import driver_for

        try:
            profile = self._profile_for_engine(name, role=role, advance=False)
            if self._backend_for_engine(name, profile) == "container":
                return True, "deferred to worker container"
            if is_web_container():
                return True, "deferred to worker container"
            base = base_engine_for_profile(profile) if profile else name
            overlay = runtime_env_for_engine(
                base,
                account_root=self.credential_accounts_root,
                account_id=(profile.get("credential_account") if profile else None),
                container=False,
            ).env
            env = {**os.environ, **overlay}
            return driver_for(profile or name).health_detail(env=env)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:160]

    def _healthy_engines(self, *, role: str = "bootstrap") -> list[str]:
        import time

        now = time.monotonic()
        ttl = self._health_probe_ttl
        candidates = [e for e in self.engines if self._engine_available_for_role(e, role)]

        results: dict[str, "tuple[bool, str]"] = {}
        to_probe: list[str] = []
        for e in candidates:
            cached = (
                _health_cache_get(self._health_probe_key(e, role), ttl, now)
                if ttl > 0 else None
            )
            if cached is not None:
                results[e] = cached
            else:
                to_probe.append(e)

        if to_probe:
            if len(to_probe) == 1:
                fresh = {to_probe[0]: self._probe_engine_health(to_probe[0], role)}
            else:
                with ThreadPoolExecutor(max_workers=len(to_probe)) as pool:
                    fresh = dict(zip(
                        to_probe,
                        pool.map(lambda e: self._probe_engine_health(e, role), to_probe),
                    ))
            for e, verdict in fresh.items():
                if ttl > 0:
                    _health_cache_put(self._health_probe_key(e, role), verdict[0],
                                      verdict[1], now)
                results[e] = verdict

        engines: list[str] = []
        for e in candidates:
            healthy, detail = results[e]
            if healthy:
                engines.append(e)
                self._note_engine_recovered(e)
            else:
                self._note_engine_degraded(e, detail or "health check failed", role=role)
        if engines:
            return engines
        return ["pi"] if not self.worker_profiles else []

    async def _healthy_engines_async(self, *, role: str = "bootstrap") -> list[str]:
        try:
            return await asyncio.to_thread(self._healthy_engines, role=role)
        except TypeError:
            return await asyncio.to_thread(self._healthy_engines)
