"""M3 canonical fact-event contract and SQLite projection schema.

The ``events`` table is the source of truth.  ``fact_effective`` is a
rebuildable fold; it never writes back to canonical event rows.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_USER_VERSION = 2
FACT_TRANSITION_KINDS = (
    "fact_verified",
    "fact_summarized",
    "fact_challenged",
    "fact_revalidated",
    "fact_rejected",
    "fact_merged",
    "fact_superseded",
)

# This is deliberately one canonical SELECT shared by the persistent VIEW and
# compatibility reads of pre-v2 databases.  Keep the column contract stable.
FACT_EFFECTIVE_SELECT = r"""
WITH
promotion AS (
    SELECT challenge_id,
           CAST(json_extract(payload, '$.fact_seq') AS INTEGER) AS fact_seq,
           MAX(seq) AS seq
    FROM events
    WHERE kind = 'fact_verified' AND json_valid(payload)
    GROUP BY challenge_id, CAST(json_extract(payload, '$.fact_seq') AS INTEGER)
),
summary_event AS (
    SELECT challenge_id,
           CAST(json_extract(payload, '$.fact_seq') AS INTEGER) AS fact_seq,
           MAX(seq) AS seq
    FROM events
    WHERE kind = 'fact_summarized' AND json_valid(payload)
    GROUP BY challenge_id, CAST(json_extract(payload, '$.fact_seq') AS INTEGER)
),
lifecycle_rows AS (
    SELECT challenge_id,
           CAST(json_extract(payload, '$.fact_seq') AS INTEGER) AS fact_seq,
           seq, kind
    FROM events
    WHERE kind IN ('fact_challenged','fact_revalidated','fact_rejected','fact_superseded')
      AND json_valid(payload)
    UNION ALL
    SELECT challenge_id,
           CAST(json_extract(payload, '$.from_fact_seq') AS INTEGER) AS fact_seq,
           seq, kind
    FROM events
    WHERE kind = 'fact_merged' AND json_valid(payload)
),
latest_lifecycle AS (
    SELECT lr.challenge_id, lr.fact_seq, lr.seq, lr.kind
    FROM lifecycle_rows lr
    JOIN (
        SELECT challenge_id, fact_seq, MAX(seq) AS seq
        FROM lifecycle_rows
        GROUP BY challenge_id, fact_seq
    ) latest
      ON latest.challenge_id = lr.challenge_id
     AND latest.fact_seq = lr.fact_seq
     AND latest.seq = lr.seq
),
terminal AS (
    SELECT challenge_id, fact_seq, MIN(seq) AS seq
    FROM lifecycle_rows
    WHERE kind IN ('fact_rejected','fact_merged','fact_superseded')
    GROUP BY challenge_id, fact_seq
)
SELECT
    f.seq AS fact_seq,
    f.challenge_id AS challenge_id,
    json_extract(f.payload, '$.fact') AS fact_text,
    json_extract(f.payload, '$.source') AS fact_source,
    f.actor AS fact_actor,
    f.ts AS fact_ts,
    CASE WHEN f.verified <> 0 OR p.seq IS NOT NULL THEN 1 ELSE 0 END AS base_verified,
    COALESCE(json_extract(pv.payload, '$.confidence'), f.confidence) AS base_confidence,
    json_extract(f.payload, '$.route_hash') AS route_hash,
    json_extract(f.payload, '$.finding.kind') AS finding_kind,
    json_extract(f.payload, '$.finding.target') AS finding_target,
    json_extract(f.payload, '$.finding.data') AS finding_data,
    p.seq AS promotion_seq,
    pv.actor AS promotion_actor,
    pv.artifact_id AS promotion_artifact_id,
    COALESCE(pv.artifact_id, f.artifact_id) AS artifact_id,
    COALESCE(json_extract(pv.payload, '$.witness'), json_extract(f.payload, '$.witness')) AS witness,
    COALESCE(json_extract(pv.payload, '$.verifier'), json_extract(f.payload, '$.verifier')) AS verifier,
    COALESCE(json_extract(pv.payload, '$.source'), json_extract(f.payload, '$.source')) AS source,
    se.seq AS summary_seq,
    COALESCE(json_extract(s.payload, '$.summary'), json_extract(f.payload, '$.summary')) AS summary,
    CASE
        WHEN ll.kind IS NULL THEN CASE WHEN f.verified <> 0 OR p.seq IS NOT NULL THEN 'verified' ELSE 'candidate' END
        WHEN ll.kind = 'fact_challenged' THEN 'challenged'
        WHEN ll.kind = 'fact_revalidated' THEN 'revalidated'
        WHEN ll.kind = 'fact_rejected' THEN 'rejected'
        WHEN ll.kind = 'fact_merged' THEN 'merged'
        WHEN ll.kind = 'fact_superseded' THEN 'superseded'
        ELSE 'candidate'
    END AS state,
    CASE WHEN t.seq IS NOT NULL THEN 1 ELSE 0 END AS retired,
    CASE
        WHEN t.seq IS NOT NULL THEN 0
        WHEN ll.kind = 'fact_challenged' THEN 0
        ELSE CASE WHEN f.verified <> 0 OR p.seq IS NOT NULL THEN 1 ELSE 0 END
    END AS verified,
    CASE
        WHEN t.seq IS NOT NULL THEN 0.0
        WHEN ll.kind = 'fact_challenged' THEN 0.4
        ELSE COALESCE(json_extract(pv.payload, '$.confidence'), f.confidence)
    END AS confidence
