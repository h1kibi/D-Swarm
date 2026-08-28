"""Default worker-roster config (apps/web/worker_config.py) + operator runtime
worker commands (RunManager.post_worker_cmd). Pure/unit 鈥?no subprocess, no key."""

from __future__ import annotations

import asyncio
import copy
import os
import subprocess

import pytest

from apps.web.run_manager import RunManager
from apps.web.drivers import _missing_profile_accounts
from apps.web.drivers import build_driver
from apps.web.worker_config import (
    DEFAULT_CATEGORY_OVERRIDES,
    DEFAULT_ENGINES,
    DEFAULT_MAX_WORKERS,
    DEFAULT_RUNTIME_PROFILES,
    DEFAULT_WORKER_PROFILES,
    DEFAULT_DEEPSEEK_BASE_URL,
    WorkerConfigStore,
)


# 鈹€鈹€ default config + validation 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_config_defaults_when_empty(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    cfg = wc.get()
    assert cfg["engines"] == []
    assert "start_workers" not in cfg
    assert cfg["max_workers"] == DEFAULT_MAX_WORKERS
    assert cfg["worker_backend"] == "container"
    assert cfg["wall_clock_budget"] == 0
    assert cfg["max_total_workers"] == 0
    assert cfg["cost_budget_usd"] == 0.0
    assert cfg["max_total_workers"] == 0
    assert cfg["review_policy"]["enabled"] is False
    assert cfg["review_policy"]["engine"] == "pi-worker"
    assert cfg["review_policy"]["candidate_spike_threshold"] == 5
    assert cfg["llm_profiles"]["planner"]["model"] == "deepseek-v4-pro"
    assert cfg["runtime_profiles"] == DEFAULT_RUNTIME_PROFILES
    assert {r["id"] for r in cfg["runtime_profiles"]} >= {
        "docker-host-target", "docker-offline", "docker-pwn-heavy"}
    assert cfg["worker_profiles"] == DEFAULT_WORKER_PROFILES
    assert cfg["overrides"] == {}
    assert all(profile["enabled"] is False for profile in cfg["worker_profiles"])


def _enabled_default_profiles(*names: str) -> list[dict]:
    enabled = set(names)
    profiles = copy.deepcopy(DEFAULT_WORKER_PROFILES)
    for profile in profiles:
        profile["enabled"] = profile["name"] in enabled
    return profiles


def test_default_profiles_leave_room_for_bootstrap_and_explore():
    # Each routed direction uses one profile. Its default capacity must
    # allow the coordinator bootstrap worker and one focused explore worker
    # to coexist; otherwise every category run rejects explore immediately.
    assert all(p["max_running"] >= 2 for p in DEFAULT_WORKER_PROFILES)


def test_default_profiles_have_agent_images():
    assert all(p.get("image") for p in DEFAULT_WORKER_PROFILES)
    by_name = {p["name"]: p["image"] for p in DEFAULT_WORKER_PROFILES}
    assert by_name["pi-worker"] == "ctf-swarm-pi:0.2.0"


def test_default_profiles_bind_pi_accounts():
    by_name = {p["name"]: p for p in DEFAULT_WORKER_PROFILES}
    assert by_name["pi-worker"]["credential_account"] == "pi-main"
    for direction in (
        "web", "pwn", "rev", "crypto", "misc", "forensics", "aisec"
    ):
        assert by_name[f"pi-{direction}"]["credential_account"] == f"pi-{direction}-main"


def test_default_direction_profiles_use_direction_images():
    expected = {
        "pi-web": "ctf-swarm-pi-web:0.2.0",
        "pi-pwn": "ctf-swarm-pi-pwn:0.2.0",
        "pi-rev": "ctf-swarm-pi-rev:0.2.0",
        "pi-crypto": "ctf-swarm-pi-crypto:0.2.0",
        "pi-misc": "ctf-swarm-pi-misc:0.2.0",
        "pi-forensics": "ctf-swarm-pi-forensics:0.2.0",
        "pi-aisec": "ctf-swarm-pi-aisec:0.2.0",
    }
    by_name = {p["name"]: p["image"] for p in DEFAULT_WORKER_PROFILES}
    for name, image in expected.items():
        assert by_name[name] == image


def test_default_profiles_use_flash_medium_and_equal_direction_priority():
    by_name = {p["name"]: p for p in DEFAULT_WORKER_PROFILES}
    assert by_name["pi-worker"]["model"] == "deepseek-v4-flash"
    assert by_name["pi-worker"]["effort"] == "medium"
    for direction in (
        "web", "pwn", "rev", "crypto", "misc", "forensics", "aisec"
    ):
        profile = by_name[f"pi-{direction}"]
        assert profile["model"] == "deepseek-v4-flash"
        assert profile["effort"] == "medium"
        assert profile["priority"] == 20


