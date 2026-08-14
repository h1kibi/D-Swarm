"""Review/ordinary worker capacity accounting."""

from __future__ import annotations


class ReviewCapacityMixin:
    def _active_review_count(self) -> int:
        return len(self._active_review_tasks)

    def _ordinary_task_count(self, tasks: dict) -> int:
        self._active_review_count()
        return sum(1 for t in tasks if t not in self._active_review_tasks)

    def _ordinary_capacity_available(self, tasks: dict) -> bool:
        return self._ordinary_task_count(tasks) < self.max_workers

    def _review_capacity_available(self) -> bool:
        return self._active_review_count() < int(self.review_policy.get("max_concurrent") or 1)

    def _dispatchable_open_intents(self, open_intents: list[dict]) -> list[dict]:
        if self._review_capacity_available():
            return open_intents
        return [
            it for it in open_intents
            if str(it.get("worker_class") or "code") != "review"
        ]

    def _capacity_dispatchable_open_intents(
        self, open_intents: list[dict], tasks: dict
    ) -> list[dict]:
        ordinary_free = self._ordinary_capacity_available(tasks)
        review_free = self._review_capacity_available()
        out: list[dict] = []
        for it in open_intents:
            wc = str(it.get("worker_class") or "code")
            if wc == "review":
                if review_free:
                    out.append(it)
            elif ordinary_free:
                out.append(it)
        return out
