"""Read-only event stream operations for the SQLite shared graph.

The event log is the append-only source of truth.  This small adapter owns only
querying and polling that log; writes and lifecycle projections remain on the
SQLite graph so this module cannot accidentally become a second fact source.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Optional


class GraphEventReader:
    """Query the event log through a graph-like persistence host.

    The host deliberately exposes only the attributes this reader needs
    (``_conn``, ``_lock``, and ``challenge``).  Keeping the adapter structural
    makes it usable by the normal and read-only graph constructors without a
    dependency back into ``shared_graph.py``.
    """

    def __init__(self, graph: Any) -> None:
        self._graph = graph

    def events(self) -> list[dict]:
        graph = self._graph
        with graph._lock:
            rows = graph._conn.execute(
                "SELECT seq, ts, actor, kind, payload, artifact_id, verified, "
                "confidence FROM events ORDER BY seq"
            ).fetchall()
        out = []
        for seq, ts, actor, kind, payload, aid, verified, conf in rows:
            out.append({"seq": seq, "ts": ts, "actor": actor, "kind": kind,
                        "payload": json.loads(payload), "artifact_id": aid,
                        "verified": bool(verified), "confidence": conf})
        return out

    def recent_events(self, limit: int = 40) -> list[dict]:
        """Return this challenge's last ``limit`` events, oldest-first.

        The SQL-side limit is intentional: a shared sessions database must not
        require materializing the complete event log for a small timeline.
        """
        if limit <= 0:
            return []
        graph = self._graph
        with graph._lock:
            rows = graph._conn.execute(
                "SELECT seq, ts, actor, kind, payload, artifact_id, verified, "
                "confidence FROM events WHERE challenge_id=? "
                "ORDER BY seq DESC LIMIT ?",
                (graph.challenge.id, int(limit)),
            ).fetchall()
        out = []
        for seq, ts, actor, kind, payload, aid, verified, conf in reversed(rows):
            try:
                parsed = json.loads(payload)
            except Exception:
                parsed = {}
            out.append({"seq": seq, "ts": ts, "actor": actor, "kind": kind,
                        "payload": parsed, "artifact_id": aid,
                        "verified": bool(verified), "confidence": conf})
        return out

    def events_since(self, after_seq: int,
                     kinds: Optional[list[str]] = None) -> list[dict]:
        after = int(after_seq or 0)
        params: list[Any] = [after]
        kind_list = [str(k) for k in (kinds or []) if str(k)]
        where = "WHERE seq > ?"
        if kind_list:
            where += " AND kind IN (" + ",".join("?" for _ in kind_list) + ")"
            params.extend(kind_list)
        graph = self._graph
        with graph._lock:
            rows = graph._conn.execute(
                "SELECT seq, ts, actor, kind, payload, artifact_id, verified, "
                f"confidence FROM events {where} ORDER BY seq",
                tuple(params),
            ).fetchall()
        out = []
        for seq, ts, actor, kind, payload, aid, verified, conf in rows:
            try:
                parsed = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                parsed = {}
            out.append({"seq": seq, "ts": ts, "actor": actor, "kind": kind,
                        "payload": parsed, "artifact_id": aid,
                        "verified": bool(verified), "confidence": conf})
        return out

    async def subscribe_events(
        self,
        after_seq: int = 0,
        kinds: Optional[list[str]] = None,
        poll_interval: float = 0.5,
    ) -> AsyncIterator[dict]:
        cursor = int(after_seq or 0)
        while True:
            events = self.events_since(cursor, kinds=kinds)
            for event in events:
                seq = int(event.get("seq") or 0)
                if seq <= cursor:
                    continue
                cursor = seq
                yield event
            await asyncio.sleep(max(0.05, float(poll_interval)))
