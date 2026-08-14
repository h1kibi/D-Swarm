"""Provider/runtime error classification and aggregation for operator diagnostics.

This module is intentionally pure: it does not retry workers itself and does not
know solver internals. It turns raw provider/CLI failure text into stable,
operator-facing diagnostics that controllers and solver runtime can emit on the existing event bus.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from typing import Any, Deque


@dataclass(frozen=True)
class ProviderErrorDiagnostic:
    category: str
    severity: str
    retryable: bool
    should_pause_dispatch: bool
    provider: str = ""
    account_id: str = ""
    worker_id: str = ""
    raw_message: str = ""
    user_message: str = ""
    suggested_action: str = ""

    def to_event(self) -> dict[str, Any]:
        return asdict(self)


_FATAL_CATEGORIES = {"insufficient_quota", "auth_invalid", "model_not_found"}
_RETRYABLE_CATEGORIES = {"transient_network", "timeout", "rate_limited", "provider_down"}


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(n in text for n in needles)


def classify_provider_error(
    message: str,
    *,
    provider: str = "",
    account_id: str = "",
    worker_id: str = "",
) -> ProviderErrorDiagnostic:
    """Classify raw LLM/worker failure text into a stable diagnostic.

    Categories deliberately use broad string matching because upstream CLI/LLM
    providers return heterogeneous error text. The classifier is conservative:
    quota/auth/model errors pause dispatch; transient failures remain retryable.
    """
    raw = str(message or "").strip()
    low = raw.lower()

    if _contains_any(low, (
        "insufficient balance", "insufficient quota", "quota exceeded",
        "billing", "recharge", "402", "余额不足", "账户余额", "额度不足",
    )):
        category = "insufficient_quota"
        user_message = "LLM 提供商返回余额/额度不足，继续派发会批量失败。"
        suggested = "请检查账号余额、套餐额度或切换可用账号/模型后再恢复。"
    elif _contains_any(low, (
        "invalid api key", "unauthorized", "401", "403", "forbidden",
        "authentication", "permission denied", "invalid token", "无效", "未授权",
    )):
        category = "auth_invalid"
        user_message = "LLM 凭据认证失败，当前账号或 API Key 不可用。"
        suggested = "请检查凭据绑定、环境变量和账号权限。"
    elif _contains_any(low, (
        "model not found", "unknown model", "invalid model", "unknown provider", "模型不存在",
    )):
        category = "model_not_found"
        user_message = "配置的模型不存在或当前账号无权访问。"
        suggested = "请在 Worker 配置中选择该账号实际可用的模型。"
    elif _contains_any(low, (
        "rate limit", "429", "too many requests", "限速", "频率限制",
    )):
        category = "rate_limited"
        user_message = "LLM 提供商限速，Worker 将退避后重试或由后续 Worker 接续。"
        suggested = "可降低并发、切换账号，或等待限速窗口恢复。"
    elif _contains_any(low, (
        "connection reset", "connection refused", "network", "dns",
        "econnreset", "enotfound", "temporary failure", "网络",
    )):
        category = "transient_network"
        user_message = "LLM 网络连接异常，系统会尝试自动重试/接续。"
        suggested = "若频繁发生，请检查网络、代理、Docker 到宿主机的连通性。"
    elif _contains_any(low, (
        "timeout", "timed out", "connecttimeout", "readtimeout", "超时",
    )):
        category = "timeout"
        user_message = "LLM 请求超时，通常可自动重试并接续当前任务。"
        suggested = "如果持续出现，请检查网络、代理或 provider endpoint。"
    elif _contains_any(low, (
        "500", "502", "503", "504", "service unavailable", "bad gateway",
        "upstream", "provider unavailable",
    )):
        category = "provider_down"
        user_message = "LLM 提供商服务暂时不可用，系统会尝试退避重试。"
        suggested = "如果多个 Worker 同时失败，建议暂停并稍后恢复或切换 provider。"
    else:
        category = "unknown_worker_failure"
        user_message = "Worker 返回未归类异常，已记录供诊断。"
        suggested = "查看 worker 日志；若同类错误批量出现，应暂停排查配置。"

    retryable = category in _RETRYABLE_CATEGORIES
    severity = "fatal" if category in _FATAL_CATEGORIES else "warning"
    should_pause = category in _FATAL_CATEGORIES
    return ProviderErrorDiagnostic(
        category=category,
        severity=severity,
        retryable=retryable,
        should_pause_dispatch=should_pause,
        provider=provider,
        account_id=account_id,
        worker_id=worker_id,
        raw_message=raw[:1000],
        user_message=user_message,
        suggested_action=suggested,
    )


class ProviderErrorAggregator:
    """Sliding-window provider error aggregator for user-facing batch alerts."""

    def __init__(self, *, window_s: float = 60.0, fatal_threshold: int = 3,
                 majority_ratio: float = 0.5) -> None:
        self.window_s = float(window_s)
        self.fatal_threshold = int(fatal_threshold)
        self.majority_ratio = float(majority_ratio)
        self._events: dict[tuple[str, str, str], Deque[tuple[float, ProviderErrorDiagnostic]]] = defaultdict(deque)
        self._alerted: set[tuple[str, str, str, int, int]] = set()

    def record(self, diag: ProviderErrorDiagnostic, *, now: float,
               active_workers: int) -> dict[str, Any] | None:
        key = (diag.provider or "unknown", diag.account_id or "", diag.category)
        q = self._events[key]
        q.append((float(now), diag))
        cutoff = float(now) - self.window_s
        while q and q[0][0] < cutoff:
            q.popleft()

        workers = {d.worker_id for _, d in q if d.worker_id}
        count = len(q)
        affected = len(workers) or count
        active = max(int(active_workers or 0), 0)
        fatal_batch = diag.severity == "fatal" and count >= self.fatal_threshold
        majority = active > 0 and affected / active >= self.majority_ratio and affected >= 2
        if not (fatal_batch or majority):
            return None

        # Avoid emitting the exact same alert repeatedly while the window count has
        # not materially changed. Count+affected in the key still lets an escalating
        # outage update the UI.
        marker = (*key, count, affected)
        if marker in self._alerted:
            return None
        self._alerted.add(marker)

        return {
            "type": "provider.batch_alert",
            "provider": diag.provider,
            "account_id": diag.account_id,
            "category": diag.category,
            "severity": "fatal" if diag.severity == "fatal" else "warning",
            "count": count,
            "affected_workers": affected,
            "active_workers": active,
            "retryable": diag.retryable,
            "should_pause_dispatch": diag.should_pause_dispatch or fatal_batch,
            "user_message": (
                f"{diag.user_message} 近 {int(self.window_s)} 秒内 {count} 次，"
                f"影响 {affected}/{active or '?'} 个 Worker。"
            ),
            "suggested_action": diag.suggested_action,
        }
