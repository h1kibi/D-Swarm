"""Target card API — the deck's Target Library / Workspace / Launch Wizard.

This is the store-backed Target layer (TargetStore, run-linked): durable
operator-facing cards with directions, hints, audit records, activity, and
provenance projections from the runs' shared graphs.

It replaces the phase-2-4 DB (ctf_targets) router on this path: docs/13-16 and
tests/test_web_targets.py are the contract, and the deck frontend
(lib/useTargets.ts) is the consumer. Auth mirrors the deck's run rail: no
dependency of its own; the P3 gate middleware covers exposure, and flag values
are redacted for unauthenticated readers.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from apps.web.http_utils import _require_dict_body
from apps.web.principal import require_operator
from apps.web.routes.runs import _redact_summary, _request_authenticated

router = APIRouter(
    prefix="/api/targets", tags=["targets"], dependencies=[Depends(require_operator)]
)


def _manager(request: Request) -> Any:
    return request.app.state.manager


def _store(request: Request) -> Any:
    return request.app.state.target_store


def _get_card_or_404(request: Request, target_id: str) -> dict[str, Any]:
    card = _store(request).get(target_id)
    if card is None:
        raise HTTPException(status_code=404, detail="target not found")
    return card


def _run_summaries(request: Request, card: dict[str, Any]) -> list[dict[str, Any]]:
    """Attached runs, through the same DTO + containment boundary as /api/runs."""
    mgr = _manager(request)
    authenticated = _request_authenticated(request)
    summaries: list[dict[str, Any]] = []
    for run_id in card.get("run_ids") or []:
        run = mgr.get(str(run_id))
        if run is None:
            continue
        summary = run.summary()
        if not authenticated:
            summary = _redact_summary(summary)
        summaries.append(summary)
    return summaries


def _target_status(summaries: list[dict[str, Any]], archived_at: Any) -> str:
    """Mirror lib/targetProjection.ts statusFor() over the run summaries."""
    if archived_at:
        return "archived"
    if any(item.get("solved") for item in summaries):
        return "solved"
    if any(item.get("status") == "failed" for item in summaries):
        return "failed"
    if any(item.get("status") == "reason_blocked" or item.get("reason_blocked") for item in summaries):
        return "reason_blocked"
    if any(item.get("status") == "fallback_active" or item.get("fallback_active") for item in summaries):
        return "fallback_active"
    if any(item.get("status") == "degraded" or item.get("degraded") for item in summaries):
        return "degraded"
    if any(item.get("status") == "running" for item in summaries):
        return "active"
    if any(item.get("status") in ("queued", "paused") for item in summaries):
        return "waiting"
    if summaries and all(item.get("archived") for item in summaries):
        return "archived"
    return "idle"


_FACTS_CACHE: dict[tuple[str, int], list[dict[str, Any]]] = {}
_FACTS_CACHE_MAX = 64


def _facts_for_run(mgr: Any, run_id: str) -> list[dict[str, Any]]:
    """Verified facts projected read-only from one run's shared graph.

    Memoized by (graph path, mtime): the deck polls /api/targets every few
    seconds and the list projection would otherwise reopen every attached
    run's SQLite graph on every poll.
    """
    db_path = mgr.workspace_dir(run_id) / "graph" / "shared_graph.db"
    if not db_path.exists():
        return []
    try:
        stamp = db_path.stat().st_mtime_ns
    except OSError:
        return []
    cache_key = (str(db_path), stamp)
    cached = _FACTS_CACHE.get(cache_key)
    if cached is not None:
        return [dict(item) for item in cached]

    from dswarm.models.solve_graph import Challenge
    from dswarm.swarm.shared_graph import SQLiteSharedGraph

    run = mgr.get(run_id)
    challenge = Challenge(
        id=run_id,
        name=((run.name if run is not None else "") or run_id),
        category=((run.category if run is not None else "") or "misc"),
    )
    graph = None
    try:
        graph = SQLiteSharedGraph.open(db_path=db_path, challenge=challenge)
        rows = graph.effective_facts(verified_only=True, active_only=True)
    except Exception:
        return []
    finally:
        if graph is not None:
            graph.close()

    facts: list[dict[str, Any]] = []
    for row in rows:
        text = str(row.get("fact_text") or "").strip()
        if not text:
            continue
        facts.append(
            {
                "fact_id": f"fact:{run_id}:{int(row.get('fact_seq'))}",
                "fact": text,
                "source_run_id": run_id,
                "fact_seq": int(row.get("fact_seq")),
                "artifact_id": row.get("artifact_id"),
                "provenance_status": "verified",
            }
        )
    _FACTS_CACHE[cache_key] = [dict(item) for item in facts]
    if len(_FACTS_CACHE) > _FACTS_CACHE_MAX:
        # Bounded memory: drop the oldest inserted entry (dicts keep order).
        _FACTS_CACHE.pop(next(iter(_FACTS_CACHE)))
    return facts


def _all_facts(request: Request, card: dict[str, Any]) -> list[dict[str, Any]]:
    """Verified facts across the card's runs + reverse inheritance lineage."""
    mgr = _manager(request)
    facts: list[dict[str, Any]] = []
    for run_id in card.get("run_ids") or []:
        facts.extend(_facts_for_run(mgr, str(run_id)))

    inherited: dict[str, list[str]] = {}
    for run_id in card.get("run_ids") or []:
        run = mgr.get(str(run_id))
        if run is None:
            continue
        for record in run.inherited_verified_facts or []:
            fact_id = str(record.get("fact_id") or "") if isinstance(record, dict) else ""
            if not fact_id:
                continue
            owners = inherited.setdefault(fact_id, [])
            if str(run_id) not in owners:
                owners.append(str(run_id))

    for item in facts:
        owners = inherited.get(item["fact_id"], [])
        item["inherited_by_run_ids"] = owners
        item["inherited_by_run_count"] = len(owners)
    return facts


