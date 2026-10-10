"""Small atomic JSON repository for operator-owned Target cards.

Target metadata is deliberately kept outside the event-sourced run log. Run
output/evidence remains authoritative; this store only owns durable operator
context and relationships.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .models import Target


_TEXT_LIMIT = 20_000
_ACTIVITY_LIMIT = 200


def _text(value: Any, *, limit: int = _TEXT_LIMIT) -> str:
    return str(value or "").strip()[:limit]


def _list_text(value: Any, *, limit: int = 256) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item, limit=limit) for item in value if _text(item, limit=limit)]


class TargetStore:
    def __init__(self, root: str | Path = "sessions") -> None:
        self.path = Path(root) / "_targets.json"
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if isinstance(raw, dict) and isinstance(raw.get("targets"), dict):
            loaded = {str(k): v for k, v in raw["targets"].items() if isinstance(v, dict)}
        elif isinstance(raw, dict):
            # Accept the compact early format used by local experiments.
            loaded = {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        else:
            loaded = {}
        # Older/local card files were allowed to contain a free-form primary
        # direction. Normalize that relationship at the read boundary so a
        # malformed card can never make Target -> Direction -> Run appear
        # attached to a direction that does not exist. Do not rewrite the file
        # during startup; the next ordinary mutation persists the correction.
        self._data = {}
        for key, value in loaded.items():
            item = dict(value)
            directions = [row for row in (item.get("directions") or [])
                          if isinstance(row, dict) and str(row.get("id") or "").strip()]
            item["directions"] = directions
            direction_ids = {str(row["id"]) for row in directions}
            primary = str(item.get("primary_direction_id") or "").strip()
            item["primary_direction_id"] = primary if primary in direction_ids else None
            self._data[key] = item

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps({"version": 1, "targets": self._data}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        last_error: OSError | None = None
        for attempt in range(5):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError as exc:
                last_error = exc
                if attempt == 4:
                    break
                time.sleep(0.02 * (attempt + 1))
        if last_error is not None:
            raise last_error

    @staticmethod
    def _now() -> float:
        return time.time()

    def _target(self, raw: dict[str, Any]) -> Target:
        return Target(
            id=str(raw.get("id") or ""),
            name=_text(raw.get("name"), limit=240) or "Untitled target",
            kind=str(raw.get("kind") or "ctf"),
            category=str(raw.get("category") or "misc"),
            description=_text(raw.get("description")),
            target_address=_text(raw.get("target_address"), limit=2000) or None,
            attachments=_list_text(raw.get("attachments"), limit=500),
            expected_flags=max(1, int(raw.get("expected_flags") or 1)),
            multi_flag=bool(raw.get("multi_flag", False)),
            flag_format_hint=_text(raw.get("flag_format_hint"), limit=2000),
            primary_direction_id=(
                _text(raw.get("primary_direction_id"), limit=200)
                if _text(raw.get("primary_direction_id"), limit=200) in {
                    str(direction.get("id")) for direction in (raw.get("directions") or [])
                    if isinstance(direction, dict) and direction.get("id")
                } else None
            ),
            directions=[
                direction for direction in (raw.get("directions") or [])
                if isinstance(direction, dict) and str(direction.get("id") or "").strip()
            ],
            hints=list(raw.get("hints") or []),
            run_ids=_list_text(raw.get("run_ids"), limit=240),
            verified_facts=list(raw.get("verified_facts") or []),
            audit_records=[item for item in (raw.get("audit_records") or []) if isinstance(item, dict)][:_ACTIVITY_LIMIT],
            activity=[item for item in (raw.get("activity") or []) if isinstance(item, dict)][:_ACTIVITY_LIMIT],
            archived_at=raw.get("archived_at"),
            created_at=float(raw.get("created_at") or 0),
            updated_at=float(raw.get("updated_at") or 0),
        )

    def _append_activity(
        self,
        raw: dict[str, Any],
        *,
        kind: str,
        label: str,
        source: str = "target",
        run_id: str | None = None,
        source_seq: int | None = None,
        timestamp: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a bounded, redacted-free activity projection to a Target.

        This is deliberately not an event source: ``source=run`` entries point
        back to an authoritative Run event sequence, while target entries only
        describe operator-owned card mutations.
        """
        ts = float(timestamp if timestamp is not None else self._now())
        activity = [item for item in (raw.get("activity") or []) if isinstance(item, dict)]
        if source_seq is not None and run_id:
            for item in activity:
                if (item.get("source") == source and item.get("run_id") == run_id
                        and item.get("source_seq") == source_seq and item.get("kind") == kind):
                    return item
        item: dict[str, Any] = {
            "id": f"activity-{uuid.uuid4().hex[:12]}",
            "kind": _text(kind, limit=80),
            "label": _text(label, limit=240),
            "source": _text(source, limit=40) or "target",
            "timestamp": ts,
        }
        if run_id:
            item["run_id"] = _text(run_id, limit=240)
        if source_seq is not None:
            item["source_seq"] = int(source_seq)
        if metadata:
            # Only compact, scalar metadata belongs in the card projection.
            item["metadata"] = {
                _text(key, limit=80): value
                for key, value in metadata.items()
                if isinstance(value, (str, int, float, bool)) or value is None
            }
        raw["activity"] = [item, *activity][: _ACTIVITY_LIMIT]
        raw["updated_at"] = max(float(raw.get("updated_at") or 0), ts)
        return item

    def _save_raw(self, raw: dict[str, Any]) -> None:
        self._data[str(raw.get("id") or "")] = raw
        self._flush()

    def record_activity(
        self,
        target_id: str,
        *,
        kind: str,
        label: str,
        source: str = "target",
        run_id: str | None = None,
        source_seq: int | None = None,
        timestamp: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        raw = self._data.get(target_id)
        if raw is None:
            return None
        raw = dict(raw)
        item = self._append_activity(
            raw, kind=kind, label=label, source=source, run_id=run_id,
            source_seq=source_seq, timestamp=timestamp, metadata=metadata,
        )
        self._save_raw(raw)
        return item

    def record_run_event(self, target_id: str, run_id: str, event_type: Any, *,
                         source_seq: int | None = None, timestamp: float | None = None,
                         status: str = "", solved: bool = False,
                         reason: str = "") -> dict[str, Any] | None:
        """Index lifecycle events without copying raw event payloads or secrets."""
        event = getattr(event_type, "value", event_type)
        event = str(event or "")
        labels = {
            "run.queued": ("run.queued", "Run queued"),
            "run.dispatched": ("run.dispatched", "Run dispatched"),
            "run.started": ("run.started", "Run started"),
            "run.reopened": ("run.reopened", "Run reopened"),
            "run.cancelled": ("run.cancelled", "Run cancelled"),
        }
        if event == "run.finished":
            if reason == "cancelled":
                kind, label = "run.cancelled", "Run cancelled"
            elif solved:
                kind, label = "run.solved", "Run solved"
            elif reason == "runtime_failure":
                kind, label = "run.failed", "Run failed"
            else:
                kind, label = "run.finished", "Run finished"
        elif event == "hitl.response":
            return None
        elif event not in labels:
            return None
        else:
            kind, label = labels[event]
        return self.record_activity(
            target_id, kind=kind, label=label, source="run", run_id=run_id,
            source_seq=source_seq, timestamp=timestamp,
            metadata={"status": status, "solved": solved} if status or solved else None,
        )

    def list(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        values = [self._target(raw) for raw in self._data.values()]
        if not include_archived:
            values = [target for target in values if target.archived_at is None]
        return [target.to_dict() for target in sorted(values, key=lambda item: item.updated_at, reverse=True)]

    def get(self, target_id: str) -> dict[str, Any] | None:
        raw = self._data.get(target_id)
        return self._target(raw).to_dict() if raw else None

    def get_direction(self, target_id: str, direction_id: str) -> dict[str, Any] | None:
        """Return a direction only when it belongs to the requested Target."""
        target = self._data.get(target_id)
        wanted = _text(direction_id, limit=200)
        if target is None or not wanted:
            return None
        return next(
            (dict(direction) for direction in (target.get("directions") or [])
             if isinstance(direction, dict) and str(direction.get("id") or "") == wanted),
            None,
        )

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = self._now()
        target_id = f"target-{uuid.uuid4().hex[:12]}"
        initial_direction: dict[str, Any] | None = None
        raw_direction = payload.get("primary_direction")
        if isinstance(raw_direction, dict):
            label = _text(raw_direction.get("label"), limit=240)
            if label:
                initial_direction = {
                    "id": f"direction-{uuid.uuid4().hex[:10]}",
                    "label": label,
                    "description": _text(raw_direction.get("description")),
                    "category": _text(raw_direction.get("category"), limit=80),
                    "status": "active",
                    "created_at": now,
                }
        target = Target(
            id=target_id,
            name=_text(payload.get("name"), limit=240) or "Untitled target",
            kind=str(payload.get("kind") or "ctf"),
            category=str(payload.get("category") or "misc"),
            description=_text(payload.get("description")),
            target_address=_text(payload.get("target_address"), limit=2000) or None,
            attachments=_list_text(payload.get("attachments"), limit=500),
            expected_flags=max(1, int(payload.get("expected_flags") or 1)),
            multi_flag=bool(payload.get("multi_flag", False)),
            flag_format_hint=_text(payload.get("flag_format_hint"), limit=2000),
            primary_direction_id=initial_direction["id"] if initial_direction else None,
            directions=[initial_direction] if initial_direction else [],
            created_at=now,
            updated_at=now,
        )
        raw = target.to_dict()
        self._append_activity(raw, kind="target.created", label="Target created", timestamp=now)
        if initial_direction:
            self._append_activity(
                raw,
                kind="direction.added",
                label="Direction added",
                timestamp=now,
                metadata={"direction_id": initial_direction["id"]},
            )
        self._data[target_id] = raw
        self._flush()
        return self._target(raw).to_dict()

    def update(self, target_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        if target_id not in self._data:
            return None
        raw = dict(self._data[target_id])
        changed_keys: list[str] = []
        for key in ("name", "kind", "category", "description", "target_address", "flag_format_hint"):
            if key in changes:
                next_value = _text(changes[key], limit=240 if key == "name" else (_TEXT_LIMIT if key == "description" else 2000)) or None
                if raw.get(key) != next_value:
                    changed_keys.append(key)
                raw[key] = next_value
        if "attachments" in changes:
            raw["attachments"] = _list_text(changes["attachments"], limit=500)
        if "expected_flags" in changes:
            try:
                raw["expected_flags"] = max(1, int(changes["expected_flags"]))
            except (TypeError, ValueError):
                pass
        if "multi_flag" in changes:
            raw["multi_flag"] = bool(changes["multi_flag"])
        if "primary_direction_id" in changes:
            requested = _text(changes["primary_direction_id"], limit=200) or None
            direction_ids = {
                str(direction.get("id"))
                for direction in (raw.get("directions") or [])
                if isinstance(direction, dict) and direction.get("id")
            }
            # A primary direction is a relationship inside the Target card,
            # never an arbitrary label supplied by the client. Rejecting an
            # unknown id keeps the Target -> Direction -> Run tree coherent.
            raw["primary_direction_id"] = requested if requested in direction_ids else None
        now = self._now()
        if "name" in changed_keys:
            self._append_activity(raw, kind="target.renamed", label="Target renamed", timestamp=now)
        elif changed_keys or "attachments" in changes or "expected_flags" in changes or "multi_flag" in changes or "primary_direction_id" in changes:
            self._append_activity(raw, kind="target.updated", label="Target updated", timestamp=now)
        raw["updated_at"] = max(float(raw.get("updated_at") or 0), now)
        self._data[target_id] = raw
        self._flush()
        return self._target(raw).to_dict()

    def archive(self, target_id: str, archived: bool = True) -> dict[str, Any] | None:
        return self._archive(target_id, archived)

    def _archive(self, target_id: str, archived: bool) -> dict[str, Any] | None:
        raw = self._data.get(target_id)
        if raw is None:
            return None
        raw = dict(raw)
        now = self._now()
        raw["archived_at"] = now if archived else None
        self._append_activity(raw, kind="target.archived" if archived else "target.restored",
                              label="Target archived" if archived else "Target restored", timestamp=now)
        raw["updated_at"] = now
        self._data[target_id] = raw
        self._flush()
        return self._target(raw).to_dict()

    def attach_run(self, target_id: str, run_id: str) -> dict[str, Any] | None:
        raw = self._data.get(target_id)
        if raw is None:
            return None
        raw = dict(raw)
        ids = list(raw.get("run_ids") or [])
        if run_id not in ids:
            ids.append(run_id)
        is_new = run_id not in (raw.get("run_ids") or [])
        raw["run_ids"] = ids
        now = self._now()
        if is_new:
            self._append_activity(raw, kind="run.created", label="Run created", source="target", run_id=run_id, timestamp=now)
        raw["updated_at"] = now
        self._data[target_id] = raw
        self._flush()
        return self._target(raw).to_dict()
    def detach_run(self, target_id: str, run_id: str) -> dict[str, Any] | None:
        """Remove a deleted Run from its durable Target relationship."""
        raw = self._data.get(target_id)
        if raw is None:
            return None
        wanted = _text(run_id, limit=240)
        ids = [item for item in (raw.get("run_ids") or []) if str(item) != wanted]
        if len(ids) == len(raw.get("run_ids") or []):
            return self._target(raw).to_dict()
        raw = dict(raw)
        raw["run_ids"] = ids
        now = self._now()
        self._append_activity(
            raw,
            kind="run.deleted",
            label="Run deleted",
            source="target",
            run_id=wanted,
            timestamp=now,
        )
        raw["updated_at"] = now
        self._data[target_id] = raw
        self._flush()
        return self._target(raw).to_dict()

    def detach_run_everywhere(self, run_id: str) -> bool:
        """Remove an orphaned Run id from every Target card."""
        changed = False
        for target_id in list(self._data):
            raw = self._data.get(target_id) or {}
            if run_id in (raw.get("run_ids") or []):
                self.detach_run(target_id, run_id)
                changed = True
        return changed


    def add_hint(self, target_id: str, text: str, *, source: str = "operator", pinned: bool = False) -> dict[str, Any] | None:
        raw = self._data.get(target_id)
        if raw is None or not _text(text):
            return None
        hint = {"id": f"hint-{uuid.uuid4().hex[:10]}", "text": _text(text), "source": _text(source, limit=40) or "operator", "pinned": bool(pinned), "created_at": self._now()}
        raw = dict(raw)
        raw["hints"] = list(raw.get("hints") or []) + [hint]
        now = self._now()
        self._append_activity(raw, kind="hint.added", label="Hint added", timestamp=now, metadata={"hint_id": hint["id"]})
        raw["updated_at"] = now
        self._data[target_id] = raw
        self._flush()
        return hint
    def add_audit_record(self, target_id: str, record: dict[str, Any]) -> dict[str, Any] | None:
        """Persist one operator-approved, provenance-bearing Target history row."""
        raw = self._data.get(target_id)
        if raw is None or not isinstance(record, dict):
            return None
        clean = dict(record)
        clean["id"] = str(clean.get("id") or f"audit-{uuid.uuid4().hex[:12]}")[:120]
        clean["item_type"] = str(clean.get("item_type") or "fact")[:80]
        clean["decision"] = "audited"
        clean["audited_at"] = float(clean.get("audited_at") or self._now())
        clean["evidence_refs"] = [str(ref)[:200] for ref in (clean.get("evidence_refs") or []) if str(ref).strip()][:16]
        raw = dict(raw)
        records = [item for item in (raw.get("audit_records") or []) if isinstance(item, dict)]
        fingerprint = (str(clean.get("source_run_id") or ""), str(clean.get("item_type") or ""), int(clean.get("fact_seq") or 0), int(clean.get("dead_end_seq") or 0), str(clean.get("route_hash") or ""))
        if not any((str(item.get("source_run_id") or ""), str(item.get("item_type") or "fact"), int(item.get("fact_seq") or 0), int(item.get("dead_end_seq") or 0), str(item.get("route_hash") or "")) == fingerprint for item in records):
            records.append(clean)
        raw["audit_records"] = records[-_ACTIVITY_LIMIT:]
        self._append_activity(raw, kind="target.audit", label="Target review audited", source="target", metadata={
            "audit_id": clean["id"], "source_run_id": clean.get("source_run_id", ""),
            "item_type": clean["item_type"], "fact_seq": int(clean.get("fact_seq") or 0), "decision": "audited",
        })
        self._save_raw(raw)
        return clean

    def add_direction(self, target_id: str, label: str, *, description: str = "", category: str = "") -> dict[str, Any] | None:
        raw = self._data.get(target_id)
        if raw is None or not _text(label, limit=240):
            return None
        direction = {"id": f"direction-{uuid.uuid4().hex[:10]}", "label": _text(label, limit=240), "description": _text(description), "category": _text(category, limit=80), "status": "active", "created_at": self._now()}
        raw = dict(raw)
        raw["directions"] = list(raw.get("directions") or []) + [direction]
        if not raw.get("primary_direction_id"):
            raw["primary_direction_id"] = direction["id"]
        now = self._now()
        self._append_activity(raw, kind="direction.added", label="Direction added", timestamp=now, metadata={"direction_id": direction["id"]})
        raw["updated_at"] = now
        self._data[target_id] = raw
        self._flush()
        return direction
