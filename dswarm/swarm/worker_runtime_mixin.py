"""Worker runtime: profile selection, environment, container and CLI spawn helpers."""
from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, Optional

from dswarm.core.runtime_env import is_web_container
from dswarm.solver.credential_accounts import runtime_env_for_engine
from dswarm.solver.llm_providers import LLMProviderSecretStore, provider_secret_root, resolve_llm_provider
from dswarm.solver.runtime_policy import RuntimePolicyError
from dswarm.solver.worker_profiles import base_engine_for_profile, direction_profile_name, normalize_profile_roster, normalize_worker_profiles
from dswarm.solver.workspace import ensure_workspace
from dswarm.swarm._bootstrap_assets import (
    _CONTAINER_BLACKBOARD_SKILL,
    _CONTAINER_DIRECTION_PROMPT,
    _CONTAINER_PI_CONFIG,
    _direction_from_profile_id,
    _ensure_base_skill_links,
    _ensure_blackboard_skill_links,
    _ensure_direction_links,
    _ensure_pi_config_links,
    _materialize_runtime_pi_config,
    _repo_direction_root,
)
from dswarm.swarm.budget import WorkerBudgetExhausted
from dswarm.swarm.errors import WorkerSpawnRejected
from dswarm.swarm.lane_gate import WorkerLaneDisabled, WorkerLaneStopped


