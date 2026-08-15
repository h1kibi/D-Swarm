"""PostgreSQL + pgvector blackboard implementation.

This is the durable board used by the Reason-centered swarm. The evidence
event log remains the authoritative append-only source; this board is the
materialized finding/pheromone layer.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, AsyncIterator, Optional

from psycopg import sql

from dswarm.swarm.board import (
    Finding,
    FindingPredicate,
    PheromoneSettings,
    ReplacementOutcome,
    ReplacementResult,
)


class PostgresBoard:
    def __init__(
        self,
        dsn: str,
        *,
        challenge_id: str = "c1",
        pheromone: Optional[PheromoneSettings] = None,
    ) -> None:
        self.dsn = dsn
        self.challenge_id = challenge_id
        self.pheromone = pheromone or PheromoneSettings.defaults()
        self._schema_name = self._schema_id(challenge_id)
        self._ensure_schema()

    def _connect(self):
        import psycopg

        conn = psycopg.connect(self.dsn)
        if self._schema_name:
            conn.execute(
                sql.SQL("SET search_path TO {}").format(
                    sql.Identifier(self._schema_name)
                )
            )
        return conn

    @staticmethod
    def _schema_id(challenge_id: str) -> str:
        slug = re.sub(r"[^a-z0-9_]+", "_", str(challenge_id or "").lower()).strip("_")
        slug = slug or "run_default"
        return f"run_{slug}"[:63]

    def _ensure_schema(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                    sql.Identifier(self._schema_name)
                )
            )
            conn.commit()
            cur.execute(
                sql.SQL("SET search_path TO {}").format(
                    sql.Identifier(self._schema_name)
                )
            )
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS swarm_findings (
                    seq BIGSERIAL PRIMARY KEY,
                    finding_id UUID NOT NULL DEFAULT gen_random_uuid(),
                    challenge_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    finding_type TEXT NOT NULL,
                    target TEXT NOT NULL,
                    data JSONB NOT NULL,
                    pheromone_base DOUBLE PRECISION NOT NULL,
                    half_life_sec INTEGER NOT NULL,
                    embedding vector(384),
                    source_seq BIGINT,
                    projection_key TEXT,
                    superseded_by UUID,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                "ALTER TABLE swarm_findings ADD COLUMN IF NOT EXISTS projection_key TEXT"
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS ux_swarm_findings_projection_key
                ON swarm_findings(challenge_id, projection_key)
                WHERE projection_key IS NOT NULL
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS swarm_agent_cursors (
                    challenge_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    last_seq BIGINT NOT NULL DEFAULT 0,
                    PRIMARY KEY (challenge_id, agent_name)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS swarm_budgets (
                    challenge_id TEXT PRIMARY KEY,
                    max_agent_hours DOUBLE PRECISION NOT NULL DEFAULT 2.0,
                    max_tokens BIGINT NOT NULL DEFAULT 2000000,
                    used_hours DOUBLE PRECISION NOT NULL DEFAULT 0,
                    used_tokens BIGINT NOT NULL DEFAULT 0
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS swarm_agent_budgets (
                    challenge_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    max_tokens BIGINT NOT NULL DEFAULT 500000,
                    warn_at_tokens BIGINT NOT NULL DEFAULT 400000,
                    used_tokens BIGINT NOT NULL DEFAULT 0,
                    warned BOOLEAN NOT NULL DEFAULT FALSE,
                    PRIMARY KEY (challenge_id, agent_name)
                )
                """
            )
            conn.commit()

    def drop_schema(self) -> None:
        if not self._schema_name:
            return
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(self._schema_name)
                )
            )
            conn.commit()

    @staticmethod
    def _finding_from_row(row: tuple[Any, ...]) -> Finding:
        (
            seq, challenge_id, agent_name, kind, target, data, base, half,
            source_seq, projection_key, superseded_by, created_at,
        ) = row
        if isinstance(data, str):
            payload = json.loads(data or "{}")
        else:
            payload = dict(data or {})
        return Finding(
            challenge_id=str(challenge_id),
            kind=str(kind),
            agent_name=str(agent_name),
            target=str(target),
            payload=payload,
            pheromone_base=float(base),
            half_life_sec=int(half),
            source_seq=int(source_seq or 0),
            projection_key=str(projection_key or ""),
            superseded_by=str(superseded_by) if superseded_by else None,
            seq=int(seq),
            created_at=created_at.timestamp(),
        )

    @staticmethod
    def _finding_columns() -> str:
        return (
            "seq, challenge_id, agent_name, finding_type, target, data, "
            "pheromone_base, half_life_sec, source_seq, projection_key, "
            "superseded_by, created_at"
        )

    def write_finding(
        self,
        *,
        challenge_id: str,
        kind: str,
        agent_name: str = "engine",
        target: str = "",
        payload: Optional[dict[str, Any]] = None,
        pheromone_base: float = 1.0,
        half_life_sec: int = 3600,
        embedding: Optional[list[float]] = None,
        source_seq: int = 0,
        projection_key: str = "",
    ) -> Finding:
        base, half = self.pheromone.lookup(kind)
        if pheromone_base != 1.0:
            base = max(0.0, min(1.0, pheromone_base))
        effective_half = half if half_life_sec == 3600 else half_life_sec
        key = str(projection_key or "")
        columns = self._finding_columns()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO swarm_findings
                  (challenge_id, agent_name, finding_type, target, data,
                   pheromone_base, half_life_sec, embedding, source_seq, projection_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (challenge_id, projection_key)
                  WHERE projection_key IS NOT NULL
                DO NOTHING
                RETURNING {columns}
                """,
                (
                    challenge_id, agent_name, kind, target,
                    json.dumps(payload or {}, ensure_ascii=False), base, effective_half,
                    embedding, source_seq or None, key or None,
                ),
            )
            row = cur.fetchone()
            if row is None and key:
                cur.execute(
                    f"SELECT {columns} FROM swarm_findings "
                    "WHERE challenge_id=%s AND projection_key=%s",
                    (challenge_id, key),
                )
                row = cur.fetchone()
            if row is None:
                raise RuntimeError("PostgresBoard failed to insert finding")
            conn.commit()
        return self._finding_from_row(row)

    def query_findings(self, predicate: FindingPredicate) -> list[Finding]:
        where = ["superseded_by IS NULL"]
        args: list[Any] = []
        if predicate.kinds:
            where.append("finding_type = ANY(%s)")
            args.append(list(predicate.kinds))
        if predicate.target_prefix:
            where.append("target LIKE %s")
            args.append(predicate.target_prefix + "%")
        if predicate.since_seq:
            where.append("seq > %s")
            args.append(predicate.since_seq)
        if predicate.limit > 0:
            limit_sql = "LIMIT %s"
            args.append(predicate.limit)
        else:
            limit_sql = ""
        query = (
            f"SELECT {self._finding_columns()} FROM swarm_findings "
            f"WHERE {' AND '.join(where)} ORDER BY seq DESC {limit_sql}"
        )
        out: list[Finding] = []
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(query, args)
            for row in cur.fetchall():
                finding = self._finding_from_row(row)
                if predicate.matches(finding, now=time.time()):
                    out.append(finding)
        return out

    def replace_by_source(
        self, *, source_seq: int, finding: Finding, projection_key: str
    ) -> ReplacementResult:
        key = str(projection_key or "")
        if not key:
            raise ValueError("projection_key is required for replacement")
        base, half = self.pheromone.lookup(finding.kind)
        if finding.pheromone_base != 1.0:
            base = max(0.0, min(1.0, finding.pheromone_base))
        effective_half = half if finding.half_life_sec == 3600 else finding.half_life_sec
        columns = self._finding_columns()
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO swarm_findings
                  (challenge_id, agent_name, finding_type, target, data,
                   pheromone_base, half_life_sec, embedding, source_seq, projection_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (challenge_id, projection_key)
                  WHERE projection_key IS NOT NULL
                DO NOTHING
                RETURNING finding_id, {columns}
                """,
                (
                    finding.challenge_id or self.challenge_id, finding.agent_name,
                    finding.kind, finding.target,
                    json.dumps(finding.payload or {}, ensure_ascii=False), base,
                    effective_half, finding.embedding, int(source_seq), key,
                ),
            )
            inserted = cur.fetchone()
            if inserted is None:
                cur.execute(
                    f"SELECT {columns} FROM swarm_findings "
                    "WHERE challenge_id=%s AND projection_key=%s",
                    (finding.challenge_id or self.challenge_id, key),
                )
                existing = cur.fetchone()
                if existing is None:
                    raise RuntimeError("projection conflict row disappeared")
                conn.commit()
                return ReplacementResult(
                    ReplacementOutcome.ALREADY_APPLIED,
                    self._finding_from_row(existing),
                )

            new_uuid, *row = inserted
            cur.execute(
                """
                SELECT finding_id FROM swarm_findings
                WHERE challenge_id=%s AND source_seq=%s
                  AND superseded_by IS NULL AND finding_id<>%s::uuid
                FOR UPDATE
                """,
                (finding.challenge_id or self.challenge_id, int(source_seq), new_uuid),
            )
            prior_ids = tuple(str(item[0]) for item in cur.fetchall())
            if prior_ids:
                cur.execute(
                    """
                    UPDATE swarm_findings SET superseded_by=%s::uuid
                    WHERE challenge_id=%s AND source_seq=%s
                      AND superseded_by IS NULL AND finding_id<>%s::uuid
                    """,
                    (
                        new_uuid, finding.challenge_id or self.challenge_id,
                        int(source_seq), new_uuid,
                    ),
                )
            conn.commit()
        replacement = self._finding_from_row(tuple(row))
        return ReplacementResult(
            ReplacementOutcome.REPLACED if prior_ids
            else ReplacementOutcome.INSERTED_NO_PRIOR,
            replacement,
            prior_ids,
        )

    async def subscribe(self, predicate: FindingPredicate) -> AsyncIterator[Finding]:
        cursor = predicate.since_seq
        while True:
            pred = FindingPredicate(
                kinds=predicate.kinds,
                target_prefix=predicate.target_prefix,
                min_pheromone=predicate.min_pheromone,
                since_seq=cursor,
                limit=predicate.limit,
            )
            findings = self.query_findings(pred)
            for f in reversed(findings):
                cursor = max(cursor, f.seq)
                yield f
            await asyncio.sleep(0.5)

    def cursor(self, challenge_id: str, agent_name: str) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT last_seq FROM swarm_agent_cursors "
                "WHERE challenge_id=%s AND agent_name=%s",
                (challenge_id, agent_name),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0

    def commit_cursor(self, challenge_id: str, agent_name: str, last_seq: int) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO swarm_agent_cursors (challenge_id, agent_name, last_seq)
                VALUES (%s, %s, %s)
                ON CONFLICT (challenge_id, agent_name)
                DO UPDATE SET last_seq = EXCLUDED.last_seq
                """,
                (challenge_id, agent_name, int(last_seq)),
            )
            conn.commit()

    def supersede(self, old_id: str, new_id: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE swarm_findings SET superseded_by=%s::uuid "
                "WHERE finding_id=%s::uuid",
                (new_id, old_id),
            )
            conn.commit()

    def budget(self, challenge_id: str) -> dict[str, Any]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO swarm_budgets (challenge_id)
                VALUES (%s)
                ON CONFLICT (challenge_id) DO NOTHING
                """,
                (challenge_id,),
            )
            conn.commit()
            cur.execute(
                "SELECT max_agent_hours, max_tokens, used_hours, used_tokens "
                "FROM swarm_budgets WHERE challenge_id=%s",
                (challenge_id,),
            )
            row = cur.fetchone()
        if not row:
            return {}
        return {
            "max_agent_hours": float(row[0]),
            "max_tokens": int(row[1]),
            "used_hours": float(row[2]),
            "used_tokens": int(row[3]),
        }

    def update_budget(
        self, challenge_id: str, delta_hours: float = 0.0, delta_tokens: int = 0
    ) -> None:
        self.budget(challenge_id)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE swarm_budgets SET used_hours=used_hours+%s, "
                "used_tokens=used_tokens+%s WHERE challenge_id=%s",
                (float(delta_hours), int(delta_tokens), challenge_id),
            )
            conn.commit()

    def agent_budget(self, challenge_id: str, agent_name: str) -> dict[str, Any]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO swarm_agent_budgets (challenge_id, agent_name)
                VALUES (%s, %s)
                ON CONFLICT (challenge_id, agent_name) DO NOTHING
                """,
                (challenge_id, agent_name),
            )
            conn.commit()
            cur.execute(
                "SELECT max_tokens, warn_at_tokens, used_tokens, warned "
                "FROM swarm_agent_budgets WHERE challenge_id=%s AND agent_name=%s",
                (challenge_id, agent_name),
            )
            row = cur.fetchone()
        if not row:
            return {}
        return {
            "max_tokens": int(row[0]),
            "warn_at_tokens": int(row[1]),
            "used_tokens": int(row[2]),
            "warned": bool(row[3]),
        }

    def charge_agent(
        self, challenge_id: str, agent_name: str, tokens: int = 0
    ) -> None:
        self.agent_budget(challenge_id, agent_name)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE swarm_agent_budgets SET used_tokens=used_tokens+%s "
                "WHERE challenge_id=%s AND agent_name=%s",
                (int(tokens), challenge_id, agent_name),
            )
            conn.commit()
