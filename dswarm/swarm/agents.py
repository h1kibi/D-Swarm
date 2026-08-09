"""Agent execution profiles and Reason dispatch decisions.

Unlike the original Pentest-Swarm-AI implementation, agents do not trigger
themselves. Reason is the central planner; these profiles describe the worker
capabilities Reason can dispatch to, and the scheduler executes them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from dswarm.solver.worker_profiles import DIRECTIONS


@dataclass
class AgentProfile:
    id: str
    worker_profile: str = ""
    mode: str = "explore"
    timeout: int = 720
    max_concurrency: int = 1
    cleanup_actions: list[str] = field(default_factory=list)
    prompt: str = ""

    def resolve_worker_profile(self, category: str) -> str:
        if self.worker_profile:
            return self.worker_profile
        return "pi-worker"


@dataclass
class DispatchDecision:
    intent_id: str
    profile: str
    goal: str
    from_facts: list[int] = field(default_factory=list)
    direction: str = ""
    mode: str = "explore"
    priority: float = 0.5
    dedupe_key: str = ""
    timeout: int = 720
    resource_keys: list[str] = field(default_factory=list)
    surface_target: str = ""
    task_kind: str = ""
    host_scan: bool = False


class AgentRegistry:
    def __init__(self, profiles: Optional[list[AgentProfile]] = None) -> None:
        self._profiles: dict[str, AgentProfile] = {
            p.id: p for p in (profiles or self.default_profiles())
        }

    def resolve(self, profile_id: str) -> AgentProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            if "pi-worker" in self._profiles:
                return self._profiles["pi-worker"]
            raise KeyError(f"unknown agent profile: {profile_id}") from exc

    def names(self) -> list[str]:
        return list(self._profiles)

    @staticmethod
    def default_profiles() -> list[AgentProfile]:
        profiles = [
            AgentProfile(
                id="pi-worker",
                worker_profile="pi-worker",
                mode="explore",
                timeout=720,
                max_concurrency=3,
            ),
        ]
        # one direction profile per pi direction: the Reason scheduler routes a
        # composite intent to the matching profile (own image/prompt/skills).
        profiles.extend(
            AgentProfile(
                id=f"pi-{direction}",
                worker_profile=f"pi-{direction}",
                mode="explore",
                timeout=720,
                max_concurrency=3,
            )
            for direction in DIRECTIONS
        )
        return profiles
