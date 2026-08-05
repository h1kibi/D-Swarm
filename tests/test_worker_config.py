"""Default worker-roster config (apps/web/worker_config.py) + operator runtime
worker commands (RunManager.post_worker_cmd). Pure/unit — no subprocess, no key."""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest

from apps.web.run_manager import RunManager
from apps.web.drivers import _missing_profile_accounts
from apps.web.drivers import build_driver
from apps.web.worker_config import (
    DEFAULT_CATEGORY_OVERRIDES,
    DEFAULT_ENGINES,
    DEFAULT_RUNTIME_PROFILES,
    DEFAULT_WORKER_PROFILES,
    WorkerConfigStore,
)


# ── default config + validation ──────────────────────────────────────────────

def test_config_defaults_when_empty(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    cfg = wc.get()
    assert cfg["engines"] == DEFAULT_ENGINES
    assert cfg["start_workers"] == len(DEFAULT_ENGINES)
    assert cfg["max_workers"] == 10
    assert cfg["worker_backend"] == "container"
    assert cfg["race_scout"] is True
    assert cfg["race_timeout"] == 720
    assert cfg["wall_clock_budget"] == 0
    assert cfg["race_engines"] == []
    assert cfg["max_total_workers"] == 0
    assert cfg["cost_budget_usd"] == 0.0
    assert cfg["stage_policy"]["budgets"]["max_total_workers"] == 0
    assert cfg["stage_policy"]["coordinator"]["review"]["enabled"] is True
    assert cfg["stage_policy"]["coordinator"]["review"]["engine"] == "pi-web"
    assert cfg["stage_policy"]["coordinator"]["review"]["candidate_spike_threshold"] == 5
    assert cfg["llm_profiles"]["planner"]["model"] == "deepseek-v4-pro"
    assert cfg["runtime_profiles"] == DEFAULT_RUNTIME_PROFILES
    assert {r["id"] for r in cfg["runtime_profiles"]} >= {
        "docker-host-target", "docker-offline", "docker-pwn-heavy"}
    assert cfg["worker_profiles"] == DEFAULT_WORKER_PROFILES
    assert cfg["overrides"] == {
        cat: {"engines": names, "start_workers": len(names)}
        for cat, names in DEFAULT_CATEGORY_OVERRIDES.items()
    }


def test_config_set_engines_dedupes_and_filters(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    cfg = wc.set(engines=["pi-web", "bogus", "pi-web", "pi-pwn"])
    assert cfg["engines"] == ["pi-web", "pi-pwn"]


# ── seat lineup tracks enabled toggles (regression) ──────────────────────────
# Bug: the seat UI showed 3 seats enabled, but a stale top-level `engines`
# lineup (left from an older config) won at get() — it short-circuits the
# "else enabled seats" fallback — so dispatch raced only the one stale engine.
# The new-model lineup must always reconcile to the enabled seats.

def _seat(sid: str, engine: str, *, enabled: bool = True) -> dict:
    return {
        "id": sid, "label": sid, "engine": engine, "transport": engine,
        "credential_mode": "api_key", "credential_account": f"{engine}-main",
        "runtime": "docker-web", "roles": ["race", "bootstrap"], "race": True,
        "enabled": enabled,
    }


def test_seat_lineup_reconciles_stale_engines_to_enabled_seats(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    # 3 enabled seats, but a stale lineup naming only one (the exact bug shape).
    wc.set_identity_model(seats=[
        _seat("seat_pi_web_x", "pi"),
        _seat("seat_pi_pwn_x", "pi"),
        _seat("seat_pi_rev_x", "pi"),
    ])
    # inject a stale lineup the way a legacy config would carry it, then re-project.
    wc._data["engines"] = ["pi"]
    wc._project_identity_to_legacy()
    engines = wc.get()["engines"]
    # all three enabled seats race now — not just the stale bare-engine "pi".
    assert set(engines) == {"seat_pi_web_x", "seat_pi_pwn_x", "seat_pi_rev_x"}


def test_seat_lineup_drops_disabled_seat(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    wc.set_identity_model(seats=[
        _seat("seat_pi_web_x", "pi"),
        _seat("seat_pi_pwn_x", "pi", enabled=False),  # disabled → out of lineup
        _seat("seat_pi_rev_x", "pi"),
    ])
    engines = wc.get()["engines"]
    assert set(engines) == {"seat_pi_web_x", "seat_pi_rev_x"}
    assert "seat_pi_pwn_x" not in engines


def test_seat_lineup_preserves_prior_order(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    wc.set_identity_model(seats=[
        _seat("seat_pi_web_x", "pi"),
        _seat("seat_pi_pwn_x", "pi"),
        _seat("seat_pi_rev_x", "pi"),
    ])
    # an intentional ordering already present in `engines` is kept; newly-enabled
    # seats are appended (not reordered).
    wc._data["engines"] = ["seat_pi_rev_x", "seat_pi_web_x"]
    wc._project_identity_to_legacy()
    engines = wc.get()["engines"]
    assert engines[:2] == ["seat_pi_rev_x", "seat_pi_web_x"]
    assert set(engines) == {"seat_pi_web_x", "seat_pi_pwn_x", "seat_pi_rev_x"}


def test_backend_set_local_rejected_in_web_container(tmp_path, monkeypatch):
    # P2-v3: an EXPLICIT operator set() of local-in-container is rejected (400 at
    # the API) so the operator sees why, rather than silently doing the wrong thing.
    import apps.web.worker_config as wcmod
    monkeypatch.setattr(wcmod, "is_web_container", lambda: True)
    wc = WorkerConfigStore(root=tmp_path)
    with pytest.raises(ValueError, match="not allowed when the web control plane"):
        wc.set(engines=["pi-web"], worker_backend="local")


def test_backend_stale_local_coerced_on_read_in_web_container(tmp_path, monkeypatch):
    # A config persisted as local on a bare host, then loaded inside a container,
    # is silently COERCED to container on read (get) — never reaches the swarm as
    # local. Persist local on a host, then flip is_web_container True and re-read.
    import apps.web.worker_config as wcmod
    monkeypatch.setattr(wcmod, "is_web_container", lambda: False)
    wc = WorkerConfigStore(root=tmp_path)
    wc.set(engines=["pi-web"], worker_backend="local")
    assert wc.get()["worker_backend"] == "local"   # host: preserved
    monkeypatch.setattr(wcmod, "is_web_container", lambda: True)
    assert wc.get()["worker_backend"] == "container"  # container: coerced on read


def test_backend_local_preserved_on_bare_host(tmp_path, monkeypatch):
    # On a bare host the local backend is preserved (historical behaviour).
    import apps.web.worker_config as wcmod
    monkeypatch.setattr(wcmod, "is_web_container", lambda: False)
    wc = WorkerConfigStore(root=tmp_path)
    cfg = wc.set(engines=["pi-web"], worker_backend="local")
    assert cfg["worker_backend"] == "local"


def test_config_set_rejects_empty_engine_list(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    with pytest.raises(ValueError):
        wc.set(engines=["nope", "alsо_bad"])  # nothing valid → reject


def test_config_set_rejects_nonpositive_counts(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    with pytest.raises(ValueError):
        wc.set(start_workers=0)
    with pytest.raises(ValueError):
        wc.set(max_workers=-3)


def test_config_max_workers_is_derived_from_roster_sum(tmp_path):
    # max_workers is a READ-ONLY derived ceiling = Σ of the dispatched seats'
    # max_running. The per-seat values the operator typed persist VERBATIM (never
    # mutated), and max_workers is computed FROM them — even if the request body
    # carries a stale/explicit max_workers, the derived sum wins.
    wc = WorkerConfigStore(root=tmp_path)
    caps = {"pi-web": 2, "pi-pwn": 1, "pi-rev": 2}
    cfg = wc.set(
        engines=["pi-web", "pi-pwn", "pi-rev"],
        max_workers=10,  # stale/ignored — overwritten by the derived sum
        worker_profiles=[
            {**p, "max_running": caps.get(p["name"], p["max_running"])}
            for p in DEFAULT_WORKER_PROFILES
        ],
    )

    by_name = {p["name"]: p for p in cfg["worker_profiles"]}
    selected = [by_name[name] for name in cfg["engines"]]
    # the operator's typed caps are left untouched.
    assert [p["max_running"] for p in selected] == [2, 1, 2]
    # max_workers tracks the sum, NOT the supplied 10.
    assert cfg["max_workers"] == 5


def test_config_set_does_not_balloon_sole_eligible_seat(tmp_path):
    # Regression (Bug B): a stale single-engine dispatch lineup (cursor-only) used
    # to make cursor the ONLY eligible profile, so the auto-grow loop ballooned
    # cursor's max_running up to max_workers — any value the user typed "reverted"
    # on every save. Now seats are NEVER mutated; cursor keeps exactly what was
    # set, and max_workers just equals that seat's capacity.
    wc = WorkerConfigStore(root=tmp_path)
    profiles = [
        {**p, "max_running": 1 if p["name"] == "pi-web" else p["max_running"]}
        for p in DEFAULT_WORKER_PROFILES
    ]
    cfg = wc.set(
        engines=["pi-web"],  # stale single-profile dispatch lineup
        max_workers=6,
        worker_profiles=profiles,
    )
    by_name = {p["name"]: p for p in cfg["worker_profiles"]}
    assert by_name["pi-web"]["max_running"] == 1  # NOT bumped to 6
    assert cfg["max_workers"] == 1  # derived = the one eligible seat's cap


def test_config_max_workers_tracks_roster_edit_up_and_down(tmp_path):
    # Editing a seat's concurrency moves the derived max_workers BOTH ways.
    wc = WorkerConfigStore(root=tmp_path)
    caps = {"pi-web": 2, "pi-pwn": 1, "pi-rev": 2}
    base = [{**p, "max_running": caps.get(p["name"], p["max_running"])}
            for p in DEFAULT_WORKER_PROFILES]
    wc.set(engines=["pi-web", "pi-pwn", "pi-rev"], worker_profiles=base)

    # raise pi-rev 2 → 4: sum 2+1+2=5 becomes 2+1+4=7
    up = [{**p, "max_running": 4 if p["name"] == "pi-rev" else p["max_running"]}
          for p in base]
    cfg = wc.set(worker_profiles=up)
    assert cfg["max_workers"] == 7

    # lower pi-web 2 → 1: sum 1+1+4 = 6  (max follows DOWN, not just up)
    down = [{**p, "max_running": (1 if p["name"] == "pi-web"
                                  else 4 if p["name"] == "pi-rev"
                                  else p["max_running"])}
            for p in base]
    cfg = wc.set(worker_profiles=down)
    assert cfg["max_workers"] == 6


def test_config_max_workers_counts_only_dispatched_seats(tmp_path):
    # Only seats in the dispatch lineup (engines) contribute to the derived max.
    wc = WorkerConfigStore(root=tmp_path)
    caps = {"pi-web": 2, "pi-pwn": 1}
    profiles = [{**p, "max_running": caps.get(p["name"], p["max_running"])}
                for p in DEFAULT_WORKER_PROFILES]
    # dispatch only pi-web(2) + pi-pwn(1); the other five excluded → sum = 3
    cfg = wc.set(engines=["pi-web", "pi-pwn"], worker_profiles=profiles)
    assert cfg["max_workers"] == 3


def test_config_dedicated_review_seat_does_not_inflate_max_workers(tmp_path):
    # The review worker is an INDEPENDENT seat. A review-only profile (roles ==
    # ["review"], no ordinary race/bootstrap/explore/respond role) must NOT count
    # toward max_workers (that ceiling gates ORDINARY worker concurrency only,
    # which the swarm enforces via max_running / _active_profile_counts — a path
    # totally separate from review's max_review_running / _active_review_profile_
    # counts). Its review concurrency field must also survive untouched. This
    # guards against folding review capacity into the ordinary ceiling.
    wc = WorkerConfigStore(root=tmp_path)
    ordinary = [
        {**p, "max_running": 2} for p in DEFAULT_WORKER_PROFILES if p["name"] == "pi-web"
    ]
    review_seat = {
        "id": "review-only",
        "name": "review-only",
        "engine": "pi",
        "transport": "pi_cli",
        "runtime": "local",
        "roles": ["review"],          # review-ONLY, no ordinary role
        "max_running": 9,             # would balloon max_workers IF wrongly counted
        "max_review_running": 1,
        "enabled": True,
    }
    cfg = wc.set(
        engines=["pi-web"],            # only the ordinary pi-web seat is dispatched
        worker_profiles=ordinary + [review_seat],
    )
    # Derived ceiling = the ordinary pi-web seat's max_running (2) ONLY. The
    # review-only seat's max_running (9) is excluded — NOT 2+9=11.
    assert cfg["max_workers"] == 2
    saved_review = next(p for p in cfg["worker_profiles"] if p["name"] == "review-only")
    # review-only stayed review-only (no ordinary role auto-appended) and its
    # review concurrency is preserved verbatim — we never mutate it.
    assert "review" in saved_review["roles"]
    assert not ({"race", "bootstrap", "explore", "respond"} & set(saved_review["roles"]))
    assert saved_review["max_review_running"] == 1
    assert saved_review["max_running"] == 9  # untouched even though excluded


def test_config_set_clamps_start_workers_to_max_workers(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    cfg = wc.set(start_workers=7, max_workers=3)

    assert cfg["start_workers"] == 3
    assert cfg["max_workers"] == 3


def test_config_persists_across_reload(tmp_path):
    WorkerConfigStore(root=tmp_path).set(engines=["pi-web"], start_workers=1,
                                         max_workers=4, worker_backend="container",
                                         race_scout=False, race_timeout=300,
                                         wall_clock_budget=1800,
                                         race_engines=["pi-web"],
                                         max_total_workers=11,
                                         cost_budget_usd=1.25,
                                         stage_policy={
                                             "coordinator": {
                                                 "wall_clock_budget": 1800,
                                                 "review": {
                                                     "enabled": True,
                                                     "engine": "pi-pwn",
                                                     "timeout": 333,
                                                     "after_fruitless_workers": 2,
                                                     "candidate_spike_threshold": 4,
                                                 },
                                             }
                                         },
                                         llm_profiles={
                                             "planner": {"provider": "deepseek", "model": "planner-x"},
                                             "titler": {"provider": "deepseek", "model": "titler-x"},
                                         })
    cfg = WorkerConfigStore(root=tmp_path).get()  # fresh load from disk
    assert cfg["engines"] == ["pi-web"]
    # max_workers is derived (Σ dispatched seats' max_running); dispatching the
    # single pi-web seat (default max_running=1) yields 1, NOT the supplied 4.
    assert cfg["start_workers"] == 1 and cfg["max_workers"] == 1
    assert cfg["worker_backend"] == "container"
    assert cfg["race_scout"] is False
    assert cfg["race_timeout"] == 300
    assert cfg["wall_clock_budget"] == 1800
    assert cfg["race_engines"] == ["pi-web"]
    assert cfg["max_total_workers"] == 11
    assert cfg["cost_budget_usd"] == 1.25
    assert cfg["stage_policy"]["race"]["engines"] == ["pi-web"]
    assert cfg["stage_policy"]["coordinator"]["review"]["engine"] == "pi-pwn"
    assert cfg["stage_policy"]["coordinator"]["review"]["timeout"] == 333
    assert cfg["stage_policy"]["coordinator"]["review"]["after_fruitless_workers"] == 2
    assert cfg["stage_policy"]["coordinator"]["review"]["candidate_spike_threshold"] == 4
    assert cfg["llm_profiles"]["titler"]["model"] == "titler-x"


# ── per-category override + resolve ──────────────────────────────────────────

def test_resolve_uses_default_without_override(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    wc.set(engines=["pi-web", "pi-pwn", "pi-rev"], start_workers=3,
           worker_backend="container", race_timeout=240)
    r = wc.resolve("unsorted")  # a category with no default direction override
    assert r["engines"] == ["pi-web", "pi-pwn", "pi-rev"]
    assert r["start_workers"] == 3
    assert r["worker_backend"] == "container"
    assert r["race_timeout"] == 240
    assert r["worker_profiles"] == wc.get()["worker_profiles"]
    assert r["runtime_profiles"] == wc.get()["runtime_profiles"]


def test_resolve_applies_category_override(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    wc.set(engines=["pi-web", "pi-pwn", "pi-rev"],
           overrides={"pwn": {"engines": ["pi-pwn"], "start_workers": 2}})
    r = wc.resolve("pwn")
    assert r["engines"] == ["pi-pwn"]
    assert r["start_workers"] == 2
    # a category with no override still gets the defaults
    assert wc.resolve("web")["engines"] == ["pi-web", "pi-pwn", "pi-rev"]


def test_resolve_override_start_workers_defaults_to_engine_count(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    wc.set(overrides={"crypto": {"engines": ["pi-crypto"]}})  # no start_workers
    assert wc.resolve("crypto")["start_workers"] == 1


# ── RunManager.post_worker_cmd (operator runtime control) ────────────────────

def test_post_worker_cmd_requires_live_run(tmp_path):
    mgr = RunManager(sessions_root=tmp_path)
    run = mgr.create("run-x")
    # no live task → rejected (a finished/ghost run has no coordinator to act)
    assert asyncio.run(mgr.post_worker_cmd("run-x", "spawn", engine="pi-web")) is False


def test_post_worker_cmd_enqueues_for_live_run(tmp_path):
    mgr = RunManager(sessions_root=tmp_path)
    run = mgr.create("run-y")

    async def go():
        run.task = asyncio.create_task(asyncio.sleep(3600))
        ok_spawn = await mgr.post_worker_cmd("run-y", "spawn", engine="pi-web")
        ok_kill = await mgr.post_worker_cmd("run-y", "kill", solver_id="cli-pi-web")
        run.task.cancel()
        return ok_spawn, ok_kill, [run.worker_cmds.get_nowait(),
                                   run.worker_cmds.get_nowait()]

    ok_spawn, ok_kill, cmds = asyncio.run(go())
    assert ok_spawn is True and ok_kill is True
    assert cmds[0] == {"action": "spawn", "engine": "pi-web"}
    assert cmds[1] == {"action": "kill", "solver_id": "cli-pi-web"}


def test_post_worker_cmd_unknown_run_returns_false(tmp_path):
    mgr = RunManager(sessions_root=tmp_path)
    assert asyncio.run(mgr.post_worker_cmd("ghost", "spawn", engine="pi-web")) is False


def test_config_rejects_invalid_backend_and_profiles(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    with pytest.raises(ValueError):
        wc.set(worker_backend="vm")
    with pytest.raises(ValueError):
        wc.set(runtime_profiles=[{"id": "bad", "backend": "vm"}])
    with pytest.raises(ValueError):
        wc.set(worker_profiles=[{"id": "bad", "engine": "deepseek"}])


def test_config_accepts_profile_schema(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    cfg = wc.set(
        runtime_profiles=[{"id": "docker-web", "backend": "container", "label": "Docker Web"}],
        worker_profiles=[{
            "id": "pi-web-custom",
            "engine": "pi",
            "transport": "pi_cli",
            "auth": "oauth_token",
            "credential_account": "pi-team",
            "runtime": "docker-web",
            "roles": ["bootstrap", "explore"],
            "race": False,
            "max_running": 3,
            "priority": 7,
            "model": "deepseek-v4-flash",
            "enabled": True,
        }],
    )
    assert cfg["runtime_profiles"][0]["backend"] == "container"
    p = cfg["worker_profiles"][0]
    assert p["credential_account"] == "pi-team"
    assert p["name"] == "pi-web-custom"
    assert p["credential_mode"] == "oauth_token"
    assert p["roles"] == ["bootstrap", "explore", "review"]
    assert p["race"] is False
    assert p["max_running"] == 3
    assert p["priority"] == 7
    assert p["model"] == "deepseek-v4-flash"


def test_worker_profiles_accept_review_role_and_default_roles_include_review(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    cfg = wc.set(
        runtime_profiles=[{"id": "local", "backend": "local", "label": "Local"}],
        worker_profiles=[{
            "id": "review-pi",
            "engine": "pi",
            "runtime": "local",
            "roles": ["review"],
            "enabled": True,
        }],
        engines=["review-pi"],
        stage_policy={"coordinator": {"review": {"engine": "review-pi"}}},
    )
    assert cfg["worker_profiles"][0]["roles"] == ["review"]
    assert cfg["stage_policy"]["coordinator"]["review"]["engine"] == "review-pi"

    cfg2 = WorkerConfigStore(root=tmp_path / "other").set(
        runtime_profiles=[{"id": "local", "backend": "local", "label": "Local"}],
        worker_profiles=[{
            "id": "default-roles",
            "engine": "pi",
            "runtime": "local",
            "enabled": True,
        }],
        engines=["default-roles"],
    )
    assert "review" in cfg2["worker_profiles"][0]["roles"]


def test_worker_profile_migrates_execution_roles_to_review(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    cfg = wc.set(
        engines=["legacy-pi"],
        worker_profiles=[{
            "id": "legacy-pi",
            "engine": "pi",
            "transport": "pi_cli",
            "runtime": "local",
            "roles": ["race", "bootstrap", "explore"],
        }],
    )

    roles = cfg["worker_profiles"][0]["roles"]
    assert roles == ["race", "bootstrap", "explore", "review"]


def test_config_preserves_blank_credential_account_for_local_subscription_cli(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    cfg = wc.set(
        runtime_profiles=[{"id": "local", "backend": "local", "label": "Local"}],
        worker_profiles=[{
            "id": "pi-local",
            "engine": "pi",
            "transport": "pi_cli",
            "auth": "subscription",
            "credential_account": "",
            "runtime": "local",
            "roles": ["race", "bootstrap", "explore"],
            "enabled": True,
        }],
        engines=["pi-local"],
    )

    assert cfg["worker_profiles"][0]["credential_account"] == ""


def test_config_rejects_duplicate_profile_ids_and_unknown_runtime(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    with pytest.raises(ValueError):
        wc.set(worker_profiles=[
            {"id": "dup", "engine": "pi", "runtime": "local"},
            {"id": "dup", "engine": "pi", "runtime": "local"},
        ])
    with pytest.raises(ValueError):
        wc.set(worker_profiles=[
            {"id": "bad-runtime", "engine": "pi", "runtime": "missing"},
        ])


def test_missing_profile_accounts_detects_container_and_api_profiles(tmp_path, monkeypatch):
    # detect_system_login now lives in the profile_health kernel that the dispatch
    # precheck delegates to (single source of truth).
    monkeypatch.setattr(
        "muteki.solver.profile_health.detect_system_login", lambda engine, env=None: "absent"
    )
    missing = _missing_profile_accounts(
        worker_profiles=[
            {"id": "pi-web-sub", "engine": "pi", "runtime": "docker-web",
             "auth": "subscription", "credential_account": "pi-main", "enabled": True},
            {"id": "local-sub", "engine": "pi", "runtime": "local",
             "auth": "subscription", "credential_account": "unused", "enabled": True},
            {"id": "pi-api", "engine": "pi", "runtime": "local",
             "auth": "api_key", "credential_account": "pi-api-main", "enabled": True},
        ],
        runtime_profiles=[
            {"id": "local", "backend": "local"},
            {"id": "docker-web", "backend": "container"},
        ],
        sessions_root=tmp_path,
    )
    assert "pi-web-sub:pi-main" in missing
    assert "pi-api:pi-api-main" in missing
    assert all("local-sub" not in x for x in missing)


def test_missing_profile_accounts_allows_local_system_login_without_account(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "muteki.solver.profile_health.detect_system_login", lambda engine, env=None: "present"
    )
    monkeypatch.setattr("muteki.solver.cli_driver.driver_for", lambda profile: type(
        "D", (), {"health_detail": lambda self, env=None: (True, "")})())

    missing = _missing_profile_accounts(
        worker_profiles=[{
            "id": "pi-api-local",
            "engine": "pi",
            "runtime": "local",
            "auth": "api_key",
            "credential_account": "pi-main",
            "enabled": True,
        }],
        runtime_profiles=[{"id": "local", "backend": "local"}],
        sessions_root=tmp_path,
    )

    assert missing == []


def test_worker_config_accepts_api_endpoint_profile_names(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    cfg = wc.set(
        runtime_profiles=[{"id": "local", "backend": "local"}],
        worker_profiles=[{
            "id": "pi-endpoint",
            "name": "pi-endpoint",
            "engine": "pi",
            "transport": "pi_cli",
            "credential_mode": "api",
            "credential_account": "pi-main",
            "base_url": "https://api.deepseek.example/v1",
            "api_key_ref": "env:MUTEKI_DEEPSEEK_API_KEY",
            "runtime": "local",
            "model": "deepseek-chat",
        }],
        engines=["pi-endpoint"],
    )

    # legacy foreign keys stay readable; the new model is attached additively.
    assert cfg["engines"] == ["pi-endpoint"]
    p = cfg["worker_profiles"][0]
    assert p["engine"] == "pi"
    assert p["base_url"] == "https://api.deepseek.example/v1"
    assert p["api_key_ref"] == "env:MUTEKI_DEEPSEEK_API_KEY"
    # the endpoint is also captured in the new Credential/Seat model (additive)
    seat = next(s for s in cfg["seats"] if s["engine"] == "pi")
    assert seat["label"] == "pi-endpoint"
    cred = next(c for c in cfg["credentials"] if c["id"] == seat["credential_id"])
    assert cred["kind"] == "custom_endpoint"
    assert cred["endpoint"]["base_url"] == "https://api.deepseek.example/v1"


def test_account_base_url_hydrates_pi_profile_for_dispatch(tmp_path, monkeypatch):
    """Regression: the settings account form stores BASE_URL in the account store,
    but dispatch switches to the custom endpoint only when profile.base_url is
    present. Reading worker config must bridge the two."""
    from muteki.solver.credential_accounts import CredentialAccountStore, account_store_root
    from muteki.solver.cli_driver import driver_for

    monkeypatch.setenv("MUTEKI_PI_BIN", "/usr/bin/pi")
    CredentialAccountStore(account_store_root(tmp_path)).upsert_secret(
        account_id="pi-main",
        engine="api",
        secret="deepseek-secret",
        base_url="https://api.deepseek.example/v1",
        target_engine="pi",
    )

    cfg = WorkerConfigStore(root=tmp_path).get()
    profile = next(p for p in cfg["worker_profiles"] if p["engine"] == "pi")

    assert profile["credential_account"] == "pi-main"
    assert profile["credential_mode"] == "api_key"
    assert profile["base_url"] == "https://api.deepseek.example/v1"

    argv = driver_for(profile).build_execute("PROMPT", None, web_access=False)
    assert argv[0] == "/usr/bin/pi"  # endpoint wrapper keeps the pi CLI argv
    assert "--mode" in argv


def test_account_base_url_hydrates_empty_binding_new_schema_pi_profile(tmp_path):
    """A saved seat can still be host-inherit/empty from the identity migration.
    If the operator later registers pi-main as a custom endpoint, dispatch
    should use that default endpoint instead of silently inheriting a provider."""
    from muteki.solver.credential_accounts import CredentialAccountStore, account_store_root
    from muteki.solver.identity_model import credential_id_for

    CredentialAccountStore(account_store_root(tmp_path)).upsert_secret(
        account_id="pi-main",
        engine="api",
        secret="deepseek-secret",
        base_url="https://api.deepseek.example/v1",
        target_engine="pi",
    )
    cred_id = credential_id_for("pi", legacy_account_id="pi-main")
    WorkerConfigStore(root=tmp_path).set_identity_model(
        seats=[{
            "id": "seat_pi_default",
            "label": "pi-local",
            "engine": "pi",
            "credential_id": cred_id,
            "environment_id": "local",
            "model": "deepseek-chat",
            "roles": ["race", "bootstrap", "explore", "review"],
            "enabled": True,
        }],
        credentials=[{
            "id": cred_id,
            "label": "pi system CLI",
            "engine": "pi",
            "kind": "system_inherit",
            "secret_ref": "",
        }],
        environments=[{"id": "local", "label": "Local host", "backend": "local"}],
    )

    cfg = WorkerConfigStore(root=tmp_path).get()
    profile = cfg["worker_profiles"][0]

    assert profile["credential_account"] == "pi-main"
    assert profile["base_url"] == "https://api.deepseek.example/v1"
    assert profile["credential_mode"] == "api_key"


def test_profile_endpoint_healthcheck_uses_endpoint_url(tmp_path, monkeypatch):
    root = tmp_path / "_secrets" / "accounts" / "pi-main"
    root.mkdir(parents=True)
    (root / "API_KEY").write_text("secret\n")

    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env") or {}
        return subprocess.CompletedProcess(
            argv, 0,
            '{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}\n',
            "",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("MUTEKI_PI_PROVIDER", "custom")
    missing = _missing_profile_accounts(
        worker_profiles=[{
            "id": "pi-endpoint",
            "name": "pi-endpoint",
            "engine": "pi",
            "transport": "pi_cli",
            "credential_mode": "api",
            "credential_account": "pi-main",
            "base_url": "https://api.deepseek.example/v1",
            "api_key_ref": "env:MUTEKI_DEEPSEEK_API_KEY",
            "runtime": "local",
            "enabled": True,
        }],
        runtime_profiles=[{"id": "local", "backend": "local"}],
        sessions_root=tmp_path,
    )

    assert missing == []
    # the endpoint probe is a direct curl to the configured base_url
    assert seen["argv"][0] == "curl"
    assert "https://api.deepseek.example/v1" in seen["argv"]
    assert seen["env"]["OPENAI_API_KEY"] == "secret"


def test_profile_account_probe_runs_minimal_model_with_injected_account(tmp_path, monkeypatch):
    root = tmp_path / "_secrets" / "accounts" / "pi-main"
    root.mkdir(parents=True)
    (root / "API_KEY").write_text("deepseek-secret\n")

    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        # the probe now passes the credential env EXPLICITLY to the subprocess
        # (env=) instead of mutating the global os.environ — that's what makes
        # parallel probes safe. Read it from the passed env, not os.environ.
        seen["key"] = (kwargs.get("env") or {}).get("DEEPSEEK_API_KEY")
        return subprocess.CompletedProcess(argv, 0, '{"type":"agent_settled"}\n', "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("MUTEKI_PI_PROVIDER", "deepseek")
    missing = _missing_profile_accounts(
        worker_profiles=[{
            "id": "pi-sub",
            "name": "pi-sub",
            "engine": "pi",
            "transport": "pi_cli",
            "credential_mode": "subscription",
            "credential_account": "pi-main",
            "runtime": "docker-web",
            "enabled": True,
        }],
        runtime_profiles=[{"id": "docker-web", "backend": "container"}],
        sessions_root=tmp_path,
    )

    assert missing == []
    assert seen["key"] == "deepseek-secret"
    assert any("Reply with exactly: OK" in str(x) for x in seen["argv"])


def test_offline_rejects_custom_endpoint_profiles(tmp_path):
    mgr = RunManager(sessions_root=tmp_path)
    driver = build_driver({
        "prompt": "solve http://example.test",
        "offline": True,
        "engines": ["pi-endpoint"],
        "worker_profiles": [{
            "id": "pi-endpoint",
            "name": "pi-endpoint",
            "engine": "pi",
            "transport": "pi_cli",
            "credential_mode": "api",
            "credential_account": "pi-main",
            "base_url": "https://api.deepseek.example/v1",
            "api_key_ref": "env:MUTEKI_DEEPSEEK_API_KEY",
            "runtime": "local",
            "enabled": True,
        }],
        "runtime_profiles": [{"id": "local", "backend": "local"}],
    }, mgr=mgr)
    run = mgr.create("endpoint-offline")

    with pytest.raises(RuntimeError, match="offline eval cannot use custom endpoint"):
        asyncio.run(driver(run))


# ── llm_profiles base_url (DESIGN §2.2 補強A) ────────────────────────────────

def test_llm_profiles_base_url_accepted_and_normalized(tmp_path):
    """planner/titler accept an OpenAI-compatible base_url; garbage → "". The
    API key is NOT stored here (stays in .env)."""
    WorkerConfigStore(root=tmp_path).set(llm_profiles={
        "planner": {"provider": "deepseek", "model": "planner-x",
                    "base_url": "  https://api.openai-compat.test/v1  "},
        "titler": {"provider": "deepseek", "model": "titler-x", "base_url": 12345},
    })
    cfg = WorkerConfigStore(root=tmp_path).get()  # fresh load from disk
    # trimmed, persisted
    assert cfg["llm_profiles"]["planner"]["base_url"] == "https://api.openai-compat.test/v1"
    # non-string garbage normalizes to empty (= default DeepSeek), never crashes
    assert cfg["llm_profiles"]["titler"]["base_url"] == ""
    # no api key leaked into config under any common name
    assert "api_key" not in cfg["llm_profiles"]["planner"]
    assert "key" not in cfg["llm_profiles"]["planner"]


def test_llm_profiles_base_url_defaults_empty(tmp_path):
    cfg = WorkerConfigStore(root=tmp_path).get()
    assert cfg["llm_profiles"]["planner"]["base_url"] == ""
    assert cfg["llm_profiles"]["titler"]["base_url"] == ""


# ── runtime-environment write-back (DESIGN §5) ───────────────────────────────

def test_set_runtime_environment_unifies_all_enabled_profiles(tmp_path):
    """Choosing local/container rewrites EVERY enabled profile's runtime —
    including profiles only referenced by a category override, not in the default
    engines (reviewer P2). Otherwise an override keeps an old runtime and the
    "displayed local, actually container" bug returns."""
    store = WorkerConfigStore(root=tmp_path)
    # seed profiles: one default + one only used by a pwn override, with DIFFERENT
    # runtimes so we can prove both get rewritten.
    store.set(
        worker_profiles=[
            {"id": "pi-web-sub", "name": "pi-web-sub", "engine": "pi",
             "transport": "pi_cli", "credential_mode": "subscription",
             "credential_account": "pi-main", "runtime": "docker-web",
             "enabled": True},
            {"id": "pi-pwn-override", "name": "pi-pwn-override", "engine": "pi",
             "transport": "pi_cli", "credential_mode": "subscription",
             "credential_account": "pi-main", "runtime": "docker-pwn-heavy",
             "enabled": True},
        ],
        engines=["pi-web-sub"],  # only pi-web-sub is a DEFAULT engine
    )
    store.set(overrides={"pwn": {"engines": ["pi-pwn-override"]}})

    # flip the whole run to local
    cfg = store.set_runtime_environment(backend="local", runtime_id="local")
    assert cfg["worker_backend"] == "local"
    runtimes = {p["id"]: p["runtime"] for p in cfg["worker_profiles"]}
    # BOTH the default engine AND the override-only profile got rewritten
    assert runtimes["pi-web-sub"] == "local"
    assert runtimes["pi-pwn-override"] == "local"

    # flip to a container recipe
    cfg = store.set_runtime_environment(backend="container", runtime_id="docker-offline")
    assert cfg["worker_backend"] == "container"
    runtimes = {p["id"]: p["runtime"] for p in cfg["worker_profiles"]}
    assert runtimes["pi-web-sub"] == "docker-offline"
    assert runtimes["pi-pwn-override"] == "docker-offline"


def test_set_runtime_environment_keeps_direction_profile_names(tmp_path):
    # Pi-only roster: the seven direction profile NAMES are the ids — flipping
    # the backend rewrites each profile's runtime, never the name.
    store = WorkerConfigStore(root=tmp_path)

    local = store.set_runtime_environment(backend="local", runtime_id="local")
    local_ids = [p["id"] for p in local["worker_profiles"]]
    assert local_ids == DEFAULT_ENGINES
    assert local["engines"] == local_ids
    assert local["stage_policy"]["coordinator"]["review"]["engine"] == "pi-web"
    assert all(p["runtime"] == "local" for p in local["worker_profiles"])

    container = store.set_runtime_environment(backend="container", runtime_id="docker-web")
    container_ids = [p["id"] for p in container["worker_profiles"]]
    assert container_ids == DEFAULT_ENGINES
    assert container["engines"] == container_ids
    assert container["stage_policy"]["coordinator"]["review"]["engine"] == "pi-web"
    assert all(p["runtime"] == "docker-web" for p in container["worker_profiles"])


def test_get_drops_stale_refs_and_falls_back_to_enabled_seats(tmp_path):
    # A legacy-shaped config whose refs don't match any current profile name must
    # degrade safely: the engines lineup falls back to the enabled seats, stale
    # race refs are dropped, and an unknown review engine falls back to a
    # review-capable profile — never a crash.
    raw = {
        "worker_backend": "local",
        "engines": ["pi-web-container", "pi-pwn-container"],
        "race_engines": ["pi-web-container"],
        "stage_policy": {
            "race": {"enabled": True, "timeout": 720, "engines": ["pi-pwn-container"]},
            "coordinator": {"review": {"enabled": True, "engine": "pi-web-container"}},
        },
        "worker_profiles": [
            {**DEFAULT_WORKER_PROFILES[0], "id": "pi-web", "name": "pi-web", "runtime": "local"},
            {**DEFAULT_WORKER_PROFILES[1], "id": "pi-pwn", "name": "pi-pwn", "runtime": "local"},
        ],
    }
    root = tmp_path / "_worker_config.json"
    root.write_text(__import__("json").dumps(raw), encoding="utf-8")

    cfg = WorkerConfigStore(root=tmp_path).get()

    assert cfg["engines"] == ["pi-web", "pi-pwn"]
    assert cfg["race_engines"] == []
    assert cfg["stage_policy"]["race"]["engines"] == ["pi-pwn-container"]
    assert cfg["stage_policy"]["coordinator"]["review"]["engine"] == "pi-web"


def test_set_rejects_stale_unknown_profile_refs(tmp_path):
    # A set() whose engines name no current profile must fail loudly (ValueError),
    # not silently persist an empty lineup.
    store = WorkerConfigStore(root=tmp_path)
    local = store.set_runtime_environment(backend="local", runtime_id="local")

    with pytest.raises(ValueError, match="at least one enabled worker profile"):
        store.set(
            engines=["pi-web-container", "pi-pwn-container"],
            worker_profiles=local["worker_profiles"],
        )


def test_set_runtime_environment_rejects_backend_runtime_mismatch(tmp_path):
    store = WorkerConfigStore(root=tmp_path)
    with pytest.raises(ValueError, match="not 'local'"):
        store.set_runtime_environment(backend="local", runtime_id="docker-web")
    with pytest.raises(ValueError, match="unknown runtime"):
        store.set_runtime_environment(backend="container", runtime_id="nope")