class WorkerRuntimeMixin:

    def _pick_engine(
        self,
        running_engines: list[str],
        healthy: list[str],
        *,
        role: str = "bootstrap",
        preferred: str = "",
    ) -> str:
        """Heterogeneity-aware engine selection: prefer an engine NOT currently
        running, so each spawned worker covers a different blind spot. Falls back to
        least-loaded when all are running. `preferred` (a profile id from an
        intent's direction) wins when it is healthy and has capacity — this is
        what routes a composite challenge's intents onto their direction
        profiles (own image/prompt/skills)."""
        available = self._healthy_role_candidates(healthy, role=role)
        if not available:
            raise RuntimeError(f"no available worker profile for role={role}")
        preferred_profile = getattr(self, "_profiles_by_name", {}).get(preferred)
        preferred_canonical = preferred
        if preferred_profile:
            preferred_canonical = str(
                preferred_profile.get("name") or preferred_profile.get("id") or preferred
            )
        if preferred_canonical and preferred_canonical in available:
            return preferred_canonical
        for e in available:
            if self._running_count_for_candidate(e, running_engines) == 0:
                return e
        # all healthy engines already running → least-loaded
        return min(available, key=lambda e: self._running_count_for_candidate(e, running_engines))

    @staticmethod
    def _clean_runtime_profiles(value: "Optional[list[dict]]") -> dict[str, dict]:
        out: dict[str, dict] = {}
        if not isinstance(value, list):
            return out
        for item in value:
            if not isinstance(item, dict):
                continue
            rid = str(item.get("id") or "").strip()
            backend = str(item.get("backend") or "").strip()
            if rid and backend in {"local", "container"}:
                out[rid] = {
                    "id": rid,
                    "backend": backend,
                    "network": str(item.get("network") or "bridge"),
                    "memory": str(item.get("memory") or ""),
                    "cpus": str(item.get("cpus") or ""),
                    "pids_limit": int(item.get("pids_limit") or 0),
                }
        return out

    def _runtime_for_engine(self, engine: str, profile: "Optional[dict]" = None) -> "Optional[dict]":
        profile = profile if profile is not None else self._profile_for_engine(engine)
        if not profile:
            return None
        return self.runtime_profiles.get(profile.get("runtime") or "")

    @staticmethod
    def _clean_worker_profiles(value: "Optional[list[dict]]") -> list[dict]:
        return normalize_worker_profiles(value, defaults=[])

    @staticmethod
    def _profile_allows_role(profile: dict, role: "Optional[str]") -> bool:
        if role is None:
            return True
        roles = profile.get("roles") or []
        return role in roles

    def _review_profile_limit(self, profile: dict) -> int:
        raw = profile.get("max_review_running")
        if raw in (None, "", 0):
            raw = self.review_policy.get("max_concurrent", 1)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            fallback = self.review_policy.get("max_concurrent", 1)
            try:
                return max(0, int(fallback))
            except (TypeError, ValueError):
                return 1

    def _profile_available(self, profile: dict, role: "Optional[str]" = None) -> bool:
        pid = profile["id"]
        if role == "review":
            return (
                self._active_review_profile_counts.get(pid, 0)
                < self._review_profile_limit(profile)
            )
        return self._active_profile_counts.get(pid, 0) < int(profile.get("max_running") or 1)

    def _profile_for_engine(
        self,
        engine: str,
        *,
        role: "Optional[str]" = None,
        advance: bool = True,
    ) -> "Optional[dict]":
        if engine in getattr(self, "_profiles_by_name", {}):
            profiles = [self._profiles_by_name[engine]]
        else:
            profiles = self._profiles_by_engine.get(engine) or []
        if not profiles:
            return None
        start = self._profile_rr.get(engine, 0)
        for off in range(len(profiles)):
            idx = (start + off) % len(profiles)
            p = profiles[idx]
            if not self._profile_allows_role(p, role):
                continue
            if not self._profile_available(p, role=role):
                continue
            if advance:
                self._profile_rr[engine] = (idx + 1) % len(profiles)
            return p
        return None

    def _profile_for_direction(self, direction: str) -> str:
        """Profile id for an intent's direction ("" when the direction profile is
        not configured/healthy on this run's roster)."""
        from dswarm.solver.worker_profiles import direction_profile_name

        profile = direction_profile_name(direction)
        if profile and profile in getattr(self, "_profiles_by_name", {}):
            return profile
        return ""

    def _engine_available_for_role(self, engine: str, role: str) -> bool:
        profiles_by_engine = getattr(self, "_profiles_by_engine", {})
        profiles_by_name = getattr(self, "_profiles_by_name", {})
        if engine not in profiles_by_name and engine not in profiles_by_engine:
            return True
        return self._profile_for_engine(engine, role=role, advance=False) is not None

    def _healthy_matches(self, engine_or_profile: str, healthy: list[str]) -> bool:
        """Healthy rosters may contain either base engine ids (claude/codex/cursor)
        or concrete worker profile ids, depending on whether they came from a live
        probe or a caller-supplied health list. Treat both forms as equivalent for
        scheduling decisions."""
        if engine_or_profile in healthy:
            return True
        profile = getattr(self, "_profiles_by_name", {}).get(engine_or_profile)
        base = base_engine_for_profile(profile or engine_or_profile)
        return base in healthy or any(
            base_engine_for_profile(
                getattr(self, "_profiles_by_name", {}).get(h) or h
            ) == base for h in healthy
        )

    def _healthy_role_candidates(self, healthy: list[str], *, role: str) -> list[str]:
        """Configured, healthy, capacity-available scheduling units for `role`.

        When worker profiles are enabled, the scheduler's unit is the profile id
        from settings. Base engine names are only compatibility input to health and
        manual-spawn paths; they are normalized back to the configured profile
        roster before a worker is selected.
        """
        if getattr(self, "worker_profiles", []):
            roster = list(getattr(self, "engines", []))
        else:
            roster = list(healthy)
        out: list[str] = []
        seen: set[str] = set()
        for e in roster:
            if e in seen:
                continue
            seen.add(e)
            if not self._healthy_matches(e, healthy):
                continue
            if self._engine_available_for_role(e, role):
                out.append(e)
        return out

    def _running_count_for_candidate(self, candidate: str, running_engines: list[str]) -> int:
        profile = getattr(self, "_profiles_by_name", {}).get(candidate)
        base = base_engine_for_profile(profile or candidate)
        n = 0
        for running in running_engines:
            if running == candidate:
                n += 1
                continue
            running_profile = getattr(self, "_profiles_by_name", {}).get(running)
            running_base = base_engine_for_profile(running_profile or running)
            if running_base == base:
                n += 1
        return n

    def _claim_worker_account(
        self, solver_id: str, engine: str, profile: "Optional[dict]",
        role: "Optional[str]" = None,
    ) -> None:
        if not profile:
            return
        pid = profile["id"]
        role_bucket = "review" if role == "review" else "worker"
        self._active_profile_by_solver[solver_id] = pid
        self._active_profile_role_by_solver[solver_id] = role_bucket
        if role_bucket == "review":
            self._active_review_profile_counts[pid] = (
                self._active_review_profile_counts.get(pid, 0) + 1)
        else:
            self._active_profile_counts[pid] = self._active_profile_counts.get(pid, 0) + 1
        account_id = profile.get("credential_account")
        if not account_id:
            return
        self._active_account_by_solver[solver_id] = account_id

    def _release_worker_account(self, solver: Any) -> None:
        token = getattr(solver, "gateway_token", None)
        if token:
            try:
                from dswarm.solver.modelgateway import ModelGateway
                ModelGateway.instance().revoke_token(token)
            except Exception:
                pass
            try:
                solver.gateway_token = None
            except Exception:
                pass
        sid = getattr(solver, "solver_id", "")
        pid = self._active_profile_by_solver.pop(sid, None)
        role_bucket = self._active_profile_role_by_solver.pop(sid, "worker")
        if pid:
            if role_bucket == "review":
                self._active_review_profile_counts[pid] = max(
                    0, self._active_review_profile_counts.get(pid, 0) - 1)
            else:
                self._active_profile_counts[pid] = max(
                    0, self._active_profile_counts.get(pid, 0) - 1)
        self._active_account_by_solver.pop(sid, None)

    def _alloc_workdir(self, engine: str) -> "Optional[str]":
        """Carve a fresh per-worker cwd under worker_root, or return None to let
        CliSolver fall back to a system mkdtemp. The monotonic _worker_seq keeps
        multiple workers on the same engine from colliding."""
        if self.worker_root is None:
            return None
        if self.workspace_root is not None:
            ensure_workspace(self.workspace_root, runtime={
                "backend": self.worker_backend,
                "run_id": self.run_id,
            })
        self._worker_seq += 1
        wd = self.worker_root / f"cli-{engine}-{self._worker_seq}"
        try:
            wd.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None  # unwritable → fall back to mkdtemp, never block the run
        return str(wd)

    def _runtime_env_for(
        self,
        engine: str,
        label: str,
        *,
        container: "Optional[object]",
        profile: "Optional[dict]" = None,
        task_token: str | None = None,
        home_and_gateway_only: bool = False,
    ) -> dict[str, str]:
        """Per-worker runtime env: Credential Account plus isolated HOME.

        ``home_and_gateway_only`` (strict M9a lease path): skip the host
        credential-account merge entirely (the lease owns credentials via its
        gateway token) but STILL prepare the isolated HOME (provider config /
        blackboard skill links) and emit HOME/PI_CODING_AGENT_DIR — without
        this, lease workers ran with the image-default HOME and no provider
        config (Unknown provider ctf-gateway).
        """

        explicit_endpoint = bool(
            (profile or {}).get("base_url")
            or (profile or {}).get("api_key_ref")
            or (profile or {}).get("provider_ref")
        )
        source_env = None
        if explicit_endpoint and not home_and_gateway_only:
            source_env = {**os.environ, "DSWARM_PI_PROVIDER": "dswarm-worker"}
        if home_and_gateway_only:
            env: dict[str, str] = {}
        else:
            env = runtime_env_for_engine(
                engine,
                account_root=self.credential_accounts_root,
                account_id=(profile.get("credential_account") if profile else None),
                container=container is not None,
                env=source_env,
            ).env
        if profile:
            provider_ref = str(profile.get("provider_ref") or "").strip()
            resolved_provider = None
            if provider_ref:
                provider_store = None
                if self.workspace_root is not None:
                    provider_store = LLMProviderSecretStore(provider_secret_root(self.workspace_root.parent))
                elif self.credential_accounts_root is not None:
                    # account root is normally sessions/_secrets/accounts
                    provider_store = LLMProviderSecretStore(self.credential_accounts_root.parent / "llm_providers")
                resolved_provider = resolve_llm_provider(provider_ref, self.llm_providers, secret_store=provider_store)
                if resolved_provider is not None:
                    env["DSWARM_PI_PROVIDER"] = "dswarm-worker"
                    if resolved_provider.base_url:
                        env["OPENAI_BASE_URL"] = resolved_provider.base_url
                        env["DSWARM_WORKER_BASE_URL"] = resolved_provider.base_url
                    env["DSWARM_WORKER_WIRE_API"] = resolved_provider.wire_api
                    env["DSWARM_WORKER_AUTH_MODE"] = resolved_provider.auth_mode
                    env["DSWARM_WORKER_AUTH_HEADER"] = resolved_provider.auth_header
                    env["DSWARM_WORKER_AUTH_PREFIX"] = resolved_provider.auth_prefix
                    if resolved_provider.api_key:
                        env["OPENAI_API_KEY"] = resolved_provider.api_key
                        env["DSWARM_WORKER_API_KEY"] = resolved_provider.api_key
            env["DSWARM_WORKER_PROFILE_ID"] = profile["id"]
            env["DSWARM_CREDENTIAL_ACCOUNT_ID"] = profile.get("credential_account", "")
            if provider_ref:
                env["DSWARM_LLM_PROVIDER_REF"] = provider_ref
            if profile.get("model"):
                env["DSWARM_WORKER_MODEL"] = str(profile["model"])
            if profile.get("effort"):
                env["DSWARM_WORKER_THINKING"] = str(profile["effort"])
            if explicit_endpoint and not provider_ref:
                env["DSWARM_PI_PROVIDER"] = "dswarm-worker"
                base_url = str(profile.get("base_url") or "").strip().rstrip("/")
                if base_url:
                    env["OPENAI_BASE_URL"] = base_url
                    env["DSWARM_WORKER_BASE_URL"] = base_url
                env["DSWARM_WORKER_WIRE_API"] = str(profile.get("wire_api") or "auto")
                env["DSWARM_WORKER_AUTH_MODE"] = str(profile.get("auth_mode") or "bearer")
                env["DSWARM_WORKER_AUTH_HEADER"] = str(profile.get("auth_header") or "Authorization")
                env["DSWARM_WORKER_AUTH_PREFIX"] = str(
                    profile.get("auth_prefix") if profile.get("auth_prefix") is not None else "Bearer"
                )
                if env.get("OPENAI_API_KEY"):
                    env["DSWARM_WORKER_API_KEY"] = env["OPENAI_API_KEY"]
                if env.get("OPENAI_API_KEY_FILE"):
                    env["DSWARM_WORKER_API_KEY_FILE"] = env["OPENAI_API_KEY_FILE"]
            direction = _direction_from_profile_id(profile["id"])
            if direction:
                if container is not None:
                    # The direction config layer is baked into the worker image.
                    env["DSWARM_DIRECTION_PROMPT"] = _CONTAINER_DIRECTION_PROMPT
                else:
                    local = _repo_direction_root()
                    if local is not None:
                        prompt = local / direction / "prompt.md"
                        if prompt.exists():
                            env["DSWARM_DIRECTION_PROMPT"] = str(prompt)
        if self.worker_root is not None and (container is not None or home_and_gateway_only):
            base = (self.workspace_root or self.worker_root.parent)
            home_host = base / "homes" / label
            try:
                home_host.mkdir(parents=True, exist_ok=True)
            except OSError:
                return env
            mapper = getattr(container, "to_container_path", None)
            if mapper is None and home_and_gateway_only:
                # strict lease path: the executor does not exist yet at env-build
                # time. The pool container mounts <workspace> at
                # CONTAINER_WORKSPACE, so the mapping is static.
                from dswarm.solver.container_exec import CONTAINER_WORKSPACE

                def mapper(host_path: str) -> str:
                    rel = Path(host_path).resolve().as_posix()
                    host_ws = Path(base).resolve().as_posix()
                    if rel.startswith(host_ws):
                        return CONTAINER_WORKSPACE + rel[len(host_ws):]
                    return host_path
            # The image ships a fallback skill, but the source checkout may have
            # added CLI protocol support since that image was built.  Put a fresh,
            # run-local copy on the bind mount and link pi's auto-discovery to it.
            skill_target = _CONTAINER_BLACKBOARD_SKILL
            try:
                from dswarm.solver.blackboard_skill import materialize_runtime_blackboard_skill
                runtime_skill = materialize_runtime_blackboard_skill(base)
                if runtime_skill is not None and callable(mapper):
                    skill_target = mapper(str(runtime_skill))
                skill_copy_source = runtime_skill
            except Exception:
                skill_copy_source = None
                pass
            _ensure_blackboard_skill_links(
                home_host, skill_target=skill_target,
                copy_source=skill_copy_source)
            _ensure_base_skill_links(home_host)
            pi_config_target = _CONTAINER_PI_CONFIG
            try:
                runtime_pi_config = _materialize_runtime_pi_config(base)
                if runtime_pi_config is not None and callable(mapper):
                    pi_config_target = mapper(str(runtime_pi_config))
                # Windows dev hosts cannot create the HOME symlinks; hand the
                # host-side config dir to the link helper so it can fall back
                # to a real copy inside the bind-mounted HOME.
                pi_config_copy_source = runtime_pi_config
            except Exception:
                pi_config_copy_source = None
                pass
            _ensure_pi_config_links(
                home_host, config_target_root=pi_config_target,
                copy_source=pi_config_copy_source)
            direction = _direction_from_profile_id(
                (profile or {}).get("id", ""))
            if direction:
                _ensure_direction_links(home_host, direction)
            from dswarm.solver.container_exec import _chown_tree_to_worker
            _chown_tree_to_worker(str(home_host))
            if callable(mapper):
                try:
                    env["HOME"] = mapper(str(home_host))
                except Exception:
                    env["HOME"] = str(home_host)
            else:
                env["HOME"] = str(home_host)
            if home_and_gateway_only and not str(env.get("HOME", "")).startswith(
                "/home/kali/workspace"
            ):
                # Windows dev host: the executor mapper's prefix match fails on
                # host path shapes, returning an absolute WINDOWS path, which
                # the control-link env filter then refuses to forward -- pi ran
                # with the supervisor default HOME=/home/kali (no provider
                # config, Unknown provider). The pool container mounts
                # <workspace> at CONTAINER_WORKSPACE, so the container path is
                # statically derivable: force it.
                from dswarm.solver.container_exec import CONTAINER_WORKSPACE

                env["HOME"] = f"{CONTAINER_WORKSPACE}/homes/{label}"
            # pi 0.83+ documents PI_CODING_AGENT_DIR as the authoritative
            # config root. Do not rely on HOME expansion inside the container
            # supervisor: make the isolated run-local config explicit so the
            # dswarm-worker provider extension is discoverable for every worker.
            env["PI_CODING_AGENT_DIR"] = f'{str(env["HOME"]).rstrip("/")}/.pi/agent'
        # route A P3: with a live task token the worker authenticates to the MODEL
        # GATEWAY using the token as its key — the real upstream key stays in the
        # host process. The worker image exposes a models.json ctf-gateway
        # provider that reads DSWARM_TASK_TOKEN / DSWARM_GATEWAY_URL; the explicit
        # DSWARM_PI_PROVIDER keeps selection deterministic. Overrides the account
        # FILE injection (the raw key must never cross into the container).
        if container is not None and task_token and not explicit_endpoint:
            env["DEEPSEEK_API_KEY"] = task_token
            env.pop("DEEPSEEK_API_KEY_FILE", None)
            env["DSWARM_TASK_TOKEN"] = task_token
            env["DSWARM_GATEWAY_URL"] = os.environ.get(
                "DSWARM_GATEWAY_URL",
                f"http://host.docker.internal:{os.environ.get('DSWARM_MODEL_GATEWAY_PORT', '9101')}/v1",
            )
            env["DSWARM_PI_PROVIDER"] = "ctf-gateway"
            # pi 0.81.x resolves the provider FROM the model: a bare `--provider
            # ctf-gateway` (no --model) falls back to the settings default model's
            # provider (models-store deepseek) and 401s with the task token as its
            # key. The gateway provider declares deepseek-v4-flash/pro, so default
            # to flash (a profile's explicit DSWARM_WORKER_MODEL still wins).
            if not env.get("DSWARM_WORKER_MODEL"):
                env["DSWARM_WORKER_MODEL"] = "deepseek-v4-flash"
        # Prefer the run SQLite graph when it is already mounted into the worker.
        # A host coordinator cannot be reached from a bridge-network worker via
        # 127.0.0.1:8000: that address is the worker container itself.  HTTP remains
        # available only when the operator explicitly supplied a URL (or when no
        # shared DB is available at all).
        # The skill needs the challenge scope independently of graph contents; a
        # fresh graph may have no event/intents yet.
        env["DSWARM_CHALLENGE_ID"] = str(self.challenge.id)
        graph_db = getattr(self.shared_graph, "db_path", None)
        if graph_db:
            db_path = str(graph_db)
            if container is not None:
                mapper = getattr(container, "to_container_path", None)
                if callable(mapper):
                    try:
                        db_path = str(mapper(db_path))
                    except Exception:
                        pass
            env["DSWARM_BLACKBOARD_DB"] = db_path

        explicit_bb_url = str(
            env.get("DSWARM_BLACKBOARD_URL")
            or os.environ.get("DSWARM_BLACKBOARD_URL")
            or ""
        ).strip()
        has_shared_db = bool(env.get("DSWARM_BLACKBOARD_DB"))
        use_http = bool(explicit_bb_url or not has_shared_db)
        if use_http:
            env["DSWARM_BLACKBOARD_URL"] = explicit_bb_url or (
                "http://web-api:8000/api/blackboard"
                if is_web_container()
                else "http://127.0.0.1:8000/api/blackboard"
            )
            env["DSWARM_BLACKBOARD_RUN_ID"] = self.run_id
            if self._blackboard_token:
                env["DSWARM_BLACKBOARD_TOKEN"] = self._blackboard_token
            else:
                env.pop("DSWARM_BLACKBOARD_TOKEN", None)
        else:
            # Do not leave an inherited/stale URL beside the DB: the skill selects
            # HTTP whenever this variable is present, which would recreate the
            # host-coordinator/container-worker connection-refused failure.
            env.pop("DSWARM_BLACKBOARD_URL", None)
            env.pop("DSWARM_BLACKBOARD_RUN_ID", None)
            env.pop("DSWARM_BLACKBOARD_TOKEN", None)
        return env

    def _backend_for_engine(self, engine: str, profile: "Optional[dict]" = None) -> str:
        """Resolve configured backend without mutating or degrading runtime policy."""
        profile = profile if profile is not None else self._profile_for_engine(engine)
        if profile:
            runtime = self._runtime_for_engine(engine, profile)
            if runtime:
                return runtime["backend"]
        return "container" if self.worker_backend == "container" else "local"

    def _make_cli_worker(self, engine: str, *, mode: str, intent_goal: str = "",
                         intent_id: str = "", timeout_override: "Optional[int]" = None,
                         profile_role: "Optional[str]" = None,
                         task_kind: str = "", host_scan: bool = False,
                         runtime_operation_kind: str = "",
                         reproduction_id: str = "", source_finding_id: str = ""):
        guard = getattr(self, "spawn_guard", None)
        if guard is not None:
            guard.check_now(operation="spawn")
        from dswarm.solver.cli_solver import CliSolver

        # Resolve the profile FIRST — BEFORE charging the spawn budget. A missing
        # profile is a recoverable rejection (WorkerSpawnRejected), not a budget
        # event: charging _spawned_total here and then bailing would leak a phantom
        # spawn toward max_total_workers (and a bare RuntimeError would crash the
        # coordinator loop, since spawn sites only catch WorkerBudgetExhausted).
        role = profile_role or (
            "review" if mode == "review" else
            "explore" if mode == "explore" else "bootstrap")
        # D: a generic (non-intent) bootstrap spawn must prefer a focused open
        # reason intent over a whole-challenge rush, otherwise open intents starve
        # while bootstrap/retry/rebootstrap workers churn through capacity
        # (run-3154: reason's I3 sat open/unclaimed 30+ min while generic workers
        # piled up). When a compatible open intent exists, run this spawn as a
        # focused explore for it. `converted` tracks whether we need the atomic
        # claim below (the worker itself would otherwise never claim it).
        converted = False
        if (not intent_id and not profile_role and mode == "bootstrap"
                and self.shared_graph is not None):
            picked = self._pick_open_intent_for_spawn(engine)
            if picked is not None:
                mode = "explore"
                role = "explore"
                intent_id = picked["intent_id"]
                intent_goal = picked["goal"]
                converted = True
        profile = self._profile_for_engine(engine, role=role)
        if self.worker_profiles and profile is None:
            raise WorkerSpawnRejected(
                f"no available worker profile for {engine} role={role}")
        runtime_policy = getattr(self, "runtime_policy", None)
        runtime_lease_factory = None
        strict_docker = runtime_policy is not None and runtime_policy.mode == "docker"
        runtime_profile_id = str(
            (profile or {}).get("name")
            or (profile or {}).get("id")
            or engine
        )
        from dswarm.swarm.runtime import runtime_operation_for_spawn
        audited_operation_kind = runtime_operation_for_spawn(
            mode=mode,
            profile_role=str(profile_role or role),
            requested=runtime_operation_kind,
        )
        budget_gate = getattr(self, "budget_gate", None)
        if budget_gate is not None and profile is not None:
            profile_id = str(profile.get("name") or profile.get("id") or engine or role)
            # Account blocking is keyed by observed billing identity. A configured
            # credential account is not treated as billing identity until a provider
            # usage record confirms it, preventing false pre-call blocks.
            billing_account = (
                profile.get("billing_account_id")
                or profile.get("billing_account")
            )
            verdict = budget_gate.authorize(
                profile_id=profile_id,
                account_id=str(billing_account).strip() if billing_account else None,
            )
            if not verdict.allowed:
                raise WorkerSpawnRejected(
                    f"budget blocked profile={profile_id}: {verdict.reason or 'budget_cap'}")

        # Resolve the strict Docker lease boundary before charging the spawn
        # budget. A profile absent from the frozen snapshot is a policy rejection,
        # not a real worker spawn.
        worker_instance_id = uuid.uuid4().hex
        if strict_docker:
            from dswarm.swarm.runtime import (
                RuntimeSpawnRequest,
                runtime_lease_factory_for_request,
            )

            runtime_request = RuntimeSpawnRequest(
                profile_id=runtime_profile_id,
                worker_instance_id=worker_instance_id,
                operation_kind=audited_operation_kind,
                mode=mode,
                intent_id=intent_id,
            )
            runtime_lease_factory = runtime_lease_factory_for_request(
                snapshot=self.runtime_snapshot,
                pool_manager=self.pool_manager,
                request=runtime_request,
            )

        # Budget is charged ONLY after we know we'll actually build a worker.
        self._reserve_worker_spawn()

        # UNIQUE label per spawn so the deck draws one lane per worker. Every
        # claude worker would otherwise be "cli-claude" and collapse onto a single
        # lane — you couldn't tell parallel / re-bootstrapped workers apart. We keep
        # the "cli-<engine>" prefix (the deck's workerEngine() badge keys off it)
        # and append a monotonic index. The first worker of an engine keeps the bare
        # "cli-<engine>" id for back-compat (winner bookkeeping, existing tests).
        transport = base_engine_for_profile(profile or engine)
        self._label_seq[transport] = self._label_seq.get(transport, 0) + 1
        n = self._label_seq[transport]
        label = f"cli-{transport}" if n == 1 else f"cli-{transport}-{n}"

        # explore = narrow single-intent probe → SHORT per-turn timeout, so a stuck
        # explore frees its slot quickly (this is the only backstop now that the
        # stall-kill is gone). bootstrap/retry = whole-challenge rush → keep the long
        # default (CliSolver's timeout=2400).
        kw = {"timeout": self.explore_timeout} if mode == "explore" else {}
        if mode == "review":
            kw["timeout"] = int(self.review_policy.get("timeout") or 420)
        # A caller may override the bootstrap/review timeout; explicit override wins.
        if timeout_override is not None:
            kw["timeout"] = int(timeout_override)

        # M-3 (single-shot migration): fold any pending intent-level operator
        # guidance into THIS spawn (workers can't be steered live anymore).
        #  - one-shot hint/redirect text → injected with standing (then consumed).
        #  - a redirect url → handed via hitl_cmd so the worker's _target_override
        #    points at the new target (CliSolver reads hitl_cmd["url"]).
        guidance_for_worker = list(self._standing_guidance) + list(self._next_worker_guidance)
        self._next_worker_guidance = []  # one-shot: consumed by this spawn
        # B: fold active operator directives into the worker prompt as highest-priority
        # steering (deduped against guidance already present). They persist across
        # spawns (table-backed) so a worker spawned after the directive still gets it.
        if self.shared_graph is not None:
            try:
                for dtext in self.shared_graph.active_operator_directive_texts():
                    tagged = f"[operator directive] {dtext}"
                    if dtext not in guidance_for_worker and tagged not in guidance_for_worker:
                        guidance_for_worker.append(tagged)
            except Exception:
                pass
        if self._target_redirect:
            kw["hitl_cmd"] = {"action": "redirect", "url": self._target_redirect}

        workdir = self._alloc_workdir(engine)
        # A frozen runtime policy is authoritative. Strict Docker workers acquire
        # asynchronously from their run-scoped pool; approved local-dev workers
        # remain on the host. Container execution without a frozen policy is the
        # retired run-global ownership path and must fail closed, never fall back.
        if runtime_policy is None and self._backend_for_engine(engine, profile) == "container":
            raise RuntimePolicyError("runtime_policy_required")
        container = None
        gateway_token = None
        explicit_endpoint = bool(
            (profile or {}).get("base_url")
            or (profile or {}).get("api_key_ref")
            or (profile or {}).get("provider_ref")
        )
        if (strict_docker or container is not None) and not explicit_endpoint:
            from dswarm.solver.modelgateway import ModelGateway, WorkerClaims
            gateway_token = ModelGateway.instance().issue_worker(WorkerClaims(
                run_id=self.run_id,
                challenge_id=str(self.challenge.id),
                worker_instance_id=worker_instance_id,
                solver_id=label,
                profile_id=str((profile or {}).get("id") or engine),
                configured_account_id=(
                    str((profile or {}).get("credential_account")).strip() or None
                    if (profile or {}).get("credential_account") is not None else None
                ),
                token_scope=(
                    "review" if role == "review" else
                    "recon" if role == "recon" else "worker"
                ),
            ))
        from dswarm.solver.cli_driver import driver_for
        try:
            if runtime_lease_factory is not None and gateway_token:
                runtime_lease_factory.bind_worker_env({
                    "DEEPSEEK_API_KEY": gateway_token,
                    "DSWARM_TASK_TOKEN": gateway_token,
                    "DSWARM_GATEWAY_URL": os.environ.get(
                        "DSWARM_GATEWAY_URL",
                        "http://host.docker.internal:"
                        f"{os.environ.get('DSWARM_MODEL_GATEWAY_PORT', '9101')}/v1",
                    ),
                    "DSWARM_PI_PROVIDER": "ctf-gateway",
                    "DSWARM_WORKER_MODEL": str(
                        (profile or {}).get("model") or "deepseek-v4-flash"
                    ),
                })
            worker = CliSolver(
                None, self.challenge, bus=self.bus, cost=self.cost,
                artifacts=self.artifacts, config=self.config, run_id=self.run_id,
                insight=self.insight, knowledge=self.knowledge,
                shared_graph=self.shared_graph, engine=transport,
                driver=driver_for(profile or transport),
                web_access=self.web_access, kb=self.kb,
                usage_writer=self.usage_writer,
                fallback_usage_writer=self.fallback_usage_writer,
                workdir=workdir,
                mode=mode, intent_goal=intent_goal, intent_id=intent_id,
                solver_label=label, **kw,
                # hand the worker the operator's standing guidance + any one-shot
                # intent-level guidance so its (single) prompt already carries VPS/SSH
                # creds, corrections, etc. (copy: the worker must not mutate the
                # coordinator's canonical list).
                standing_guidance=guidance_for_worker,
                # multi-flag: seed the already-found set so a re-bootstrapped worker's
                # turn-1 prompt lists the flags the run already has and hunts the rest
                # (empty for a single-flag run → no effect).
                found_flags=list(self._found_flags),
                # swarm sub-worker: its end is worker-level (WORKER_FINISHED), NOT the
                # run's. The coordinator owns the single run-level RUN_FINISHED so a
                # worker ending mid-run doesn't make the deck show "已结束" while the
                # coordinator is still re-bootstrapping (the run-7345 bug).
                lifecycle_scope="worker",
                # container backend (None → local host subprocess, default).
                container=container,
                worker_env=self._runtime_env_for(
                    transport, label, container=container, profile=profile,
                    task_token=gateway_token,
                    home_and_gateway_only=strict_docker,
                ),
                runtime_lease_factory=runtime_lease_factory,
                runtime_policy=runtime_policy,
                runtime_operation_kind=audited_operation_kind,
                task_kind=task_kind,
                host_scan=host_scan,
                reproduction_id=reproduction_id,
                source_finding_id=source_finding_id,
            )
        except BaseException:
            if gateway_token:
                from dswarm.solver.modelgateway import ModelGateway
                ModelGateway.instance().revoke_token(gateway_token)
            raise
        worker.worker_instance_id = worker_instance_id
        worker.gateway_token = gateway_token
        if runtime_lease_factory is not None:
            # The callable binding records the concrete generation identity when
            # CliSolver acquires its lease. SwarmWorkerRuntime consumes only this
            # frozen identity when classifying a failure; it never re-reads settings.
            worker.runtime_pool_id = runtime_lease_factory.pool_id
            worker.runtime_pool_instance_id = ""
            worker.runtime_lease_binding = runtime_lease_factory
        try:
            self._claim_worker_account(worker.solver_id, transport, profile, role=role)
        except BaseException:
            # Token issuance happens before profile/account admission.  If the
            # admission hook fails, release the worker-local token as well as any
            # partial accounting state it may have installed before raising.
            self._release_worker_account(worker)
            raise
        if converted:
            # D: claim the pre-empted open intent atomically under THIS worker's
            # solver_id (so conclude's owner-fence accepts exactly this worker).
            # If someone else claimed the intent, do not duplicate their work — release
            # the slot and let the caller pick another path.
            try:
                won = self.shared_graph.claim_intent(
                    worker=worker.solver_id, intent_id=intent_id,
                    lease_s=float(self.explore_timeout) + 300.0)
            except Exception:
                won = False
            if not won:
                self._release_worker_account(worker)
                raise WorkerSpawnRejected(
                    f"open intent {intent_id} already claimed elsewhere")
        return worker

    async def _apply_worker_cmds(self, *, tasks: dict, task_solvers: dict,
                                 healthy: list[str], running_engines_fn, emit_bb) -> None:
        """Drain operator spawn/kill worker commands onto the live scheduler
        state (BE-worker-management runtime control). Mutates tasks/task_solvers
        in place. A spawn adds a fresh bootstrap worker for the requested engine
        (capped at max_workers; engine must be in the roster or currently healthy);
        a kill cancels the worker whose solver_id matches (it's reaped next loop)."""
        if self.worker_cmds is None:
            return
        while not self.worker_cmds.empty():
            try:
                cmd = self.worker_cmds.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not isinstance(cmd, dict):
                continue
            action = cmd.get("action")
            if action == "spawn":
                if not self._ordinary_capacity_available(tasks):
                    await emit_bb("worker_spawn_rejected", reason="max_workers")
                    continue
                try:
                    requested = cmd.get("engine")
                    if requested and self.worker_profiles:
                        matches = [
                            e for e in normalize_profile_roster([requested], self.worker_profiles)
                            if e in self.engines and self._healthy_matches(e, healthy)
                        ]
                        if not matches:
                            await emit_bb("worker_spawn_rejected",
                                          reason="unavailable_profile",
                                          engine=str(requested))
                            continue
                        engine = matches[0]
                    else:
                        engine = requested or self._pick_engine(
                            running_engines_fn(), healthy, role="bootstrap")
                except RuntimeError as exc:
                    await emit_bb("worker_spawn_rejected", reason=str(exc))
                    continue
                # only spawn an engine in the configured roster or one currently
                # healthy — never silently launch something offline mode dropped.
                if self.worker_profiles:
                    unknown = engine not in self.engines or not self._healthy_matches(str(engine), healthy)
                else:
                    unknown = engine not in self.engines and engine not in healthy
                if unknown:
                    await emit_bb("worker_spawn_rejected",
                                  reason="unknown_engine", engine=str(engine))
                    continue
                if not self._engine_available_for_role(str(engine), "bootstrap"):
                    await emit_bb("worker_spawn_rejected",
                                  reason="profile_capacity", engine=str(engine))
                    continue
                try:
                    lane = await self._worker_lane_gate.acquire(
                        mode="bootstrap",
                        worker_class="shell_agent",
                        stop_event=getattr(self, "_reason_stop_event", None),
                        pause_event=getattr(self, "_reason_pause_gate", None),
                    )
                except (WorkerLaneDisabled, WorkerLaneStopped) as exc:
                    await emit_bb("worker_spawn_rejected", reason=str(exc),
                                  engine=str(engine), phase="operator")
                    continue
                try:
                    w = self._make_cli_worker(
                        engine,
                        mode="bootstrap",
                        runtime_operation_kind="bootstrap",
                    )
                except WorkerSpawnRejected as exc:
                    self._worker_lane_gate.release(lane)
                    await emit_bb("worker_spawn_rejected", reason=str(exc),
                                  engine=str(engine), phase="operator")
                    continue
                except WorkerBudgetExhausted as exc:
                    self._worker_lane_gate.release(lane)
                    await emit_bb(str(exc), spawned_total=self._spawned_total,
                                  max_total_workers=self.max_total_workers,
                                  cost_usd=self._current_cost_usd(),
                                  cost_budget_usd=self.cost_budget_usd)
                    continue
                except BaseException:
                    self._worker_lane_gate.release(lane)
                    raise

                release_state = {"released": False}

                def _release_lane_once(
                    _lane=lane, _state=release_state,
                ) -> None:
                    if _state["released"]:
                        return
                    _state["released"] = True
                    self._worker_lane_gate.release(_lane)

                async def _run_operator_worker(
                    _worker=w, _release=_release_lane_once,
                ):
                    try:
                        return await _worker.run()
                    finally:
                        self._release_worker_account(_worker)
                        _release()

                try:
                    t = asyncio.create_task(
                        _run_operator_worker(), name=f"operator-{engine}"
                    )
                except BaseException:
                    _release_lane_once()
                    raise
                t.add_done_callback(
                    lambda _task, _release=_release_lane_once: _release()
                )
                tasks[t] = engine
                task_solvers[t] = w
                await emit_bb("worker_spawned", worker=w.solver_id,
                              phase="operator", worker_role="worker")
            elif action == "kill":
                sid = cmd.get("solver_id")
                for t, w in list(task_solvers.items()):
                    if getattr(w, "solver_id", None) == sid:
                        self._cancel_solver(w)
                        t.cancel()
                        await emit_bb("worker_killed", worker=sid)
                        break
