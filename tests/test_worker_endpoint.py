from __future__ import annotations

from pathlib import Path

import pytest

from apps.web.worker_endpoint import resolve_saved_api_key
from dswarm.solver.endpoint_probe import (
    auth_headers,
    endpoint_url,
    normalize_base_url,
    probe_endpoint,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://api.example.com", "https://api.example.com"),
        ("https://api.example.com/v1/", "https://api.example.com/v1"),
        ("https://api.example.com/v1/models", "https://api.example.com/v1"),
        ("https://api.example.com/v1/chat/completions", "https://api.example.com/v1"),
        ("https://api.example.com/v1/responses", "https://api.example.com/v1"),
    ],
)
def test_normalize_base_url_preserves_api_prefix(raw: str, expected: str) -> None:
    assert normalize_base_url(raw) == expected
    assert endpoint_url(raw, "models") == f"{expected}/models"


def test_auth_headers_support_all_modes() -> None:
    assert auth_headers({"auth_mode": "bearer"}, "secret") == {"Authorization": "Bearer secret"}
    assert auth_headers({"auth_mode": "x-api-key"}, "secret") == {"x-api-key": "secret"}
    assert auth_headers({"auth_mode": "custom", "auth_header": "X-Key", "auth_prefix": "Token"}, "secret") == {"X-Key": "Token secret"}


def test_models_probe_parses_openai_list_and_never_returns_key() -> None:
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return 200, {"data": [{"id": "model-a"}, {"id": "model-b", "name": "B"}]}, "ok"

    result = probe_endpoint({"base_url": "https://api.example.com/v1"}, api_key="top-secret", request_json=request)
    assert result["ok"] is True
    assert [row["id"] for row in result["models"]] == ["model-a", "model-b"]
    assert calls[0][0:2] == ("GET", "https://api.example.com/v1/models")
    assert "top-secret" not in repr(result)


def test_401_is_authentication_error_and_stops_before_model_call() -> None:
    calls = []
    def request(method, url, **kwargs):
        calls.append((method, url))
        return 401, {"error": {"message": "bad key"}}, "bad key"

    result = probe_endpoint({"base_url": "https://api.example.com/v1", "model": "m"},
                            api_key="bad", validate_model=True, request_json=request)
    assert result["ok"] is False
    assert result["error_layer"] == "authentication"
    assert "认证失败" in result["detail"]
    assert calls == [("GET", "https://api.example.com/v1/models")]


def test_auto_falls_back_to_responses_only_for_protocol_mismatch() -> None:
    calls = []
    def request(method, url, **kwargs):
        calls.append(url)
        if method == "GET":
            return 200, {"data": [{"id": "m"}]}, "ok"
        if url.endswith("/chat/completions"):
            return 404, {"error": "unknown endpoint"}, "unknown endpoint"
        return 200, {"id": "resp"}, "ok"

    result = probe_endpoint({"base_url": "https://api.example.com/v1", "model": "m", "wire_api": "auto"},
                            api_key="key", validate_model=True, request_json=request)
    assert result["ok"] is True
    assert result["detected_wire_api"] == "openai-responses"
    assert calls[-2:] == ["https://api.example.com/v1/chat/completions", "https://api.example.com/v1/responses"]


def test_auto_does_not_fallback_for_normal_model_error() -> None:
    calls = []
    def request(method, url, **kwargs):
        calls.append(url)
        if method == "GET":
            return 200, {"data": []}, "ok"
        return 400, {"error": {"message": "model not found"}}, "model not found"

    result = probe_endpoint({"base_url": "https://api.example.com/v1", "model": "missing", "wire_api": "auto"},
                            api_key="key", validate_model=True, request_json=request)
    assert result["ok"] is False
    assert calls[-1].endswith("/chat/completions")
    assert not any(url.endswith("/responses") for url in calls)


def test_model_validation_can_run_when_models_endpoint_is_unavailable() -> None:
    def request(method, url, **kwargs):
        if method == "GET":
            return 404, None, "not found"
        return 200, {"choices": []}, "ok"

    result = probe_endpoint({"base_url": "https://api.example.com/v1", "model": "manual", "wire_api": "openai-chat"},
                            api_key="key", validate_model=True, request_json=request)
    assert result["ok"] is True
    assert result["model_discovery"]["ok"] is False
    assert result["model_probe"]["ok"] is True


def test_resolve_saved_key_forces_openai_credential_mapping(tmp_path: Path) -> None:
    account = tmp_path / "_secrets" / "accounts" / "pi-main"
    account.mkdir(parents=True)
    (account / "API_KEY").write_text("saved-key\n", encoding="utf-8")
    assert resolve_saved_api_key({"credential_account": "pi-main"}, tmp_path) == "saved-key"