def _target_card(request: Request, card: dict[str, Any]) -> dict[str, Any]:
    summaries = _run_summaries(request, card)
    facts = _all_facts(request, card)
    projected = dict(card)
    projected["status"] = _target_status(summaries, card.get("archived_at"))
    projected["run_count"] = len(summaries)
    projected["runs"] = summaries
    projected["active_count"] = sum(1 for item in summaries if item.get("status") == "running")
    projected["needs_attention"] = any(
        item.get("status") in ("failed", "reason_blocked") for item in summaries
    )
    projected["verified_fact_count"] = len(facts)
    return projected


@router.get("")
async def list_targets(request: Request, archived: int = 0) -> dict[str, Any]:
    cards = _store(request).list(include_archived=bool(archived))
    return {"targets": [_target_card(request, card) for card in cards]}


@router.post("")
async def create_target(request: Request) -> dict[str, Any]:
    body = await _require_dict_body(request)
    card = _store(request).create(body)
    return {"target": _target_card(request, card)}


@router.get("/{target_id}")
async def get_target(target_id: str, request: Request) -> dict[str, Any]:
    return {"target": _target_card(request, _get_card_or_404(request, target_id))}


@router.patch("/{target_id}")
async def update_target(target_id: str, request: Request) -> dict[str, Any]:
    body = await _require_dict_body(request)
    card = _store(request).update(target_id, body)
    if card is None:
        raise HTTPException(status_code=404, detail="target not found")
    return {"target": _target_card(request, card)}


@router.post("/{target_id}/archive")
async def archive_target(target_id: str, request: Request) -> dict[str, Any]:
    body = await _require_dict_body(request, allow_empty=True)
    card = _store(request).archive(target_id, bool(body.get("archived", True)))
    if card is None:
        raise HTTPException(status_code=404, detail="target not found")
    return {"target": _target_card(request, card)}


@router.post("/{target_id}/directions")
async def add_direction(target_id: str, request: Request) -> dict[str, Any]:
    body = await _require_dict_body(request)
    direction = _store(request).add_direction(
        target_id,
        str(body.get("label") or ""),
        description=str(body.get("description") or ""),
        category=str(body.get("category") or ""),
    )
    if direction is None:
        raise HTTPException(status_code=404, detail="target not found")
    return {"direction": direction}


