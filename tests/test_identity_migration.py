"""Migration regression suite (plan §6.5) — locks the Phase-A storage/migration
boundary in WorkerConfigStore: legacy configs read into the new model, the new
model round-trips through disk, foreign keys (engines[]/review.engine/race_engines)
survive, the additive API keeps old fields, and the container×system_inherit
legality gate holds.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.web.worker_config import WorkerConfigStore


LEGACY_CONFIG = {
    "worker_backend": "container",
    "worker_profiles": [
        {"id": "pi-web-local", "name": "pi-web-local", "engine": "pi",
         "credential_mode": "subscription", "credential_account": "pi-main",
         "runtime": "docker-web", "model": "deepseek-v4-pro",
         "roles": ["race", "review"], "race": True, "priority": 10, "enabled": True},
        {"id": "pi-pwn-local", "name": "pi-pwn-local", "engine": "pi",
         "credential_mode": "subscription", "credential_account": "",
         "runtime": "docker-web", "model": "deepseek-v4-flash",
         "roles": ["race", "review"], "race": True, "priority": 20, "enabled": True},
    ],
    "engines": ["pi-web-local", "pi-pwn-local"],
    "race_engines": ["pi-web-local"],
    "stage_policy": {
        "race": {"engines": ["pi-pwn-local"]},
        "coordinator": {"review": {"engine": "pi-web-local"}},
    },
}


def _store(tmp_path: Path, cfg: dict, *, accounts: dict[str, str] | None = None) -> WorkerConfigStore:
    """Build a store over a tmp config. `accounts` maps account_id → on-disk mode
    so migration binds an empty profile to a real default account (engine_key)
    instead of host-inherit — mirroring a machine that actually has credentials."""
    (tmp_path / "_worker_config.json").write_text(json.dumps(cfg), encoding="utf-8")
    if accounts:
        # materialize the credential files inspect() keys off, so _account_modes()
        # reports them and the empty-binding pi profile resolves to engine_key.
        root = tmp_path / "_secrets" / "accounts"
        for acct, mode in accounts.items():
            d = root / acct
            d.mkdir(parents=True, exist_ok=True)
            if mode == "api_key":
                (d / "API_KEY").write_text("x")
    return WorkerConfigStore(root=tmp_path)


# accounts present on disk for the round-trip tests (pi-main makes the empty
# pi binding a legal engine_key instead of an illegal container+inherit).
_ACCOUNTS = {"pi-main": "api_key"}


# 1) legacy name refs resolve AND get() exposes new seat ids
def test_reads_legacy_profile_name_refs_and_exposes_seat_ids(tmp_path):
    st = _store(tmp_path, LEGACY_CONFIG)
    cfg = st.get()
    # legacy fields preserved (additive)
    assert [p["name"] for p in cfg["worker_profiles"]] == ["pi-web-local", "pi-pwn-local"]
    assert cfg["engines"] == ["pi-web-local", "pi-pwn-local"]
    # new model attached
    assert len(cfg["seats"]) == 2
    assert all(s["id"].startswith("seat_") for s in cfg["seats"])
    assert cfg["seat_alias"]["pi-web-local"].startswith("seat_pi_")


# 2) new seat-id config round-trips through disk
def test_round_trips_new_seat_id_refs(tmp_path):
    st = _store(tmp_path, LEGACY_CONFIG, accounts=_ACCOUNTS)
    ident = st.get()
    # save the derived new model back as the authoritative on-disk shape
    saved = st.set_identity_model(
        seats=ident["seats"], credentials=ident["credentials"],
        environments=ident["environments"],
    )
    assert len(saved["seats"]) == 2
    # reload from disk → seats persist, legacy projection still present
    st2 = WorkerConfigStore(root=tmp_path)
    cfg2 = st2.get()
    assert len(cfg2["seats"]) == 2
    assert len(cfg2["worker_profiles"]) == 2
    on_disk = json.loads((tmp_path / "_worker_config.json").read_text())
    assert "seats" in on_disk and "credentials" in on_disk


# 3) saving new seats keeps foreign keys from dangling
def test_worker_profiles_rename_cascades_foreign_keys(tmp_path):
    st = _store(tmp_path, LEGACY_CONFIG, accounts=_ACCOUNTS)
    ident = st.get()
    st.set_identity_model(seats=ident["seats"], credentials=ident["credentials"],
                          environments=ident["environments"])
    cfg = WorkerConfigStore(root=tmp_path).get()
    # review.engine must still resolve to a real profile name (not dangling)
    names = {p["name"] for p in cfg["worker_profiles"]}
    assert cfg["stage_policy"]["coordinator"]["review"]["engine"] in names
    # engines roster non-empty
    assert cfg["engines"]


# 4) inflight legacy-name alias still resolves a seat ref
def test_inflight_legacy_name_alias_resolves(tmp_path):
    from dswarm.solver.worker_profiles import resolve_seat_ref
    st = _store(tmp_path, LEGACY_CONFIG)
    cfg = st.get()
    seats = cfg["seats"]
    alias = cfg["seat_alias"]
    # the old review.engine value "pi-web-local" must map to the pi-web seat
    sid = resolve_seat_ref("pi-web-local", seats=seats, alias_table=alias)
    assert sid == alias["pi-web-local"]


# 5) the additive API accepts legacy AND new fields
def test_api_keeps_legacy_fields_additive(tmp_path):
    st = _store(tmp_path, LEGACY_CONFIG)
    cfg = st.get()
    # old + new coexist in the same payload
    for legacy in ("worker_profiles", "engines", "stage_policy"):
        assert legacy in cfg
    for new in ("seats", "credentials", "environments", "seat_alias"):
        assert new in cfg


# 6) /api/engines-shaped consumers still see profile names (round-trip)
def test_engines_roster_preserved_on_reload(tmp_path):
    st = _store(tmp_path, LEGACY_CONFIG, accounts=_ACCOUNTS)
    ident = st.get()
    st.set_identity_model(seats=ident["seats"], credentials=ident["credentials"],
                          environments=ident["environments"])
    cfg = WorkerConfigStore(root=tmp_path).get()
    # engines still names enabled profiles (the scheduler roster)
    names = {p["name"] for p in cfg["worker_profiles"]}
    assert set(cfg["engines"]) <= names
    assert len(cfg["engines"]) == 2


def test_new_shape_overrides_remap_legacy_profile_names(tmp_path):
    st = _store(tmp_path, LEGACY_CONFIG, accounts=_ACCOUNTS)
    ident = st.get()
    st.set_identity_model(seats=ident["seats"], credentials=ident["credentials"],
                          environments=ident["environments"])
    path = tmp_path / "_worker_config.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["overrides"] = {"web": {"engines": ["pi-web-local"], "start_workers": 1}}
    path.write_text(json.dumps(raw), encoding="utf-8")

    cfg = WorkerConfigStore(root=tmp_path).get()

    sid = cfg["seat_alias"]["pi-web-local"]
    assert cfg["overrides"]["web"]["engines"] == [sid]
    assert WorkerConfigStore(root=tmp_path).resolve("web")["engines"] == [sid]


# 7) empty binding migrates to inherited (pi-pwn-local), bound to default account
def test_empty_binding_becomes_inherited_credential(tmp_path):
    st = _store(tmp_path, LEGACY_CONFIG)
    cfg = st.get()
    pwn_seat = next(s for s in cfg["seats"] if s["model"] == "deepseek-v4-flash")
    cred = next(c for c in cfg["credentials"] if c["id"] == pwn_seat["credential_id"])
    # with no account_modes on disk in tmp, the empty binding falls back to host-inherit
    assert cred["kind"] in ("system_inherit", "engine_key")


# 8) model pin survives migration (into seat + back into legacy profile)
def test_model_pin_survives_migration(tmp_path):
    st = _store(tmp_path, LEGACY_CONFIG)
    cfg = st.get()
    web_seat = next(s for s in cfg["seats"] if s["model"] == "deepseek-v4-pro")
    assert web_seat["model"] == "deepseek-v4-pro"
    # and the adapted legacy profile still carries it (for the driver)
    web_prof = next(p for p in cfg["worker_profiles"] if p["name"] == "pi-web-local")
    assert web_prof["model"] == "deepseek-v4-pro"


# 9) race:false survives migration and still gates the race role
def test_race_false_survives_migration(tmp_path):
    cfg = dict(LEGACY_CONFIG)
    cfg["worker_profiles"] = [dict(LEGACY_CONFIG["worker_profiles"][0], race=False)]
    cfg["engines"] = ["pi-web-local"]
    cfg["race_engines"] = []
    cfg["stage_policy"] = {"coordinator": {"review": {"engine": "pi-web-local"}}}
    st = _store(tmp_path, cfg)
    got = st.get()
    seat = got["seats"][0]
    assert seat["race"] is False
    assert got["worker_profiles"][0]["race"] is False


# 10) binding fields are additive (don't break old health consumers)
def test_health_binding_fields_are_additive():
    from dswarm.solver.profile_health import ProfileHealth
    h = ProfileHealth(
        profile_id="p", engine="pi", backend="container", status="ok",
        layer=None, blocker=None, detail="ok", model="", account_id="pi-main",
    )
    # defaults present, old fields intact
    assert h.binding_kind == "explicit"
    assert h.effective_credential_id == ""
    assert h.ok is True


# §6.5#7) the health route accepts BOTH the old name and the new seat id
def test_profile_health_route_accepts_old_name_and_new_seat_id(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from apps.web.server import create_app
    from apps.web.run_manager import RunManager

    monkeypatch.setattr("dswarm.core.runtime_env.is_web_container", lambda: False)
    monkeypatch.delenv("DSWARM_WEB_PASSWORD", raising=False)

    st = _store(tmp_path, LEGACY_CONFIG, accounts=_ACCOUNTS)
    # force the new-schema-on-disk persistence (mirrors a real UI save)
    st.set(worker_profiles=st.get()["worker_profiles"], engines=st.get()["engines"])
    stored = st.get()
    seat_id = stored["seat_alias"]["pi-web-local"]

    app = create_app(RunManager(sessions_root=str(tmp_path)))
    with TestClient(app) as c:
        # old legacy name resolves
        r1 = c.post("/api/settings/profiles/pi-web-local/health")
        assert r1.status_code == 200
        # new seat id resolves
        r2 = c.post(f"/api/settings/profiles/{seat_id}/health")
        assert r2.status_code == 200
        # genuinely unknown → 404 (not silently swallowed)
        r3 = c.post("/api/settings/profiles/does-not-exist/health")
        assert r3.status_code == 404


# 11) container × system_inherit is rejected at save time
def test_container_system_inherit_rejected_on_save(tmp_path):
    st = WorkerConfigStore(root=tmp_path)
    with pytest.raises(ValueError):
        st.set_identity_model(
            seats=[{"id": "seat_x", "label": "X", "engine": "pi",
                    "credential_id": "cred_h", "environment_id": "docker-web"}],
            credentials=[{"id": "cred_h", "label": "host", "engine": "pi",
                          "kind": "system_inherit", "secret_ref": ""}],
            environments=[{"id": "docker-web", "label": "Docker", "backend": "container"}],
        )


# 12) direction seats map to their direction worker image in the legacy projection
def test_seat_to_legacy_profile_sets_direction_image():
    from dswarm.solver.identity_model import seat_to_legacy_profile
    from dswarm.solver.worker_profiles import DEFAULT_WORKER_IMAGE, direction_image

    cred = {"id": "cred_pi_1", "kind": "engine_key", "secret_ref": "pi-main", "engine": "pi"}
    env = {"id": "docker-web", "backend": "container"}

    def flat(label: str, sid: str = "seat_pi_000001") -> dict:
        return seat_to_legacy_profile(
            {"id": sid, "label": label, "engine": "pi",
             "credential_id": "cred_pi_1", "environment_id": "docker-web",
             "roles": ["recon", "explore"]},
            cred, env,
        )

    # label carries the direction (the UI "Agent" name)
    assert flat("pi-web")["image"] == direction_image("web")
    assert flat("pi-rev")["image"] == direction_image("rev")
    # id may itself be the direction profile id
    assert flat("pi-web", sid="pi-web")["image"] == direction_image("web")
    # case-insensitive (prefix and direction: Pi-Web / pi-AIsec)
    assert flat("Pi-Web")["image"] == direction_image("web")
    assert flat("pi-AIsec")["image"] == direction_image("aisec")
    # generic / non-direction seats keep the base image
    assert flat("pi-worker")["image"] == DEFAULT_WORKER_IMAGE
    assert flat("custom-agent")["image"] == DEFAULT_WORKER_IMAGE


# 13) a new-shape config with direction seats projects direction images store-wide
def test_new_shape_direction_seats_project_to_direction_images(tmp_path):
    from dswarm.solver.worker_profiles import DEFAULT_WORKER_IMAGE, direction_image

    wc = WorkerConfigStore(root=tmp_path)
    wc.set_identity_model(
        credentials=[{"id": "cred_pi_main", "kind": "engine_key",
                      "secret_ref": "pi-main", "engine": "pi"}],
        environments=[{"id": "docker-web", "backend": "container", "label": "Docker web"}],
        seats=[
            {"id": "seat_pi_web_1", "label": "pi-web", "engine": "pi",
             "credential_id": "cred_pi_main", "environment_id": "docker-web",
             "roles": ["recon", "explore", "review"], "race": True, "enabled": True},
            {"id": "seat_pi_pwn_1", "label": "pi-pwn", "engine": "pi",
             "credential_id": "cred_pi_main", "environment_id": "docker-web",
             "roles": ["recon", "explore", "review"], "race": True, "enabled": True},
            {"id": "seat_pi_gen", "label": "pi-worker", "engine": "pi",
             "credential_id": "cred_pi_main", "environment_id": "docker-web",
             "roles": ["recon", "explore", "review"], "race": True, "enabled": True},
        ],
    )
    by_id = {p["id"]: p for p in wc.get()["worker_profiles"]}
    assert by_id["seat_pi_web_1"]["image"] == direction_image("web")
    assert by_id["seat_pi_pwn_1"]["image"] == direction_image("pwn")
    assert by_id["seat_pi_gen"]["image"] == DEFAULT_WORKER_IMAGE
