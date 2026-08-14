from __future__ import annotations

from apps.web.http_utils import project_probe_result
from dswarm.solver.profile_health import ProfileHealth


def test_project_probe_result_projects_dataclass_and_computed_ok() -> None:
    health = ProfileHealth(
        profile_id="pi-web",
        engine="pi",
        backend="container",
        status="auth_failed",
        layer="auth",
        blocker=None,
        detail="api_error_status:403",
        model="deepseek-chat",
        account_id="pi-main",
        binding_kind="explicit",
        effective_credential_id="pi-main",
    )

    payload = project_probe_result(
        health,
        fields=(
            "profile_id", "engine", "backend", "status", "layer", "blocker",
            "detail", "model", "account_id", "binding_kind", "effective_credential_id",
        ),
        include_ok=True,
        omit_none=True,
        extras={"worker_id": "Web"},
    )

    assert payload == {
        "profile_id": "pi-web",
        "engine": "pi",
        "backend": "container",
        "status": "auth_failed",
        "layer": "auth",
        "detail": "api_error_status:403",
        "model": "deepseek-chat",
        "account_id": "pi-main",
        "binding_kind": "explicit",
        "effective_credential_id": "pi-main",
        "ok": False,
        "worker_id": "Web",
    }


def test_project_probe_result_preserves_dict_probe_keys_without_renaming() -> None:
    result = {
        "ok": False,
        "detail": "认证失败",
        "error_layer": "authentication",
        "layer": None,
        "authentication": {"ok": False, "status": 401},
    }

    payload = project_probe_result(
        result,
        fields=("detail", "error_layer", "layer", "authentication"),
        include_ok=True,
        omit_none=True,
    )

    assert payload == {
        "detail": "认证失败",
        "error_layer": "authentication",
        "authentication": {"ok": False, "status": 401},
        "ok": False,
    }



def test_credential_account_test_endpoint_preserves_legacy_compact_shape(tmp_path, monkeypatch) -> None:
    from starlette.testclient import TestClient

    from apps.web.run_manager import RunManager
    from apps.web.server import create_app

    monkeypatch.delenv("DSWARM_WEB_PASSWORD", raising=False)

    def fake_probe_account(**kwargs):
        assert kwargs["engine"] == "pi"
        assert kwargs["account_id"] == "pi-main"
        assert kwargs["backend"] == "container"
        return {"ok": False, "detail": "bad key", "layer": "auth"}

    monkeypatch.setattr("apps.web.account_test.probe_account", fake_probe_account)
    app = create_app(RunManager(sessions_root=str(tmp_path)))
    with TestClient(app) as client:
        response = client.post(
            "/api/settings/credential-accounts/pi-main/test",
            json={"engine": "pi", "backend": "container"},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": False, "detail": "bad key", "layer": "auth"}