def test_default_profiles_show_deepseek_base_url():
    assert all(p["base_url"] == DEFAULT_DEEPSEEK_BASE_URL for p in DEFAULT_WORKER_PROFILES)


def test_account_endpoint_overrides_deepseek_default(tmp_path, monkeypatch):
    from dswarm.solver.credential_accounts import CredentialAccountStore, account_store_root

    root = tmp_path / "sessions"
    store = CredentialAccountStore(account_store_root(root))
    store.upsert_secret(
        account_id="pi-main",
        engine="api",
        secret="secret",
        base_url="https://gateway.example/v1",
        target_engine="pi",
    )
    cfg = WorkerConfigStore(root=root).get()
    by_name = {p["name"]: p for p in cfg["worker_profiles"]}
    assert by_name["pi-worker"]["base_url"] == "https://gateway.example/v1"


def test_worker_profile_normalization_keeps_effort():
    from dswarm.solver.worker_profiles import normalize_worker_profiles

    profiles = normalize_worker_profiles([
        {"id": "pi-web", "name": "pi-web", "engine": "pi", "effort": "high"}
    ])
    assert profiles[0]["effort"] == "high"


def test_config_set_engines_dedupes_and_filters(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    cfg = wc.set(
        engines=["pi-worker", "bogus", "pi-worker", "pi-worker"],
        worker_profiles=_enabled_default_profiles("pi-worker"),
    )
    assert cfg["engines"] == ["pi-worker"]


# 鈹€鈹€ seat roster tracks enabled toggles (regression) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
# Bug: the seat UI showed 3 seats enabled, but a stale top-level `engines`
# lineup (left from an older config) won at get() 鈥?it short-circuits the
# "else enabled seats" fallback 鈥?so dispatch raced only the one stale engine.
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
    # 3 enabled seats, but a stale roster naming only one (the exact bug shape).
    wc.set_identity_model(seats=[
        _seat("seat_pi_web_x", "pi"),
        _seat("seat_pi_pwn_x", "pi"),
        _seat("seat_pi_rev_x", "pi"),
    ])
    # inject a stale roster the way a legacy config would carry it, then re-project.
    wc._data["engines"] = ["pi"]
    wc._project_identity_to_legacy()
    engines = wc.get()["engines"]
    # all three enabled seats race now 鈥?not just the stale bare-engine "pi".
    assert set(engines) == {"seat_pi_web_x", "seat_pi_pwn_x", "seat_pi_rev_x"}


def test_seat_lineup_drops_disabled_seat(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    wc.set_identity_model(seats=[
        _seat("seat_pi_web_x", "pi"),
        _seat("seat_pi_pwn_x", "pi", enabled=False),  # disabled 鈫?out of roster
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
        wc.set(engines=["pi-worker"], worker_backend="local")


def test_backend_stale_local_coerced_on_read_in_web_container(tmp_path, monkeypatch):
    # A config persisted as local on a bare host, then loaded inside a container,
    # is silently COERCED to container on read (get) 鈥?never reaches the swarm as
    # local. Persist local on a host, then flip is_web_container True and re-read.
    import apps.web.worker_config as wcmod
    monkeypatch.setattr(wcmod, "is_web_container", lambda: False)
    wc = WorkerConfigStore(root=tmp_path)
    wc.set(engines=["pi-worker"], worker_backend="local")
    assert wc.get()["worker_backend"] == "local"   # host: preserved
    monkeypatch.setattr(wcmod, "is_web_container", lambda: True)
    assert wc.get()["worker_backend"] == "container"  # container: coerced on read


def test_backend_local_preserved_on_bare_host(tmp_path, monkeypatch):
    # On a bare host the local backend is preserved (historical behaviour).
    import apps.web.worker_config as wcmod
    monkeypatch.setattr(wcmod, "is_web_container", lambda: False)
    wc = WorkerConfigStore(root=tmp_path)
    cfg = wc.set(engines=["pi-worker"], worker_backend="local")
    assert cfg["worker_backend"] == "local"


def test_config_set_rejects_empty_engine_list(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    with pytest.raises(ValueError):
        wc.set(engines=["nope", "als芯_bad"])  # nothing valid 鈫?reject


def test_config_set_rejects_nonpositive_counts(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    with pytest.raises(ValueError):
        wc.set(max_workers=-3)


def test_config_max_workers_is_derived_from_roster_sum(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    cfg = wc.set(engines=['pi-worker'], max_workers=10, worker_profiles=[{**DEFAULT_WORKER_PROFILES[0], 'max_running': 3}])
    assert cfg['max_workers'] == 3


def test_config_set_does_not_balloon_sole_eligible_seat(tmp_path):
    # Regression (Bug B): a stale single-engine dispatch lineup (cursor-only) used
    # to make cursor the ONLY eligible profile, so the auto-grow loop ballooned
    # cursor's max_running up to max_workers 鈥?any value the user typed "reverted"
    # on every save. Now seats are NEVER mutated; cursor keeps exactly what was
    # set, and max_workers just equals that seat's capacity.
    wc = WorkerConfigStore(root=tmp_path)
    profiles = [
        {**p, "max_running": 1 if p["name"] == "pi-worker" else p["max_running"]}
        for p in DEFAULT_WORKER_PROFILES
    ]
    cfg = wc.set(
        engines=["pi-worker"],  # stale single-profile dispatch lineup
        max_workers=6,
        worker_profiles=profiles,
    )
    by_name = {p["name"]: p for p in cfg["worker_profiles"]}
    assert by_name["pi-worker"]["max_running"] == 1  # NOT bumped to 6
    assert cfg["max_workers"] == 1  # derived = the one eligible seat's cap


def test_config_max_workers_tracks_roster_edit_up_and_down(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    wc.set(engines=['pi-worker'], worker_profiles=[{**DEFAULT_WORKER_PROFILES[0], 'max_running': 2}])
    assert wc.set(worker_profiles=[{**DEFAULT_WORKER_PROFILES[0], 'max_running': 4}])['max_workers'] == 4
    assert wc.set(worker_profiles=[{**DEFAULT_WORKER_PROFILES[0], 'max_running': 1}])['max_workers'] == 1


def test_config_max_workers_counts_only_dispatched_seats(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    profiles = [{**DEFAULT_WORKER_PROFILES[0], 'max_running': 2}]
    cfg = wc.set(engines=['pi-worker'], worker_profiles=profiles)
    assert cfg['max_workers'] == 2


def test_config_dedicated_review_seat_does_not_inflate_max_workers(tmp_path):
    # The review worker is an INDEPENDENT seat. A review-only profile (roles ==
    # ["review"], no ordinary race/bootstrap/explore/respond role) must NOT count
    # toward max_workers (that ceiling gates ORDINARY worker concurrency only,
    # which the swarm enforces via max_running / _active_profile_counts 鈥?a path
    # totally separate from review's max_review_running / _active_review_profile_
    # counts). Its review concurrency field must also survive untouched. This
    # guards against folding review capacity into the ordinary ceiling.
    wc = WorkerConfigStore(root=tmp_path)
    ordinary = [
        {**p, "max_running": 2} for p in DEFAULT_WORKER_PROFILES if p["name"] == "pi-worker"
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
        engines=["pi-worker"],            # only the ordinary pi-worker seat is dispatched
        worker_profiles=ordinary + [review_seat],
    )
    # Derived ceiling = the ordinary pi-worker seat's max_running (2) ONLY. The
    # review-only seat's max_running (9) is excluded 鈥?NOT 2+9=11.
    assert cfg["max_workers"] == 2
    saved_review = next(p for p in cfg["worker_profiles"] if p["name"] == "review-only")
    # review-only stayed review-only (no ordinary role auto-appended) and its
    # review concurrency is preserved verbatim 鈥?we never mutate it.
    assert "review" in saved_review["roles"]
    assert not ({"race", "bootstrap", "explore", "respond"} & set(saved_review["roles"]))
    assert saved_review["max_review_running"] == 1
    assert saved_review["max_running"] == 9  # untouched even though excluded


def test_config_uses_profile_capacity_without_bootstrap_count(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    cfg = wc.set(max_workers=3)

    assert "start_workers" not in cfg
    assert cfg["max_workers"] == 3


def test_config_persists_across_reload(tmp_path):
    WorkerConfigStore(root=tmp_path).set(
                                         engines=["pi-worker"],
                                         worker_profiles=_enabled_default_profiles("pi-worker"),
                                         max_workers=4, worker_backend="container",
                                         wall_clock_budget=1800,
                                         max_total_workers=11,
                                         cost_budget_usd=1.25,
                                         review_policy={
                                             "enabled": True,
                                             "engine": "pi-worker",
                                             "timeout": 333,
                                             "after_fruitless_workers": 2,
                                             "candidate_spike_threshold": 4,
                                         },
                                         llm_profiles={
                                             "planner": {"provider": "deepseek", "model": "planner-x"},
                                             "titler": {"provider": "deepseek", "model": "titler-x"},
                                         })
    cfg = WorkerConfigStore(root=tmp_path).get()  # fresh load from disk
    enabled_system = next(
        p for p in cfg["worker_profiles"]
        if p.get("label") == "pi-worker" and p.get("enabled")
    )
    assert cfg["engines"] == [enabled_system["id"]]
    # max_workers is derived from the dispatched seat capacity; the default
    # pi-worker seat now has two slots for bootstrap plus explore.
    assert "start_workers" not in cfg and cfg["max_workers"] == 3
    assert cfg["worker_backend"] == "container"
    assert cfg["wall_clock_budget"] == 1800
    assert cfg["max_total_workers"] == 11
    assert cfg["cost_budget_usd"] == 1.25
    assert (
        cfg["review_policy"]["engine"]
        == enabled_system["id"]
    )
    assert cfg["review_policy"]["timeout"] == 333
    assert cfg["review_policy"]["after_fruitless_workers"] == 2
    assert cfg["review_policy"]["candidate_spike_threshold"] == 4
    assert cfg["llm_profiles"]["titler"]["model"] == "titler-x"


# 鈹€鈹€ per-category override + resolve 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_resolve_uses_default_without_override(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    wc.set(
        engines=["pi-worker", "pi-worker", "pi-worker"],
        worker_profiles=_enabled_default_profiles("pi-worker"),
        worker_backend="container",
    )
    r = wc.resolve("unsorted")  # a category with no default direction override
    assert r["engines"] == ["pi-worker"]
    assert "start_workers" not in r
    assert r["worker_backend"] == "container"
    assert r["worker_profiles"] == wc.get()["worker_profiles"]
    assert r["runtime_profiles"] == wc.get()["runtime_profiles"]


def test_resolve_applies_category_override(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    wc.set(
        engines=["pi-worker", "pi-worker", "pi-worker"],
        worker_profiles=_enabled_default_profiles("pi-worker"),
        overrides={"pwn": {"engines": ["pi-worker"], "start_workers": 2}},
    )
    r = wc.resolve("pwn")
    assert r["engines"] == ["pi-worker"]
    assert "start_workers" not in r
    assert wc.get()["overrides"]["pwn"] == {"engines": ["pi-worker"]}
    # a category with no override still gets the defaults
    assert wc.resolve("web")["engines"] == ["pi-worker"]


def test_resolve_override_ignores_retired_bootstrap_count(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    wc.set(
        engines=["pi-worker"],
        worker_profiles=_enabled_default_profiles("pi-worker"),
        overrides={"crypto": {"engines": ["pi-worker"]}},  # no start_workers
    )
    resolved = wc.resolve("crypto")
    assert "start_workers" not in resolved
    assert resolved["max_workers"] == 3


def test_legacy_config_without_profile_enablement_stays_disabled(tmp_path):
    # A roster-only legacy file has no per-direction enablement state. The new
    # workspace must not silently launch fresh direction workers until the
    # operator explicitly configures and enables them.
    (tmp_path / "_worker_config.json").write_text(
        '{"engines": ["pi-worker", "pi-worker"], "start_workers": 1}',
        encoding="utf-8",
    )
    resolved = WorkerConfigStore(root=tmp_path).resolve("web")
    assert resolved["engines"] == []
    assert "start_workers" not in resolved
    assert resolved["max_workers"] == 0


def test_direction_profile_names_are_real_not_aliases(tmp_path):
    """The seven direction profile names must survive cleaning and routing."""
    wc = WorkerConfigStore(root=tmp_path)
    by_name = {p["name"]: p for p in wc.get()["worker_profiles"]}
    assert {"pi-web", "pi-pwn", "pi-rev", "pi-crypto",
            "pi-misc", "pi-forensics", "pi-aisec"} <= set(by_name)
    # each direction profile carries its own image tag
    assert by_name["pi-web"]["image"] == "ctf-swarm-pi-web:0.2.0"
    assert by_name["pi-rev"]["image"] == "ctf-swarm-pi-rev:0.2.0"
    # cleaning no longer collapses direction names to pi-worker
    cleaned = wc.get()  # stored engines untouched by defaults
    assert "pi-worker" in {p["name"] for p in cleaned["worker_profiles"]}


def test_category_override_routes_each_direction(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    wc.set(worker_profiles=_enabled_default_profiles(
        "pi-web", "pi-pwn", "pi-rev", "pi-crypto", "pi-misc",
        "pi-forensics", "pi-aisec",
    ))
    assert wc.resolve("web")["engines"] == ["pi-web"]
    assert wc.resolve("pwn")["engines"] == ["pi-pwn"]
    assert wc.resolve("reverse")["engines"] == ["pi-rev"]
    assert wc.resolve("crypto")["engines"] == ["pi-crypto"]
    assert wc.resolve("forensics")["engines"] == ["pi-forensics"]
    assert wc.resolve("aisec")["engines"] == ["pi-aisec"]


def test_clean_engines_matches_direction_names_case_insensitively(tmp_path):
    wc = WorkerConfigStore(root=tmp_path)
    cleaned = wc.set(
        engines=["pi-AISEC", "pi-WEB"],
        worker_profiles=_enabled_default_profiles("pi-aisec", "pi-web"),
    )
    assert cleaned["engines"] == ["pi-aisec", "pi-web"]


# 鈹€鈹€ RunManager.post_worker_cmd (operator runtime control) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_post_worker_cmd_requires_live_run(tmp_path):
    mgr = RunManager(sessions_root=tmp_path)
    run = mgr.create("run-x")
    # no live task 鈫?rejected (a finished/ghost run has no coordinator to act)
    assert asyncio.run(mgr.post_worker_cmd("run-x", "spawn", engine="pi-worker")) is False


def test_post_worker_cmd_enqueues_for_live_run(tmp_path):
    mgr = RunManager(sessions_root=tmp_path)
    run = mgr.create("run-y")

    async def go():
        run.task = asyncio.create_task(asyncio.sleep(3600))
        ok_spawn = await mgr.post_worker_cmd("run-y", "spawn", engine="pi-worker")
        ok_kill = await mgr.post_worker_cmd("run-y", "kill", solver_id="cli-pi-worker")
        run.task.cancel()
        return ok_spawn, ok_kill, [run.worker_cmds.get_nowait(),
                                   run.worker_cmds.get_nowait()]

    ok_spawn, ok_kill, cmds = asyncio.run(go())
    assert ok_spawn is True and ok_kill is True
    assert cmds[0] == {"action": "spawn", "engine": "pi-worker"}
    assert cmds[1] == {"action": "kill", "solver_id": "cli-pi-worker"}


def test_post_worker_cmd_unknown_run_returns_false(tmp_path):
    mgr = RunManager(sessions_root=tmp_path)
    assert asyncio.run(mgr.post_worker_cmd("ghost", "spawn", engine="pi-worker")) is False


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
            "id": "pi-worker-custom",
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
    assert p["name"] == "pi-worker-custom"
    assert p["credential_mode"] == "oauth_token"
    assert p["roles"] == ["bootstrap", "explore", "review"]
    assert "race" not in p
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
        review_policy={"engine": "review-pi"},
    )
    assert cfg["worker_profiles"][0]["roles"] == ["review"]
    assert cfg["review_policy"]["engine"] == "review-pi"

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


def test_worker_profile_migrates_retired_race_role_to_explore(tmp_path):
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
    assert roles == ["explore", "bootstrap", "review"]


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
        "dswarm.solver.profile_health.detect_system_login", lambda engine, env=None: "absent"
    )
    missing = _missing_profile_accounts(
        worker_profiles=[
            {"id": "pi-worker-sub", "engine": "pi", "runtime": "docker-web",
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
    assert "pi-worker-sub:pi-main" in missing
    assert "pi-api:pi-api-main" in missing
    assert all("local-sub" not in x for x in missing)


def test_missing_profile_accounts_allows_local_system_login_without_account(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "dswarm.solver.profile_health.detect_system_login", lambda engine, env=None: "present"
    )
    monkeypatch.setattr("dswarm.solver.cli_driver.driver_for", lambda profile: type(
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
            "api_key_ref": "env:DSWARM_DEEPSEEK_API_KEY",
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
    assert p["api_key_ref"] == "env:DSWARM_DEEPSEEK_API_KEY"
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
    from dswarm.solver.credential_accounts import CredentialAccountStore, account_store_root
    from dswarm.solver.cli_driver import driver_for

    monkeypatch.setenv("DSWARM_PI_BIN", "/usr/bin/pi")
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
    from dswarm.solver.credential_accounts import CredentialAccountStore, account_store_root
    from dswarm.solver.identity_model import credential_id_for

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
    import dswarm.solver.cli_driver as cli_driver

    root = tmp_path / "_secrets" / "accounts" / "pi-main"
    root.mkdir(parents=True)
    (root / "API_KEY").write_text("secret\n")

    seen = {}

    def fake_probe_endpoint(profile, *, api_key, validate_model=False, **kwargs):
        seen["profile"] = profile
        seen["api_key"] = api_key
        seen["validate_model"] = validate_model
        return {"ok": True, "detail": "模型验证成功"}

    monkeypatch.setattr(cli_driver, "probe_endpoint", fake_probe_endpoint)
    monkeypatch.setenv("DSWARM_PI_PROVIDER", "custom")
    missing = _missing_profile_accounts(
        worker_profiles=[{
            "id": "pi-endpoint",
            "name": "pi-endpoint",
            "engine": "pi",
            "transport": "pi_cli",
            "credential_mode": "api",
            "credential_account": "pi-main",
            "base_url": "https://api.deepseek.example/v1",
            "api_key_ref": "env:DSWARM_DEEPSEEK_API_KEY",
            "runtime": "local",
            "enabled": True,
        }],
        runtime_profiles=[{"id": "local", "backend": "local"}],
        sessions_root=tmp_path,
    )

    assert missing == []
    assert seen["profile"]["base_url"] == "https://api.deepseek.example/v1"
    assert seen["api_key"] == "secret"
    assert seen["validate_model"] is True


def test_profile_account_probe_runs_minimal_model_with_injected_account(tmp_path, monkeypatch):
    root = tmp_path / "_secrets" / "accounts" / "pi-main"
    root.mkdir(parents=True)
    (root / "API_KEY").write_text("deepseek-secret\n")

    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        # the probe now passes the credential env EXPLICITLY to the subprocess
        # (env=) instead of mutating the global os.environ 鈥?that's what makes
        # parallel probes safe. Read it from the passed env, not os.environ.
        seen["key"] = (kwargs.get("env") or {}).get("DEEPSEEK_API_KEY")
        return subprocess.CompletedProcess(argv, 0, '{"type":"agent_settled"}\n', "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("DSWARM_PI_PROVIDER", "deepseek")
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


def test_offline_custom_endpoint_auto_relaxes_strict_network(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    class FakeSwarm:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        async def run(self):
            return type("Result", (), {"flag": None})()

    monkeypatch.setattr("dswarm.swarm.swarm.Swarm", FakeSwarm)
    mgr = RunManager(sessions_root=tmp_path)
    driver = build_driver({
        "prompt": "solve http://example.test",
        "reason_swarm": False,
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
            "api_key_ref": "env:DSWARM_DEEPSEEK_API_KEY",
            "runtime": "local",
            "enabled": True,
        }],
        "runtime_profiles": [{"id": "local", "backend": "local"}],
    })
    run = mgr.create("endpoint-offline")

    asyncio.run(driver(run))

    assert captured["web_access"] is False
    assert captured["kb"] is False
    assert captured["runtime_profiles"] == [{"id": "local", "backend": "local"}]
    async def collect_events():
        return [ev async for ev in run.store.replay(run.run_id)]

    events = asyncio.run(collect_events())
    notice = [
        ev.payload for ev in events
        if getattr(ev, "event_type", None).value == "blackboard.delta"
        and (ev.payload or {}).get("code") == "offline_endpoint_compat"
    ]
    assert notice and notice[0]["strict_offline_effective"] is False


def test_offline_selected_seat_endpoint_does_not_raise_in_container_runtime(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    class FakeSwarm:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        async def run(self):
            return type("Result", (), {"flag": None})()

    monkeypatch.setattr("dswarm.swarm.swarm.Swarm", FakeSwarm)
    mgr = RunManager(sessions_root=tmp_path)
    driver = build_driver({
        "prompt": "solve http://node1.anna.nssctf.cn:26179/",
        "reason_swarm": False,
        "offline": True,
        "engines": ["seat_pi_147fcb"],
        "worker_profiles": [{
            "id": "seat_pi_147fcb",
            "name": "seat_pi_147fcb",
            "engine": "pi",
            "transport": "pi_cli",
            "credential_mode": "api",
            "credential_account": "seat_pi_147fcb",
            "base_url": DEFAULT_DEEPSEEK_BASE_URL,
            "api_key_ref": "env:DSWARM_DEEPSEEK_API_KEY",
            "runtime": "docker-web",
            "enabled": True,
        }],
        "runtime_profiles": [{"id": "docker-web", "backend": "container", "network": "bridge"}],
    })
    run = mgr.create("seat-endpoint-offline")

    asyncio.run(driver(run))

    assert captured["web_access"] is False
    assert captured["kb"] is False
    assert captured["runtime_profiles"][0]["network"] == "bridge"


def test_offline_without_endpoint_keeps_strict_network(tmp_path, monkeypatch):
    captured: dict[str, object] = {}

    class FakeSwarm:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        async def run(self):
            return type("Result", (), {"flag": None})()

    monkeypatch.setattr("dswarm.swarm.swarm.Swarm", FakeSwarm)
    mgr = RunManager(sessions_root=tmp_path)
    driver = build_driver({
        "prompt": "solve http://example.test",
        "reason_swarm": False,
        "offline": True,
        "engines": ["pi-local"],
        "worker_profiles": [{
            "id": "pi-local",
            "name": "pi-local",
            "engine": "pi",
            "transport": "pi_cli",
            "credential_mode": "subscription",
            "credential_account": "pi-main",
            "base_url": "",
            "runtime": "docker-web",
            "enabled": True,
        }],
        "runtime_profiles": [{"id": "docker-web", "backend": "container", "network": "bridge"}],
    })
    run = mgr.create("strict-offline")

    asyncio.run(driver(run))

    assert captured["web_access"] is False
    assert captured["kb"] is False
    assert captured["runtime_profiles"][0]["network"] == "none"


# 鈹€鈹€ llm_profiles base_url (DESIGN 搂2.2 瑁滃挤A) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_llm_profiles_base_url_accepted_and_normalized(tmp_path):
    """planner/titler accept an OpenAI-compatible base_url; garbage 鈫?"". The
    API key is NOT stored here (stays in .env)."""
    WorkerConfigStore(root=tmp_path).set(llm_profiles={
        "planner": {"provider": "deepseek", "model": "planner-x",
                    "base_url": "  https://api.openai-compat.test/v1  "},
        "titler": {"provider": "deepseek", "model": "titler-x", "base_url": 12345},
    })
    cfg = WorkerConfigStore(root=tmp_path).get()  # fresh load from disk
    # trimmed, persisted
    assert cfg["llm_profiles"]["planner"]["base_url"] == "https://api.openai-compat.test/v1"
    # non-string garbage restores the visible DeepSeek default, never crashes
    assert cfg["llm_profiles"]["titler"]["base_url"] == DEFAULT_DEEPSEEK_BASE_URL
    # no api key leaked into config under any common name
    assert "api_key" not in cfg["llm_profiles"]["planner"]
    assert "key" not in cfg["llm_profiles"]["planner"]


def test_llm_profiles_base_url_defaults_deepseek(tmp_path):
    cfg = WorkerConfigStore(root=tmp_path).get()
    assert cfg["llm_profiles"]["planner"]["base_url"] == DEFAULT_DEEPSEEK_BASE_URL
    assert cfg["llm_profiles"]["titler"]["base_url"] == DEFAULT_DEEPSEEK_BASE_URL
    assert cfg["llm_profiles"]["planner"]["timeout"] == 120
    assert cfg["llm_profiles"]["titler"]["effort"] == "low"


# 鈹€鈹€ runtime-environment write-back (DESIGN 搂5) 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€

def test_set_runtime_environment_unifies_all_enabled_profiles(tmp_path):
    """Choosing local/container rewrites EVERY enabled profile's runtime 鈥?
    including profiles only referenced by a category override, not in the default
    engines (reviewer P2). Otherwise an override keeps an old runtime and the
    "displayed local, actually container" bug returns."""
    store = WorkerConfigStore(root=tmp_path)
    # seed profiles: one default + one only used by a pwn override, with DIFFERENT
    # runtimes so we can prove both get rewritten.
    store.set(
        worker_profiles=[
            {"id": "pi-worker-sub", "name": "pi-worker-sub", "engine": "pi",
             "transport": "pi_cli", "credential_mode": "subscription",
             "credential_account": "pi-main", "runtime": "docker-web",
             "enabled": True},
            {"id": "pi-worker-override", "name": "pi-worker-override", "engine": "pi",
             "transport": "pi_cli", "credential_mode": "subscription",
             "credential_account": "pi-main", "runtime": "docker-pwn-heavy",
             "enabled": True},
        ],
        engines=["pi-worker-sub"],  # only pi-worker-sub is a DEFAULT engine
    )
    store.set(overrides={"pwn": {"engines": ["pi-worker-override"]}})

    # flip the whole run to local
    cfg = store.set_runtime_environment(backend="local", runtime_id="local")
    assert cfg["worker_backend"] == "local"
    runtimes = {p["id"]: p["runtime"] for p in cfg["worker_profiles"]}
    # BOTH the default engine AND the override-only profile got rewritten
    assert runtimes["pi-worker-sub"] == "local"
    assert runtimes["pi-worker-override"] == "local"

    # flip to a container recipe
    cfg = store.set_runtime_environment(backend="container", runtime_id="docker-offline")
    assert cfg["worker_backend"] == "container"
    runtimes = {p["id"]: p["runtime"] for p in cfg["worker_profiles"]}
    assert runtimes["pi-worker-sub"] == "docker-offline"
    assert runtimes["pi-worker-override"] == "docker-offline"


def test_set_runtime_environment_keeps_direction_profile_names(tmp_path):
    # Pi-only roster: the seven direction profile NAMES are the ids 鈥?flipping
    # the backend rewrites each profile's runtime, never the name.
    store = WorkerConfigStore(root=tmp_path)

    local = store.set_runtime_environment(backend="local", runtime_id="local")
    local_ids = [p["id"] for p in local["worker_profiles"]]
    assert local_ids == DEFAULT_ENGINES
    assert local["engines"] == []
    assert local["review_policy"]["engine"] == "pi-worker"
    assert local["review_policy"]["enabled"] is False
    assert all(p["runtime"] == "local" for p in local["worker_profiles"])

    container = store.set_runtime_environment(backend="container", runtime_id="docker-web")
    container_ids = [p["id"] for p in container["worker_profiles"]]
    assert container_ids == DEFAULT_ENGINES
    assert container["engines"] == []
    assert container["review_policy"]["engine"] == "pi-worker"
    assert container["review_policy"]["enabled"] is False
    assert all(p["runtime"] == "docker-web" for p in container["worker_profiles"])


def test_get_drops_stale_refs_and_falls_back_to_enabled_seats(tmp_path):
    # A legacy-shaped config whose refs don't match any current profile name must
    # degrade safely: the engines lineup falls back to the enabled seats, stale
    # race refs are dropped, and an unknown review engine falls back to a
    # review-capable profile 鈥?never a crash.
    raw = {
        "worker_backend": "local",
        "engines": ["pi-worker-container", "pi-worker-container"],
        "race_engines": ["pi-worker-container"],
        "stage_policy": {
            "race": {"enabled": True, "timeout": 720, "engines": ["pi-worker-container"]},
            "coordinator": {"review": {"enabled": True, "engine": "pi-worker-container"}},
        },
        "worker_profiles": [
            {**DEFAULT_WORKER_PROFILES[0], "id": "pi-worker", "name": "pi-worker", "runtime": "local", "enabled": True},
        ],
    }
    root = tmp_path / "_worker_config.json"
    root.write_text(__import__("json").dumps(raw), encoding="utf-8")

    cfg = WorkerConfigStore(root=tmp_path).get()

    assert cfg["engines"] == ["pi-worker"]
    assert cfg["review_policy"]["engine"] == "pi-worker"


def test_set_rejects_stale_unknown_profile_refs(tmp_path):
    # A set() whose engines name no current profile must fail loudly (ValueError),
    # not silently persist an empty roster.
    store = WorkerConfigStore(root=tmp_path)
    local = store.set_runtime_environment(backend="local", runtime_id="local")

    with pytest.raises(ValueError, match="at least one enabled worker profile"):
        store.set(
            engines=["pi-worker-container", "pi-worker-container"],
            worker_profiles=local["worker_profiles"],
        )


def test_set_runtime_environment_rejects_backend_runtime_mismatch(tmp_path):
    store = WorkerConfigStore(root=tmp_path)
    with pytest.raises(ValueError, match="not 'local'"):
        store.set_runtime_environment(backend="local", runtime_id="docker-web")
    with pytest.raises(ValueError, match="unknown runtime"):
        store.set_runtime_environment(backend="container", runtime_id="nope")


def test_provider_bound_profile_drops_legacy_endpoint_fields():
    from dswarm.solver.worker_profiles import normalize_worker_profile

    profile = normalize_worker_profile({
        "name": "pi-web",
        "engine": "pi",
        "enabled": True,
        "model": "relay-model",
        "provider_ref": "relay-main",
        "credential_account": "legacy-unused",
        "base_url": "https://old.example.test/v1",
        "api_key_ref": "old-key",
        "wire_api": "openai-responses",
        "auth_mode": "x-api-key",
        "auth_header": "X-API-Key",
        "auth_prefix": "",
    })
    assert profile["provider_ref"] == "relay-main"
    assert profile["credential_account"] == ""
    assert profile["base_url"] == ""
    assert profile["api_key_ref"] == ""
    assert profile["wire_api"] == "auto"
