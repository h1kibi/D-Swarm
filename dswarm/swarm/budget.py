"""Run-level and profile/account budget enforcement.

The legacy ``BudgetMixin`` remains responsible for worker-count and global USD
circuit breakers.  ``ProfileBudgetGate`` is the M5 token-accounting gate: it
folds canonical usage records, keeps profile and billing-account blockers
independent, and exposes durable action semantics without changing COST_UPDATE.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from dswarm.core.usage_journal import UsageRecord


class WorkerBudgetExhausted(RuntimeError):
    pass


@dataclass(frozen=True)
class BudgetAction:
    action: str
    profile_id: str | None = None
    account_id: str | None = None
    value: int | float | None = None
    reason: str = ""


@dataclass(frozen=True)
class BudgetVerdict:
    level: str  # ok | warn | cap
    allowed: bool
    reason: str | None = None
    alerts: tuple[dict[str, Any], ...] = ()


@dataclass
class _Usage:
    tokens: int = 0
    calls: int = 0


class ProfileBudgetGate:
    """Independent run-total profile/account token blockers.

    Unknown usage never becomes zero-cost spend, but it also cannot be converted
    into a fabricated token count.  A caller may choose to fail closed on its own
    policy; this gate reports the unknown call in its snapshot and only enforces
    measured/estimated token totals.
    """

    def __init__(
        self,
        *,
        profile_caps: dict[str, int | float] | None = None,
        account_caps: dict[str, int | float] | None = None,
        warn_ratio: float = 0.8,
    ) -> None:
        self.profile_caps = {str(k): float(v) for k, v in (profile_caps or {}).items()}
        self.account_caps = {str(k): float(v) for k, v in (account_caps or {}).items()}
        self.warn_ratio = min(1.0, max(0.0, float(warn_ratio)))
        self._profiles: dict[str, _Usage] = {}
        self._accounts: dict[str, _Usage] = {}
        self._blocked_profiles: set[str] = set()
        self._blocked_accounts: set[str] = set()
        self._actions: list[dict[str, Any]] = []
        # Emit a durable alert only when a scope crosses a budget state edge.
        # Repeated usage records above the same threshold remain visible in the
        # snapshot without flooding the event stream.
        self._alerted_levels: dict[tuple[str, str], str] = {}

    @staticmethod
    def _tokens(record: UsageRecord) -> int:
        return max(0, int(record.input_tokens or 0)) + max(0, int(record.output_tokens or 0))

    def _alert(self, scope: str, key: str, *, level: str, used: float, cap: float) -> dict[str, Any]:
        return {
            "scope": scope,
            "key": key,
            "level": level,
            "used_tokens": used,
            "cap_tokens": cap,
            "ratio": (used / cap) if cap else None,
        }

    def apply(self, record: UsageRecord) -> BudgetVerdict:
        tokens = self._tokens(record)
        alerts: list[dict[str, Any]] = []
        level = "ok"
        for scope, key, caps, values, blocked in (
            ("profile", record.profile_id, self.profile_caps, self._profiles, self._blocked_profiles),
            ("account", record.billing_account_id, self.account_caps, self._accounts, self._blocked_accounts),
        ):
            if not key:
                continue
            usage = values.setdefault(key, _Usage())
            usage.tokens += tokens
            usage.calls += 1
            cap = caps.get(key)
            if cap is None:
                continue
            if usage.tokens >= cap:
                blocked.add(key)
                level = "cap"
                new_level = "cap"
            elif usage.tokens >= cap * self.warn_ratio:
                level = "warn" if level == "ok" else level
                new_level = "warn"
            else:
                new_level = "ok"
            previous_level = self._alerted_levels.get((scope, key), "ok")
            if new_level in {"warn", "cap"} and new_level != previous_level:
                alerts.append(self._alert(scope, key, level=new_level,
                                          used=usage.tokens, cap=cap))
            self._alerted_levels[(scope, key)] = new_level
        return BudgetVerdict(level=level, allowed=level != "cap", reason="budget_cap" if level == "cap" else None, alerts=tuple(alerts))

    def authorize(self, *, profile_id: str | None, account_id: str | None) -> BudgetVerdict:
        alerts: list[dict[str, Any]] = []
        blocked: list[str] = []
        for scope, key, caps, values, blocked_keys in (
            ("profile", profile_id, self.profile_caps, self._profiles, self._blocked_profiles),
            ("account", account_id, self.account_caps, self._accounts, self._blocked_accounts),
        ):
            if not key:
                continue
            cap = caps.get(key)
            if cap is None:
                continue
            used = values.get(key, _Usage()).tokens
            if key in blocked_keys or used >= cap:
                blocked_keys.add(key)
                blocked.append(f"{scope}:{key}")
                alerts.append(self._alert(scope, key, level="cap", used=used, cap=cap))
        if blocked:
            return BudgetVerdict(level="cap", allowed=False, reason="budget_cap:" + ",".join(blocked), alerts=tuple(alerts))
        return BudgetVerdict(level="ok", allowed=True)

    def apply_action(self, action: BudgetAction | dict[str, Any]) -> None:
        if isinstance(action, dict):
            action = BudgetAction(
                action=str(action.get("action") or ""),
                profile_id=action.get("profile_id"),
                account_id=action.get("account_id"),
                value=action.get("value"),
                reason=str(action.get("reason") or ""),
            )
        self._actions.append({
            "action": action.action,
            "profile_id": action.profile_id,
            "account_id": action.account_id,
            "value": action.value,
            "reason": action.reason,
        })
        if action.action == "raise_cap" and action.value is not None:
            if action.profile_id:
                self.profile_caps[action.profile_id] = float(action.value)
                self._blocked_profiles.discard(action.profile_id)
                self._alerted_levels.pop(("profile", action.profile_id), None)
            if action.account_id:
                self.account_caps[action.account_id] = float(action.value)
                self._blocked_accounts.discard(action.account_id)
                self._alerted_levels.pop(("account", action.account_id), None)
        elif action.action == "override":
            if action.profile_id:
                self._blocked_profiles.discard(action.profile_id)
                self._alerted_levels.pop(("profile", action.profile_id), None)
            if action.account_id:
                self._blocked_accounts.discard(action.account_id)
                self._alerted_levels.pop(("account", action.account_id), None)

    def rebuild(
        self,
        records: Iterable[UsageRecord],
        actions: Iterable[BudgetAction | dict[str, Any]] = (),
    ) -> None:
        """Rebuild runtime projections from canonical ledger records/actions.

        Configuration caps remain intact; usage, blocker, alert-edge, and action
        projections are folded from the durable inputs in deterministic order.
        """
        self._profiles.clear()
        self._accounts.clear()
        self._blocked_profiles.clear()
        self._blocked_accounts.clear()
        self._actions.clear()
        self._alerted_levels.clear()
        for record in records:
            self.apply(record)
        for action in actions:
            self.apply_action(action)

    def snapshot(self) -> dict[str, Any]:
        def encode(values: dict[str, _Usage], caps: dict[str, float], blocked: set[str]) -> dict[str, Any]:
            return {
                key: {
                    "tokens": usage.tokens,
                    "calls": usage.calls,
                    "cap_tokens": caps.get(key),
                    "blocked": key in blocked,
                }
                for key, usage in values.items()
            }
        return {
            "profile": encode(self._profiles, self.profile_caps, self._blocked_profiles),
            "account": encode(self._accounts, self.account_caps, self._blocked_accounts),
            "actions": list(self._actions),
            "warn_ratio": self.warn_ratio,
        }


class BudgetMixin:
    def _current_cost_usd(self) -> float:
        ledger = getattr(self.cost, "_global", None)
        return float(getattr(ledger, "usd", 0.0) or 0.0)

    def _budget_exhausted(self) -> str | None:
        if self.max_total_workers is not None and self._spawned_total >= self.max_total_workers:
            return "worker_budget_exhausted"
        if self.cost_budget_usd is not None and self._current_cost_usd() >= self.cost_budget_usd:
            return "cost_budget_exhausted"
        return None

    def _reserve_worker_spawn(self) -> None:
        kind = self._budget_exhausted()
        if kind:
            self._budget_exhausted_kind = kind
            raise WorkerBudgetExhausted(kind)
        self._spawned_total += 1
