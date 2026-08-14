"""Review/ordinary worker capacity views backed by WorkerLaneGate."""

from __future__ import annotations


class ReviewCapacityMixin:
    def _active_review_count(self) -> int:
        return self._worker_lane_gate.snapshot()["review_active"]

    def _ordinary_task_count(self, tasks: dict) -> int:
        del tasks  # task registries track lifecycle; the lane gate owns capacity.
        return self._worker_lane_gate.snapshot()["ordinary_active"]

    def _ordinary_capacity_available(self, tasks: dict) -> bool:
        del tasks
        if int(self.max_workers) <= 0:
            return False
        return (
            self._worker_lane_gate.snapshot()["ordinary_active"]
            < self._worker_lane_gate.ordinary_limit
        )

    def _review_capacity_available(self) -> bool:
        return (
            self._worker_lane_gate.review_limit > 0
            and self._active_review_count() < self._worker_lane_gate.review_limit
        )

    @staticmethod
    def _intent_uses_review_lane(intent: dict) -> bool:
        return str(intent.get("worker_class") or "code").strip().lower() in {
            "review", "verifier"
        }

    def _dispatchable_open_intents(self, open_intents: list[dict]) -> list[dict]:
        if self._review_capacity_available():
            return open_intents
        return [
            it for it in open_intents
            if not self._intent_uses_review_lane(it)
        ]

    def _capacity_dispatchable_open_intents(
        self, open_intents: list[dict], tasks: dict
    ) -> list[dict]:
        ordinary_free = self._ordinary_capacity_available(tasks)
        review_free = self._review_capacity_available()
        out: list[dict] = []
        for it in open_intents:
            if self._intent_uses_review_lane(it):
                if review_free:
                    out.append(it)
            elif ordinary_free:
                out.append(it)
        return out