FROM events f
LEFT JOIN promotion p ON p.challenge_id = f.challenge_id AND p.fact_seq = f.seq
LEFT JOIN events pv ON pv.seq = p.seq
LEFT JOIN summary_event se ON se.challenge_id = f.challenge_id AND se.fact_seq = f.seq
LEFT JOIN events s ON s.seq = se.seq
LEFT JOIN latest_lifecycle ll ON ll.challenge_id = f.challenge_id AND ll.fact_seq = f.seq
LEFT JOIN terminal t ON t.challenge_id = f.challenge_id AND t.fact_seq = f.seq
WHERE f.kind = 'fact_added' AND json_valid(f.payload)
""".strip()

FACT_EVENT_SCHEMA = f"""
DROP VIEW IF EXISTS fact_effective;
CREATE VIEW fact_effective AS
{FACT_EFFECTIVE_SELECT};

CREATE UNIQUE INDEX IF NOT EXISTS ux_events_fact_verified_once
ON events(challenge_id, CAST(json_extract(payload, '$.fact_seq') AS INTEGER))
WHERE kind = 'fact_verified';

CREATE UNIQUE INDEX IF NOT EXISTS ux_events_fact_summarized_once
ON events(challenge_id, CAST(json_extract(payload, '$.fact_seq') AS INTEGER))
WHERE kind = 'fact_summarized';

CREATE TRIGGER IF NOT EXISTS events_immutable_update
BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;

CREATE TRIGGER IF NOT EXISTS events_immutable_delete
BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are immutable'); END;

CREATE TRIGGER IF NOT EXISTS fact_transition_json_guard
BEFORE INSERT ON events
WHEN NEW.kind IN ('fact_verified','fact_summarized','fact_challenged','fact_revalidated','fact_rejected','fact_superseded')
 AND (
      json_valid(NEW.payload) = 0
      OR COALESCE(json_type(NEW.payload, '$.fact_seq'), '') <> 'integer'
 )
BEGIN SELECT RAISE(ABORT, 'fact transition requires valid JSON with integer fact_seq'); END;

CREATE TRIGGER IF NOT EXISTS fact_transition_target_guard
BEFORE INSERT ON events
WHEN NEW.kind IN ('fact_verified','fact_summarized','fact_challenged','fact_revalidated','fact_rejected','fact_superseded')
 AND NOT EXISTS (
      SELECT 1 FROM events target
      WHERE target.seq = CAST(json_extract(NEW.payload, '$.fact_seq') AS INTEGER)
        AND target.kind = 'fact_added'
        AND target.challenge_id = NEW.challenge_id
 )
BEGIN SELECT RAISE(ABORT, 'fact transition target must be a same-challenge fact_added'); END;

CREATE TRIGGER IF NOT EXISTS fact_verified_candidate_guard
BEFORE INSERT ON events
WHEN NEW.kind = 'fact_verified'
 AND EXISTS (
      SELECT 1 FROM events target
      WHERE target.seq = CAST(json_extract(NEW.payload, '$.fact_seq') AS INTEGER)
        AND target.challenge_id = NEW.challenge_id
        AND target.kind = 'fact_added'
        AND target.verified <> 0
 )
BEGIN SELECT RAISE(ABORT, 'fact_verified may only promote a candidate'); END;

CREATE TRIGGER IF NOT EXISTS fact_summary_shape_guard
BEFORE INSERT ON events
WHEN NEW.kind = 'fact_summarized'
 AND (
      COALESCE(json_type(NEW.payload, '$.summary'), '') <> 'text'
      OR length(trim(COALESCE(json_extract(NEW.payload, '$.summary'), ''))) = 0
 )
BEGIN SELECT RAISE(ABORT, 'fact_summarized requires non-empty summary'); END;

CREATE TRIGGER IF NOT EXISTS fact_merge_json_guard
BEFORE INSERT ON events
WHEN NEW.kind = 'fact_merged'
 AND (
      json_valid(NEW.payload) = 0
      OR COALESCE(json_type(NEW.payload, '$.from_fact_seq'), '') <> 'integer'
      OR COALESCE(json_type(NEW.payload, '$.to_fact_seq'), '') <> 'integer'
      OR json_extract(NEW.payload, '$.from_fact_seq') = json_extract(NEW.payload, '$.to_fact_seq')
 )