@router.post("/{target_id}/hints")
async def add_hint(target_id: str, request: Request) -> dict[str, Any]:
    body = await _require_dict_body(request)
    hint = _store(request).add_hint(
        target_id,
        str(body.get("text") or ""),
        pinned=bool(body.get("pinned", False)),
    )
    if hint is None:
        raise HTTPException(status_code=404, detail="target not found")
    return {"hint": hint}


@router.post("/{target_id}/runs")
async def create_target_run(target_id: str, request: Request) -> dict[str, Any]:
    """Mint a target-bound draft run (the Launch Wizard's landing pad).

    Hint selection and inherited verified facts are persisted through the
    manager's dispatch-context sidecar so /start can expand them restart-safely.
    """
    body = await _require_dict_body(request, allow_empty=True)
    card = _get_card_or_404(request, target_id)
    store = _store(request)
    mgr = _manager(request)

    direction_id = str(body.get("direction_id") or "").strip()
    direction_label = ""
    if direction_id:
        direction = store.get_direction(target_id, direction_id)
        if direction is None:
            raise HTTPException(
                status_code=400, detail="direction does not belong to this target"
            )
        direction_label = str(direction.get("label") or "")
    else:
        direction_label = str(body.get("direction") or "").strip()

    run = mgr.create_new()
    run.target_id = target_id
    run.target_name = str(card.get("name") or "")
    run.target_category = str(card.get("category") or "")
    if direction_id:
        run.direction_id = direction_id
    if direction_label:
        run.direction = direction_label
    store.attach_run(target_id, run.run_id)

    context: dict[str, Any] = {}
    if "include_target_hints" in body:
        context["include_target_hints"] = bool(body.get("include_target_hints"))
    if "target_hint_ids" in body:
        raw_ids = body.get("target_hint_ids") or []
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        context["target_hint_ids"] = [str(value) for value in raw_ids if str(value).strip()]

    requested_fact_ids = body.get("verified_fact_ids") or []
    if isinstance(requested_fact_ids, str):
        requested_fact_ids = [requested_fact_ids]
    if requested_fact_ids:
        by_id = {item["fact_id"]: item for item in _all_facts(request, card)}
        inherited: list[dict[str, Any]] = []
        for fact_id in [str(value) for value in requested_fact_ids if str(value).strip()]:
            item = by_id.get(fact_id)
            if item is None:
                continue
            record: dict[str, Any] = {
                "fact_id": item["fact_id"],
                "fact": item["fact"],
                "provenance_status": item["provenance_status"],
                "source_run_id": item["source_run_id"],
                "fact_seq": item["fact_seq"],
            }
            if item.get("artifact_id"):
                record["artifact_id"] = item["artifact_id"]
            inherited.append(record)
        if inherited:
            context["inherited_verified_facts"] = inherited

    if context:
        mgr.remember_run_context(run.run_id, context)

    summary = run.summary()
    if not _request_authenticated(request):
        summary = _redact_summary(summary)
    return {"run": summary}


@router.get("/{target_id}/runs")
async def list_target_runs(target_id: str, request: Request) -> dict[str, Any]:
    card = _get_card_or_404(request, target_id)
    return {"runs": _run_summaries(request, card)}


@router.get("/{target_id}/facts")
async def list_target_facts(target_id: str, request: Request) -> dict[str, Any]:
    card = _get_card_or_404(request, target_id)
    return {"facts": _all_facts(request, card)}


@router.get("/{target_id}/activity")
async def list_target_activity(target_id: str, request: Request) -> dict[str, Any]:
    card = _get_card_or_404(request, target_id)
    mgr = _manager(request)
    activity = [item for item in (card.get("activity") or []) if isinstance(item, dict)]

    # Verified facts are a read-only projection of the runs' graphs; they are
    # never appended to the card (the card's activity log stays operator-owned).
    for item in _all_facts(request, card):
        run = mgr.get(item["source_run_id"])
        activity.append(
            {
                "id": f"activity-proj-{item['source_run_id']}-{item['fact_seq']}",
                "kind": "fact.verified",
                "label": "Fact verified",
                "source": "run_projection",
                "run_id": item["source_run_id"],
                "source_seq": int(item["fact_seq"]),
                "fact_id": item["fact_id"],
                "timestamp": (getattr(run, "updated_at", 0.0) if run is not None else 0.0) or 0.0,
            }
        )

    activity.sort(key=lambda entry: float(entry.get("timestamp") or 0.0), reverse=True)
    return {"activity": activity}


