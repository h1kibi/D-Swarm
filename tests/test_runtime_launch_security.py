from __future__ import annotations

import pytest

from dswarm.core.runtime_env import (
    WebLaunchConfigError,
    validate_web_launch,
)
from tests.test_run_sh import _run_web


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20"])
def test_non_loopback_publish_requires_password(host, tmp_path):
    result, calls = _run_web(
        tmp_path,
        "--host",
        host,
        env={"DSWARM_WEB_PASSWORD": ""},
    )
    assert result.returncode != 0
    assert "web_password_required_for_non_loopback" in result.stderr
    assert calls == []


def test_non_loopback_publish_with_password_reaches_compose(tmp_path):
    result, calls = _run_web(
        tmp_path,
        "--host",
        "192.168.1.20",
        env={"DSWARM_WEB_PASSWORD": "not-printed"},
    )
    assert result.returncode == 0
    assert any("CALL\tcompose\tup" in line for line in calls)
    assert "not-printed" not in result.stdout + result.stderr


def test_loopback_publish_without_password_is_allowed():
    validate_web_launch(public_host="127.0.0.1", password="")
    validate_web_launch(public_host="localhost", password="")


def test_internal_wildcard_is_safe_only_with_trusted_loopback_publication():
    validate_web_launch(
        public_host="127.0.0.1",
        password="",
        internal_bind="0.0.0.0",
        trusted_control_plane=True,
    )
    with pytest.raises(WebLaunchConfigError, match="web_password_required_for_non_loopback"):
        validate_web_launch(
            public_host="0.0.0.0",
            password="",
            internal_bind="0.0.0.0",
            trusted_control_plane=True,
        )


def test_password_is_not_in_validation_error():
    with pytest.raises(WebLaunchConfigError) as exc:
        validate_web_launch(public_host="0.0.0.0", password="")
    assert "super-secret" not in str(exc.value)