BEGIN SELECT RAISE(ABORT, 'fact_merged requires distinct integer from/to fact seqs'); END;

CREATE TRIGGER IF NOT EXISTS fact_merge_target_guard
BEFORE INSERT ON events
WHEN NEW.kind = 'fact_merged'
 AND (
      NOT EXISTS (
          SELECT 1 FROM events target
          WHERE target.seq = CAST(json_extract(NEW.payload, '$.from_fact_seq') AS INTEGER)
            AND target.kind = 'fact_added' AND target.challenge_id = NEW.challenge_id
      )
      OR NOT EXISTS (
          SELECT 1 FROM events target
          WHERE target.seq = CAST(json_extract(NEW.payload, '$.to_fact_seq') AS INTEGER)
            AND target.kind = 'fact_added' AND target.challenge_id = NEW.challenge_id
      )
 )
BEGIN SELECT RAISE(ABORT, 'fact_merged targets must be same-challenge fact_added rows'); END;
"""


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def require_supported_version(conn: sqlite3.Connection, supported: int = SCHEMA_USER_VERSION) -> int:
    version = user_version(conn)
    if version > supported:
        raise sqlite3.DatabaseError(
            f"database user_version {version} is newer than supported {supported}"
        )
    return version


def preflight_fact_events(conn: sqlite3.Connection) -> None:
    """Reject historical transition corruption before installing JSON indexes/view."""
    rows = conn.execute(
        "SELECT seq, challenge_id, kind, payload FROM events WHERE kind IN (%s) ORDER BY seq"
        % ",".join("?" for _ in FACT_TRANSITION_KINDS),
        FACT_TRANSITION_KINDS,
    ).fetchall()
    seen_verified: set[tuple[str, int]] = set()
    seen_summaries: set[tuple[str, int]] = set()
    for seq, challenge_id, kind, raw_payload in rows:
        try:
            payload = json.loads(raw_payload)
        except (TypeError, json.JSONDecodeError) as exc:
            raise sqlite3.DatabaseError(f"malformed fact transition at events.seq={seq}") from exc
        if not isinstance(payload, dict):
            raise sqlite3.DatabaseError(f"non-object fact transition at events.seq={seq}")
        if kind == "fact_merged":
            keys = ("from_fact_seq", "to_fact_seq")
            values = [payload.get(k) for k in keys]
            if any(type(v) is not int for v in values) or values[0] == values[1]:
                raise sqlite3.DatabaseError(f"invalid fact_merged payload at events.seq={seq}")
            targets = values
        else:
            fact_seq = payload.get("fact_seq")
            if type(fact_seq) is not int:
                raise sqlite3.DatabaseError(f"invalid fact_seq at events.seq={seq}")
            targets = [fact_seq]
        for target_seq in targets:
            target = conn.execute(
                "SELECT kind, challenge_id, verified FROM events WHERE seq=?", (target_seq,)
            ).fetchone()
            if not target or target[0] != "fact_added" or str(target[1]) != str(challenge_id):
                raise sqlite3.DatabaseError(f"invalid fact target at events.seq={seq}")
        if kind == "fact_verified":
            target = conn.execute("SELECT verified FROM events WHERE seq=?", (targets[0],)).fetchone()
            if target and int(target[0] or 0):
                raise sqlite3.DatabaseError(f"promotion targets verified genesis at events.seq={seq}")
            key = (str(challenge_id), int(targets[0]))
            if key in seen_verified:
                raise sqlite3.DatabaseError(f"duplicate fact_verified at events.seq={seq}")
            seen_verified.add(key)
        if kind == "fact_summarized":
            summary = payload.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                raise sqlite3.DatabaseError(f"invalid fact summary at events.seq={seq}")
            key = (str(challenge_id), int(targets[0]))
            if key in seen_summaries:
                raise sqlite3.DatabaseError(f"duplicate fact_summarized at events.seq={seq}")
            seen_summaries.add(key)


def install_fact_event_contract(conn: sqlite3.Connection) -> None:
    """Install the M3 projection contract atomically, bumping version last."""
    preflight_fact_events(conn)
    script = (
        "BEGIN IMMEDIATE;\n"
        + FACT_EVENT_SCHEMA
        + f"\nPRAGMA user_version={SCHEMA_USER_VERSION};\nCOMMIT;"
    )
    try:
        conn.executescript(script)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def backup_database(conn: sqlite3.Connection, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(str(target))
    # sqlite3's backup API is a consistent online snapshot and works on Windows.
    backup = sqlite3.connect(str(target))
    try:
        conn.backup(backup)
        backup.commit()
    finally:
        backup.close()
    return target