def _auditable_facts_for_run(mgr: Any, run_id: str) -> dict[int, dict[str, Any]]:
    """Facts eligible for an audit record.

    The union of effective verified facts and facts whose review decision was
    ``verified`` (the decision carries the bounded evidence references; the
    promotion to ``fact_verified`` is a later, separate lifecycle step).
    """
    db_path = mgr.workspace_dir(run_id) / "graph" / "shared_graph.db"
    if not db_path.exists():
        return {}
    from dswarm.models.solve_graph import Challenge
    from dswarm.swarm.shared_graph import SQLiteSharedGraph

    run = mgr.get(run_id)
    challenge = Challenge(
        id=run_id,
        name=((run.name if run is not None else "") or run_id),
        category=((run.category if run is not None else "") or "misc"),
    )
    graph = None
    try:
        graph = SQLiteSharedGraph.open(db_path=db_path, challenge=challenge)
        entries: dict[int, dict[str, Any]] = {}
        for row in graph.effective_facts():
            seq = int(row.get("fact_seq"))
            entries[seq] = {
                "fact": str(row.get("fact_text") or ""),
                "artifact_id": row.get("artifact_id"),
                "verified": bool(row.get("verified")),
                "evidence_refs": [],
            }
        for decision in graph.evidence_review_decisions():
            if str(decision.get("decision") or "") != "verified":
                continue
            try:
                seq = int(decision.get("fact_seq"))
            except (TypeError, ValueError):
                continue
            entry = entries.setdefault(
                seq,
                {"fact": "", "artifact_id": None, "verified": False, "evidence_refs": []},
            )
            refs = [str(ref) for ref in (decision.get("evidence_refs") or []) if str(ref).strip()]
            if refs:
                entry["evidence_refs"] = refs
            entry["review_approved"] = True
    except Exception:
        return {}
    finally:
        if graph is not None:
            graph.close()

    return {
        seq: entry
        for seq, entry in entries.items()
        if entry.get("verified") or entry.get("review_approved")
    }


@router.post("/{target_id}/audit-review")
async def audit_review(target_id: str, request: Request) -> dict[str, Any]:
    """Persist operator-approved provenance records for a terminal run.

    The record links a verified fact (from the run's shared graph) back to its
    source run + artifact evidence; the raw graph stays untouched.
    """
    body = await _require_dict_body(request)
    card = _get_card_or_404(request, target_id)
    store = _store(request)
    mgr = _manager(request)

    run_id = str(body.get("run_id") or "").strip()
    run = mgr.get(run_id) if run_id else None
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run_id not in (card.get("run_ids") or ()):
        raise HTTPException(status_code=400, detail="run is not attached to this target")
    if not (run.finished or run.reason_blocked or run.cancelled):
        raise HTTPException(status_code=400, detail="run is not terminal")

    auditor = str(body.get("auditor") or "operator").strip() or "operator"
    requested = body.get("facts") or []
    if isinstance(requested, dict):
        requested = [requested]

    by_seq = _auditable_facts_for_run(mgr, run_id)
    records: list[dict[str, Any]] = []
    for entry in requested:
        try:
            fact_seq = int((entry or {}).get("fact_seq"))
        except (TypeError, ValueError):
            continue
        item = by_seq.get(fact_seq)
        if item is None:
            continue
        refs = list(item.get("evidence_refs") or [])
        if not refs and item.get("artifact_id"):
            refs = [f"artifact:{item['artifact_id']}"]
        saved = store.add_audit_record(
            target_id,
            {
                "source_run_id": run_id,
                "item_type": "fact",
                "fact_seq": fact_seq,
                "fact": item.get("fact") or "",
                "evidence_refs": refs,
                "auditor": auditor,
            },
        )
        if saved is not None:
            records.append(saved)

    if not records:
        raise HTTPException(status_code=400, detail="no verified facts matched")
    return {"audit_records": records, "target": _target_card(request, card)}
