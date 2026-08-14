"""Run-level worker/cost budget enforcement."""

from __future__ import annotations


class WorkerBudgetExhausted(RuntimeError):
    pass


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
