"""Principal policy (role enforcement) + account runtime guards.

These tests pin the P0 security contract added after the production audit:
account JWTs authenticate (and a deactivated one stops immediately), member
JWTs are forbidden on admin settings surfaces, the P3 operator is admin, and
the account JWT secret may not silently stay on its public dev default once the
backend leaves loopback.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from apps.web import auth as A
from apps.web import principal as P
from apps.web.accounts import runtime as AR
from apps.web.run_manager import RunManager
from apps.web.server import create_app


def _account_token(account_id: str = "acc-1", role: str = "member") -> str:
    payload = {
        "account_id": account_id,
        "username": "tester",
        "role": role,
        "type": "access",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    return pyjwt.encode(payload, AR.jwt_secret(), algorithm="HS256")


def _fake_account(role: str = "member", active: bool = True) -> SimpleNamespace:
    return SimpleNamespace(id="acc-1", is_active=active, role=SimpleNamespace(value=role))


@pytest.fixture
def gated_client(monkeypatch, tmp_path):
    """App with the P3 gate enabled (password set, loopback bind)."""
    monkeypatch.setenv(A.PASSWORD_ENV, "letmein")
    monkeypatch.delenv(A.BIND_ENV, raising=False)
    app = create_app(RunManager(sessions_root=str(tmp_path / "s")))
    return TestClient(app)


# ── account runtime guards ───────────────────────────────────────────────────

def test_jwt_secret_required_on_public_bind(monkeypatch):
    monkeypatch.delenv(AR.JWT_SECRET_ENV, raising=False)
    monkeypatch.setenv(A.BIND_ENV, "0.0.0.0")
    with pytest.raises(RuntimeError, match="DSWARM_JWT_SECRET"):
        AR.require_jwt_secret_for_public_bind()
    monkeypatch.setenv(AR.JWT_SECRET_ENV, "x" * 32)
    AR.require_jwt_secret_for_public_bind()  # explicit secret → fine


def test_jwt_secret_optional_on_loopback(monkeypatch):
    monkeypatch.delenv(AR.JWT_SECRET_ENV, raising=False)
    monkeypatch.setenv(A.BIND_ENV, "127.0.0.1")
    AR.require_jwt_secret_for_public_bind()  # dev loopback → fine


# ── principal policy ─────────────────────────────────────────────────────────

def test_member_token_authenticates_but_is_forbidden_on_admin_surface(monkeypatch, gated_client):
    monkeypatch.setattr(P, "load_account", lambda account_id: _fake_account("member"))
    headers = {"Authorization": f"Bearer {_account_token(role='member')}"}

    assert gated_client.get("/api/folders", headers=headers).status_code == 200

    response = gated_client.get("/api/settings/credential-accounts", headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"] == "Admin required"


def test_admin_account_token_allowed_on_admin_surface(monkeypatch, gated_client):
    monkeypatch.setattr(P, "load_account", lambda account_id: _fake_account("admin"))
    headers = {"Authorization": f"Bearer {_account_token(role='admin')}"}
    assert gated_client.get("/api/settings/credential-accounts", headers=headers).status_code == 200


def test_deactivated_account_token_is_rejected(monkeypatch, gated_client):
    monkeypatch.setattr(P, "load_account", lambda account_id: _fake_account("member", active=False))
    headers = {"Authorization": f"Bearer {_account_token()}"}
    assert gated_client.get("/api/folders", headers=headers).status_code == 401


def test_p3_operator_token_is_admin(gated_client):
    token = gated_client.post("/api/auth/login", json={"password": "letmein"}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert gated_client.get("/api/settings/credential-accounts", headers=headers).status_code == 200


# ── login throttle ───────────────────────────────────────────────────────────

def test_login_throttle_returns_429_after_repeated_failures(monkeypatch, tmp_path):
    monkeypatch.setenv(A.PASSWORD_ENV, "letmein")
    monkeypatch.delenv(A.BIND_ENV, raising=False)
    monkeypatch.setenv("DSWARM_LOGIN_MAX_FAILS", "3")
    monkeypatch.setenv("DSWARM_LOGIN_WINDOW_S", "60")
    client = TestClient(create_app(RunManager(sessions_root=str(tmp_path / "s"))))

    for _ in range(3):
        assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 429
    # the correct password also waits out the window (lockout, not per-guess)
    assert client.post("/api/auth/login", json={"password": "letmein"}).status_code == 429


def test_login_throttle_resets_after_success(monkeypatch, tmp_path):
    monkeypatch.setenv(A.PASSWORD_ENV, "letmein")
    monkeypatch.delenv(A.BIND_ENV, raising=False)
    monkeypatch.setenv("DSWARM_LOGIN_MAX_FAILS", "3")
    monkeypatch.setenv("DSWARM_LOGIN_WINDOW_S", "60")
    client = TestClient(create_app(RunManager(sessions_root=str(tmp_path / "s"))))

    assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "letmein"}).status_code == 200
    # a success clears the bucket: the next failures start from zero again
    assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401


# ── audit writes ─────────────────────────────────────────────────────────────

def test_login_writes_audit_rows(monkeypatch, tmp_path):
    monkeypatch.setenv(A.PASSWORD_ENV, "letmein")
    monkeypatch.delenv(A.BIND_ENV, raising=False)
    client = TestClient(create_app(RunManager(sessions_root=str(tmp_path / "s"))))

    assert client.post("/api/auth/login", json={"password": "letmein"}).status_code == 200
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401

    from apps.web.db.models import AuditLogModel
    from apps.web.db.session import SessionLocal

    db = SessionLocal()
    try:
        success = (
            db.query(AuditLogModel)
            .filter_by(action="login", actor="operator")
            .all()
        )
        assert any(row.status == "success" and row.ip_address == "testclient" for row in success)
        failures = (
            db.query(AuditLogModel)
            .filter_by(action="login", actor="unknown", status="failure")
            .filter(AuditLogModel.ip_address == "testclient")
            .all()
        )
        assert failures
    finally:
        db.close()


# ── account secret policy (extended) ─────────────────────────────────────────

def test_account_secrets_required_on_public_bind(monkeypatch):
    monkeypatch.delenv(AR.JWT_SECRET_ENV, raising=False)
    monkeypatch.delenv(AR.ENCRYPTION_KEY_ENV, raising=False)
    monkeypatch.setenv(A.BIND_ENV, "0.0.0.0")
    with pytest.raises(RuntimeError, match="DSWARM_JWT_SECRET"):
        AR.require_account_secrets_for_public_bind()
    monkeypatch.setenv(AR.JWT_SECRET_ENV, "x" * 32)
    with pytest.raises(RuntimeError, match="DSWARM_ENCRYPTION_KEY"):
        AR.require_account_secrets_for_public_bind()
    monkeypatch.setenv(AR.ENCRYPTION_KEY_ENV, "fernet-placeholder")
    AR.require_account_secrets_for_public_bind()  # both present → fine


# ── api keys ─────────────────────────────────────────────────────────────────

def test_api_key_create_verify_revoke_roundtrip(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from apps.web.accounts.db_service import DbAccountService
    from apps.web.accounts.models import AccountRole
    from apps.web.db.models import Base

    engine = create_engine(
        f"sqlite:///{tmp_path / 'accounts.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        service = DbAccountService(db)
        account = service.create_account(
            username="key-owner", display_name="Key Owner", password="secret123",
            role=AccountRole.MEMBER,
        )
        token, key = service.create_api_key(account.id)
        assert token
        verified = service.verify_api_token(token)
        assert verified is not None and verified["account_id"] == account.id
        listed = service.list_api_keys(account.id)
        assert listed and listed[0]["is_active"] is True
        assert service.revoke_api_key(key.id) is True
        assert service.verify_api_token(token) is None
        assert service.list_api_keys(account.id)[0]["is_active"] is False
    finally:
        db.close()


def test_api_key_authenticates_http_with_account_role(monkeypatch, gated_client):
    monkeypatch.setattr(P, "load_account", lambda account_id: _fake_account("member"))
    monkeypatch.setattr(
        "apps.web.accounts.db_service.DbAccountService.verify_api_token",
        lambda self, token: {"account_id": "acc-1"} if token == "key-sentinel" else None,
    )

    headers = {"Authorization": "Bearer key-sentinel"}
    # the API key passes the gate and acts as the member account
    assert gated_client.get("/api/folders", headers=headers).status_code == 200
    # …and is held to the same role policy as the account
    assert gated_client.get("/api/settings/credential-accounts", headers=headers).status_code == 403

    # a revoked key resolves to nobody → the gate 401s it
    monkeypatch.setattr(
        "apps.web.accounts.db_service.DbAccountService.verify_api_token",
        lambda self, token: None,
    )
    assert gated_client.get("/api/folders", headers=headers).status_code == 401
