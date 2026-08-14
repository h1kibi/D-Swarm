"""Provider/runtime error classification and aggregation."""

from __future__ import annotations

from apps.web.provider_errors import ProviderErrorAggregator, classify_provider_error


def test_classifies_transient_network_error_as_retryable():
    diag = classify_provider_error(
        "ConnectTimeout: connection reset by peer",
        provider="deepseek",
        account_id="main",
        worker_id="pi-web",
    )
    assert diag.category == "transient_network"
    assert diag.severity == "warning"
    assert diag.retryable is True
    assert diag.should_pause_dispatch is False
    assert "自动重试" in diag.user_message


def test_classifies_insufficient_balance_as_fatal_quota():
    diag = classify_provider_error(
        "402 insufficient balance: please recharge your account",
        provider="deepseek",
        account_id="main",
        worker_id="pi-pwn",
    )
    assert diag.category == "insufficient_quota"
    assert diag.severity == "fatal"
    assert diag.retryable is False
    assert diag.should_pause_dispatch is True
    assert "余额" in diag.user_message


def test_classifies_invalid_auth_as_fatal_and_pauses_dispatch():
    diag = classify_provider_error(
        "401 Unauthorized: invalid api key",
        provider="openai-compatible",
        account_id="team",
        worker_id="pi-rev",
    )
    assert diag.category == "auth_invalid"
    assert diag.severity == "fatal"
    assert diag.retryable is False
    assert diag.should_pause_dispatch is True


def test_aggregator_alerts_after_three_fatal_errors_in_window():
    agg = ProviderErrorAggregator(window_s=60, fatal_threshold=3, majority_ratio=0.5)
    alerts = []
    for i in range(3):
        diag = classify_provider_error(
            "insufficient balance",
            provider="deepseek",
            account_id="main",
            worker_id=f"pi-{i}",
        )
        alert = agg.record(diag, now=100 + i, active_workers=8)
        if alert:
            alerts.append(alert)
    assert alerts
    assert alerts[-1]["type"] == "provider.batch_alert"
    assert alerts[-1]["category"] == "insufficient_quota"
    assert alerts[-1]["count"] == 3
    assert alerts[-1]["affected_workers"] == 3
    assert alerts[-1]["should_pause_dispatch"] is True


def test_aggregator_alerts_when_majority_workers_hit_same_error():
    agg = ProviderErrorAggregator(window_s=60, fatal_threshold=99, majority_ratio=0.5)
    alert = None
    for i in range(3):
        diag = classify_provider_error(
            "rate limit exceeded",
            provider="openai-compatible",
            account_id="team",
            worker_id=f"pi-{i}",
        )
        alert = agg.record(diag, now=200 + i, active_workers=5)
    assert alert is not None
    assert alert["affected_workers"] == 3
    assert alert["active_workers"] == 5
    assert alert["category"] == "rate_limited"


def test_aggregator_does_not_alert_for_single_transient_error():
    agg = ProviderErrorAggregator(window_s=60, fatal_threshold=3, majority_ratio=0.5)
    diag = classify_provider_error(
        "temporary network timeout",
        provider="deepseek",
        account_id="main",
        worker_id="pi-0",
    )
    assert agg.record(diag, now=1, active_workers=6) is None
