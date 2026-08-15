"""Reviewer coordination flow."""
from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any, Optional

from dswarm.solver.worker_profiles import normalize_profile_roster
from dswarm.swarm.budget import WorkerBudgetExhausted
from dswarm.swarm.errors import WorkerSpawnRejected
from dswarm.swarm.lane_gate import WorkerLaneDisabled, WorkerLaneStopped
from dswarm.swarm.shared_graph import canonicalize_lane


class ReviewFlowMixin:

    @staticmethod
    def _clean_review_policy(value: Any) -> dict[str, Any]:
        configured = isinstance(value, dict)
        raw = value if configured else {}
        defaults = {
            "enabled": configured,
            "engine": "",
            "after_race": True,
            "after_fruitless_workers": 3,
            "after_duplicate_intents": 2,
            "on_course_correct": True,
            "on_reason_dry": True,
            "on_candidate_spike": True,
            "on_operator_hint": True,
            "on_unverified_flag": True,
            "every_completed_workers": 6,
            "candidate_spike_threshold": 5,
            "unverified_flag_threshold": 1,
            "max_concurrent": 1,
            "allow_review_fallback": False,
            "cooldown_events": 8,
            "timeout": 420,
            "max_review_workers": 12,
            "max_challenges_per_cycle": 8,
        }
        out = dict(defaults)
        for key in ("enabled", "after_race", "on_course_correct", "on_reason_dry",
                    "on_candidate_spike", "on_operator_hint", "on_unverified_flag", "allow_review_fallback"):
            if key in raw:
                out[key] = bool(raw.get(key))
        if raw.get("engine"):
            out["engine"] = str(raw.get("engine")).strip()
        for key in ("after_fruitless_workers", "after_duplicate_intents",
                    "every_completed_workers", "candidate_spike_threshold",
                    "unverified_flag_threshold",
                    "max_concurrent", "max_challenges_per_cycle",
                    "cooldown_events", "timeout", "max_review_workers"):
            if key in raw:
                try:
                    out[key] = max(0, int(raw.get(key)))
                except (TypeError, ValueError):
                    pass
        return out

    def _candidate_fact_count(self) -> int:
        """LIVE unverified-candidate count (刀3): lifecycle-aware via
        active_candidates(), so a rejected / merged / superseded candidate stops
        counting. This drives the candidate-spike review trigger and is the number
        the board reflects — it must shrink when a candidate is retired, unlike the
        raw progress checkpoint above. Falls back to the raw event scan only if the
        lifecycle view is unavailable."""
        if self.shared_graph is None:
            return 0
        try:
            # active_candidates() already excludes verified + retired/terminal facts,
            # so its length IS the live unverified-candidate count.
            return len(self.shared_graph.active_candidates())
        except Exception:
            # M3 fallback remains projection-only: raw ``events.verified`` is
            # genesis metadata and cannot represent promotions or retirement.
            try:
                rows = self.shared_graph.effective_facts(active_only=True)
                return sum(
                    1
                    for item in rows
                    if str(item.get("state") or "") == "candidate"
                    and not bool(item.get("verified"))
                    and not bool(item.get("retired"))
                )
            except Exception:
                return 0

    def _current_graph_seq(self) -> int:
        if self.shared_graph is None:
            return 0
        try:
            evs = self.shared_graph.events()
            return int(evs[-1]["seq"]) if evs else 0
        except Exception:
            return 0

    def _select_review_engine(self, healthy: list[str]) -> str:
        configured = str(self.review_policy.get("engine") or "").strip()
        if configured:
            candidates: list[str] = []
            if self.worker_profiles:
                candidates = normalize_profile_roster([configured], self.worker_profiles)
                if configured in getattr(self, "_profiles_by_name", {}):
                    candidates = [configured] + [c for c in candidates if c != configured]
            else:
                candidates = [configured]
            for e in candidates:
                if self._healthy_matches(e, healthy) and self._engine_available_for_role(e, "review"):
                    return e
            if (self._healthy_matches(configured, healthy)
                    and self._engine_available_for_role(configured, "review")):
                return configured
            if not self.review_policy.get("allow_review_fallback", False):
                raise RuntimeError(
                    f"configured review engine unavailable: {configured}")
        return self._pick_engine([], healthy, role="review")

    def _queue_review_request(self, *, trigger: str, directive: str) -> None:
        if not self.review_policy.get("enabled", True):
            return
        trigger = (trigger or "review").strip()[:80]
        directive = (directive or "").strip()
        if not directive:
            return
        item = {"trigger": trigger, "directive": directive}
        if item in self._queued_review_requests:
            return
        self._queued_review_requests.append(item)
        if len(self._queued_review_requests) > 16:
            self._queued_review_requests = self._queued_review_requests[-16:]

    @staticmethod
    def _lane_hint_from_text(text: str, *, worker: str = "",
                             require_control_hint: bool = False) -> dict[str, Any]:
        text = text or ""
        low = text.lower()
        direct = re.search(
            r"\b(?P<risk>[a-z_][a-z0-9_-]*):tcp:"
            r"(?P<port>\*|[1-9]\d{0,4})@"
            r"(?P<host>(?:\d{1,3}\.){3}\d{1,3}|[a-z0-9][a-z0-9.-]{0,252})\b",
            low,
        )
        if direct:
            lane, confidence, degradation_reason = canonicalize_lane(
                host=direct.group("host"),
                port=None if direct.group("port") == "*" else direct.group("port"),
                service="",
                risk_class=direct.group("risk"),
            )
            risk_class = lane.split(":", 1)[0] if lane else direct.group("risk")
            return {
                "lane_key": lane,
                "risk_class": risk_class,
                "confidence": confidence,
                "degradation_reason": degradation_reason,
                "reason": text[:1000],
                "owner_worker": worker,
            }
        if require_control_hint and not any(k in low for k in (
            "lane", "destructive", "exclusive", "serialize", "serialized",
            "sequential", "one request", "single request", "single-request",
            "rate-limit", "rate sensitive", "rate-sensitive", "holds the",
            "under the", "同一", "独占", "串行", "序列化",
        )):
            return {"lane_key": "", "risk_class": "", "confidence": 0.0,
                    "degradation_reason": "no_control_hint", "reason": text[:1000],
                    "owner_worker": worker}
        host = ""
        m = re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
        if m:
            host = m.group(0)
        else:
            hm = re.search(r"\b([a-z0-9][a-z0-9.-]+\.[a-z]{2,})\b", low)
            if hm:
                host = hm.group(1)
        if require_control_hint and not host:
            return {"lane_key": "", "risk_class": "", "confidence": 0.0,
                    "degradation_reason": "no_host", "reason": text[:1000],
                    "owner_worker": worker}
        service = ""
        port: str | int | None = None
        if any(k in low for k in ("smb", "445", "eternalblue", "ms17", "relay", "responder")):
            service, port = "smb", 445
        elif "winrm" in low or "5985" in low:
            service, port = "winrm", 5985
        elif "rdp" in low or "3389" in low:
            service, port = "rdp", 3389
        elif "http" in low or "web" in low:
            service = "https" if "https" in low or "443" in low else "http"
            port = 443 if service == "https" else 80
        pm = re.search(r"(?<!\d)([1-9]\d{1,4})(?!\d)", low)
        if pm and not port:
            try:
                p = int(pm.group(1))
                if 0 < p <= 65535:
                    port = p
            except ValueError:
                port = None
        risk = "relay_service" if any(k in low for k in ("relay", "responder")) else "destructive"
        lane, confidence, degradation_reason = canonicalize_lane(
            host=host, port=port, service=service, risk_class=risk)
        return {
            "lane_key": lane,
            "risk_class": risk,
            "confidence": confidence,
            "degradation_reason": degradation_reason,
            "reason": text[:1000],
            "owner_worker": worker,
        }

    @staticmethod

    @staticmethod
    def _mechanical_need_kind(text: str) -> str:
        low = (text or "").lower()
        if any(k in low for k in (
            "ask operator", "operator decide", "need a decision from",
            "需要 operator",
        )):
            return "operator_directive_needed"
        if any(k in low for k in (
            "exclusive", "serialize", "another worker", "same target",
            "stop hammering", "独占", "序列化", "其他 worker", "其它 worker",
        )):
            return "lane_lock_request"
        if any(k in low for k in (
            "dead end", "dead-end", "route dead", "route failed",
            "known dead", "no longer viable", "repeated failures",
            "走死", "已知失败",
        )):
            return "route_dead_end"
        if any(k in low for k in (
            "unreachable", "connection refused", "refused", "timed out",
            "timeout", "expired", "instance", "502", "503", "down",
            "credential", "vps", "attachment", "token", "runtime",
            "container", "凭据", "附件",
        )):
            return "external_blocker"
        return "worker_uncertainty"

    @classmethod
    def _rechecked_need_kind(cls, need_text: str, proposed_kind: str) -> str:
        valid = {
            "external_blocker",
            "operator_directive_needed",
            "lane_lock_request",
            "route_dead_end",
            "worker_uncertainty",
        }
        proposed = (proposed_kind or "").strip().lower()
        if proposed not in valid:
            return cls._mechanical_need_kind(need_text)
        if proposed == "external_blocker":
            return cls._mechanical_need_kind(need_text)
        return proposed

    async def _consume_lane_release(self, rel: dict, *, emit_bb) -> None:
        if not rel:
            return
        lane = str(rel.get("lane_key") or "")
        for iid in rel.get("revived", []) or []:
            try:
                await emit_bb("lane_revived", intent_id=str(iid), lane_key=lane)
            except Exception:
                pass
        for iid in rel.get("escalated", []) or []:
            self._queue_review_request(
                trigger="lane_blocked",
                directive=(
                    f"lane {lane} 上 intent {iid} 长期争用；"
                    "请审查当前路线，提出绕开该资源或重新排序的 NEXT_INTENT。"
                ),
            )

    async def _maybe_start_review(
        self,
        *,
        trigger: str,
        directive: str,
        healthy: list[str],
        tasks: dict,
        task_solvers: dict,
        emit_bb,
    ) -> bool:
        if not self.review_policy.get("enabled", True):
            return False
        if self._flags_complete():
            return False
        if not self._review_capacity_available():
            return False
        if self._review_workers_spawned >= int(self.review_policy.get("max_review_workers") or 12):
            return False
        seq = self._current_graph_seq()
        cooldown = int(self.review_policy.get("cooldown_events") or 0)
        if (self._last_review_seq > 0
                and seq <= self._last_review_seq + cooldown
                and trigger != "course_correct"):
            return False
        try:
            engine = self._select_review_engine(healthy)
        except RuntimeError as exc:
            await emit_bb("worker_spawn_rejected", reason=str(exc), phase="review")
            return False
        try:
            lane = await self._worker_lane_gate.acquire(
                mode="review",
                worker_class="review",
                stop_event=getattr(self, "_reason_stop_event", None),
                pause_event=getattr(self, "_reason_pause_gate", None),
            )
        except (WorkerLaneDisabled, WorkerLaneStopped) as exc:
            await emit_bb("worker_spawn_rejected", reason=str(exc), phase="review")
            return False
        try:
            w = self._make_cli_worker(
                engine, mode="review", intent_goal=directive)
        except WorkerSpawnRejected as exc:
            self._worker_lane_gate.release(lane)
            await emit_bb("worker_spawn_rejected", reason=str(exc),
                          engine=str(engine), phase="review")
            return False
        except WorkerBudgetExhausted as exc:
            self._worker_lane_gate.release(lane)
            await emit_bb(str(exc), spawned_total=self._spawned_total,
                          max_total_workers=self.max_total_workers,
                          cost_usd=self._current_cost_usd(),
                          cost_budget_usd=self.cost_budget_usd)
            return False
        except BaseException:
            self._worker_lane_gate.release(lane)
            raise

        lane_released = False

        def _release_lane_once() -> None:
            nonlocal lane_released
            if lane_released:
                return
            lane_released = True
            self._worker_lane_gate.release(lane)

        async def _run_review_worker():
            try:
                return await w.run()
            finally:
                _release_lane_once()

        try:
            t = asyncio.create_task(_run_review_worker(), name=f"review-{engine}")
        except BaseException:
            _release_lane_once()
            raise
        tasks[t] = engine
        task_solvers[t] = w
        self._active_review_tasks.add(t)

        def _review_done(done_task) -> None:
            self._active_review_tasks.discard(done_task)
            _release_lane_once()

        t.add_done_callback(_review_done)
        self._review_workers_spawned += 1
        self._last_review_seq = seq
        self._completed_workers_since_review = 0
        self._last_candidate_review_count = self._candidate_fact_count()
        await emit_bb("review_started", trigger=trigger, worker=w.solver_id,
                      engine=str(engine), directive=directive[:300])
        await emit_bb("worker_spawned", worker=w.solver_id,
                      phase="review", worker_role="review")
        return True

    def _queue_unverified_flag_review(self) -> bool:
        """Queue Reviewer when workers claim flags that failed provenance.

        The hard gate already refused these claims; Reviewer only audits whether the
        value looks hallucinated vs worth a verifier-reproduction intent.
        """
        if self.shared_graph is None:
            return False
        if not self.review_policy.get("enabled", True):
            return False
        if not self.review_policy.get("on_unverified_flag", True):
            return False
        try:
            events = self.shared_graph.events_since(
                self._last_unverified_flag_review_seq,
                kinds=["flag_unverified"],
            )
        except Exception:
            return False
        claims = []
        max_seq = self._last_unverified_flag_review_seq
        seen: set[str] = set()
        for ev in events:
            seq = int(ev.get("seq") or 0)
            max_seq = max(max_seq, seq)
            payload = dict(ev.get("payload") or {})
            flag = str(payload.get("flag") or "").strip()
            if not flag or flag in seen:
                continue
            seen.add(flag)
            claims.append({
                "seq": seq,
                "flag": flag,
                "actor": str(ev.get("actor") or ""),
                "reason": str(payload.get("reason") or ""),
            })
        threshold = int(self.review_policy.get("unverified_flag_threshold") or 1)
        threshold = max(1, threshold)
        if len(claims) < threshold:
            return False
        self._last_unverified_flag_review_seq = max_seq
        lines = []
        for c in claims[:8]:
            lines.append(
                f"#{c['seq']} {c['flag']} by {c['actor']}: {c['reason'][:160]}"
            )
        self._queue_review_request(
            trigger="unverified_flag",
            directive=(
                "Audit unverified FOUND_FLAG claims that failed the hard provenance gate. "
                "Decide hallucination vs possible real-but-missing-output; emit FLAG_AUDIT "
                "for each claim. If possibly real, propose a verifier NEXT_INTENT that must "
                "reproduce the flag from real target/tool output. Never accept the flag.\n"
                + "\n".join(lines)
            ),
        )
        return True

    async def _drain_review_proposals(self, *, emit_bb, fruitless_workers: int = 0) -> int:
        if self.shared_graph is None:
            return 0
        try:
            events = self.shared_graph.events()
        except Exception:
            return 0
        proposals = [
            e for e in events
            if e.get("kind") == "review_proposal"
            and int(e.get("seq") or 0) > self._last_review_proposal_seq
        ]
        if not proposals:
            return 0
        applied = 0
        # run-75377: a single review cycle could emit dozens of FACT_CHALLENGE /
        # NEXT_INTENT, flooding the backlog with new (mostly verify) intents that then
        # starved solving. Cap the per-cycle fan-out of intent-creating markers; the
        # rest of the cycle only records REVIEW_FINDING. Eliminate-only markers
        # (FACT_MERGE/SUPERSEDE/REJECT, REVIEW_FINDING) are NOT counted — they shrink
        # backlog, not grow it. Counter is local so it resets every drain cycle.
        # A configured 0 genuinely disables challenge fan-out for the cycle (0 >= 0 is
        # immediately true, so every challenge-creating marker is recorded-only); don't
        # collapse it to the default. _clean_review_policy already supplies 8 when the
        # key is absent, so this get() only falls back for a non-dict review_policy.
        raw_budget = self.review_policy.get("max_challenges_per_cycle", 8)
        try:
            challenge_budget = max(0, int(raw_budget))
        except (TypeError, ValueError):
            challenge_budget = 8
        fanout_used = 0
        for ev in proposals:
            seq = int(ev.get("seq") or 0)
            self._last_review_proposal_seq = max(self._last_review_proposal_seq, seq)
            p = dict(ev.get("payload") or {})
            marker = str(p.get("marker") or "").upper()
            payload = dict(p.get("payload") or {})
            tier = str(p.get("tier") or "tier1")
            accepted = False
            reason = ""
            applied_seq: Optional[int] = None
            try:
                if tier == "tier2" and marker == "ROUTE_SUPPRESS":
                    route = str(payload.get("route_hash") or "")
                    failures = 0
                    try:
                        failures = int(self.shared_graph.genuine_failures_for_route(route))  # type: ignore[attr-defined]
                    except Exception:
                        failures = 0
                    confidence = float(payload.get("confidence", 1.0) or 1.0)
                    accepted = failures >= 3 and confidence >= 0.80
                    reason = f"failures={failures}, confidence={confidence:.2f}"
                    if accepted:
                        info = self.shared_graph.suppress_route(
                            actor="coordinator",
                            route_hash=route,
                            label=str(payload.get("label") or ""),
                            reason=str(payload.get("reason") or ""),
                            until=str(payload.get("until") or "new_evidence"),
                            matching_intents=[
                                str(x) for x in payload.get("matching_intents", []) if x
                            ],
                        )
                        applied_seq = int(info.get("seq") or 0) or None
                        await emit_bb("route_suppressed", **info,
                                      label=str(payload.get("label") or ""),
                                      reason=str(payload.get("reason") or ""),
                                      proposal_seq=seq)
                elif tier == "tier2" and marker == "LANE_LOCK":
                    lane = str(payload.get("lane_key") or "")
                    owner = str(payload.get("owner_worker") or payload.get("worker") or "coordinator")
                    accepted = bool(lane) and not self.shared_graph.is_lane_held_by_other(  # type: ignore[attr-defined]
                        lane, owner)
                    reason = "lane available" if accepted else "lane already held"
                    if accepted:
                        info = self.shared_graph.lock_lane(  # type: ignore[attr-defined]
                            actor="coordinator",
                            lane_key=lane,
                            risk_class=str(payload.get("risk_class") or ""),
                            owner_worker=owner,
                            owner_intent=str(payload.get("owner_intent") or ""),
                        )
                        accepted = bool(info.get("acquired"))
                        reason = "lane locked" if accepted else "lane already held"
                        if accepted:
                            applied_seq = int(info.get("seq") or 0) or None
                            directive_seq = self.shared_graph.add_coordinator_directive(
                                actor="coordinator",
                                action="lane_lock",
                                directive=(
                                    f"lane {info.get('lane_key')} is exclusively held by {owner}; "
                                    "do not start destructive/exclusive work on that resource."
                                ),
                                priority="high",
                            )
                            await emit_bb("lane_locked", **info,
                                          proposal_seq=seq,
                                          directive_seq=directive_seq)
                elif tier == "tier2" and marker == "LANE_UNLOCK":
                    lane = str(payload.get("lane_key") or "")
                    accepted = bool(lane)
                    reason = "lane released" if accepted else "empty lane_key"
                    if accepted:
                        info = self.shared_graph.release_lane(  # type: ignore[attr-defined]
                            actor="coordinator", lane_key=lane,
                            by_worker=str(payload.get("owner_worker") or ""),
                        )
                        applied_seq = int(info.get("seq") or 0) or None
                        await emit_bb("lane_released", **info, proposal_seq=seq)
                        await self._consume_lane_release(info, emit_bb=emit_bb)
                elif tier == "tier2" and marker == "COORDINATOR_DIRECTIVE":
                    action = str(payload.get("action") or "").strip() or "note"
                    accepted = (
                        action == "rebootstrap"
                        and self.barren_limit > 0
                        and fruitless_workers >= self.barren_limit
                    )
                    reason = (
                        f"fruitless_workers={fruitless_workers}, "
                        f"barren_limit={self.barren_limit}"
                    )
                    if accepted:
                        applied_seq = self.shared_graph.add_coordinator_directive(
                            actor="coordinator",
                            action=action,
                            directive=str(payload.get("directive") or ""),
                            priority=str(payload.get("priority") or "normal"),
                            route_hash=str(payload.get("route_hash") or ""),
                        )
                        await emit_bb("coordinator_directive", seq=applied_seq,
                                      proposal_seq=seq, **payload)
                else:
                    accepted = True
                    if marker == "REVIEW_FINDING":
                        applied_seq = self.shared_graph.add_review_finding(
                            actor="coordinator",
                            kind=str(payload.get("kind") or "no_action"),
                            severity=str(payload.get("severity") or "info"),
                            summary=str(payload.get("summary") or ""),
                            evidence_seqs=[
                                int(x) for x in payload.get("evidence_seqs", [])
                                if isinstance(x, int)
                            ],
                            intent_ids=[str(x) for x in payload.get("intent_ids", []) if x],
                            route_hash=str(payload.get("route_hash") or ""),
                            branch_id=str(payload.get("branch_id") or ""),
                            recommended_actions=[
                                str(x) for x in payload.get("recommended_actions", []) if x
                            ],
                        )
                        await emit_bb("review_finding", seq=applied_seq,
                                      finding_kind=str(payload.get("kind") or "no_action"),
                                      severity=str(payload.get("severity") or "info"),
                                      summary=str(payload.get("summary") or ""),
                                      route_hash=str(payload.get("route_hash") or ""),
                                      branch_id=str(payload.get("branch_id") or ""),
                                      proposal_seq=seq)
                    elif marker == "FLAG_AUDIT":
                        flag = str(payload.get("flag") or "").strip()
                        verdict = str(payload.get("verdict") or "insufficient_context").strip()
                        reason_text = str(payload.get("reason") or payload.get("summary") or "")
                        action = str(payload.get("recommended_action") or "")
                        await emit_bb("flag_audit", seq=seq, flag=flag,
                                      verdict=verdict, reason=reason_text,
                                      recommended_action=action,
                                      proposal_seq=seq)
                    elif marker == "FACT_CHALLENGE":
                        if fanout_used >= challenge_budget:
                            accepted = False
                            reason = (f"fan-out budget exhausted "
                                      f"({challenge_budget}/cycle)")
                            await emit_bb("review_fanout_skipped", marker=marker,
                                          proposal_seq=seq, budget=challenge_budget)
                        else:
                            info = self.shared_graph.challenge_fact(
                                actor="coordinator",
                                fact_seq=int(payload.get("fact_seq")),
                                reason=str(payload.get("reason") or ""),
                                verification_goal=str(payload.get("verification_goal") or ""),
                            )
                            applied_seq = int(info.get("seq") or 0) or None
                            fanout_used += 1
                            await emit_bb("fact_challenged", **info, proposal_seq=seq)
                    elif marker == "FACT_REVALIDATION":
                        applied_seq = self.shared_graph.revalidate_fact(
                            actor="coordinator",
                            fact_seq=int(payload.get("fact_seq")),
                            reason=str(payload.get("reason") or ""),
                        )
                        await emit_bb("fact_revalidated", seq=applied_seq,
                                      fact_seq=int(payload.get("fact_seq")),
                                      reason=str(payload.get("reason") or ""),
                                      proposal_seq=seq)
                    elif marker == "FACT_REJECT":
                        # A: review proved a candidate false → retire it. Only the
                        # candidate view dims; the originating event stays (audit).
                        # Reviewer can never set solved / kill workers, so this is
                        # safe to auto-adopt alongside challenge/revalidate.
                        fseq = int(payload.get("fact_seq"))
                        applied_seq = self.shared_graph.reject_fact(
                            actor="coordinator", fact_seq=fseq,
                            reason=str(payload.get("reason") or ""))
                        await emit_bb("fact_rejected", seq=applied_seq,
                                      fact_seq=fseq,
                                      reason=str(payload.get("reason") or ""),
                                      proposal_seq=seq)
                    elif marker == "FACT_MERGE":
                        # A: fold a duplicate finding into its canonical fact.
                        from_seq = int(payload.get("fact_seq")
                                       if payload.get("fact_seq") is not None
                                       else payload.get("from_fact_seq"))
                        to_seq = int(payload.get("to_fact_seq") or 0)
                        applied_seq = self.shared_graph.merge_fact(
                            actor="coordinator", from_fact_seq=from_seq,
                            to_fact_seq=to_seq,
                            reason=str(payload.get("reason") or ""))
                        if applied_seq is not None and applied_seq < 0:
                            accepted = False
                            reason = "merge into self / invalid to_fact_seq"
                            applied_seq = None
                        else:
                            await emit_bb("fact_merged", seq=applied_seq,
                                          from_fact_seq=from_seq, to_fact_seq=to_seq,
                                          reason=str(payload.get("reason") or ""),
                                          proposal_seq=seq)
                    elif marker == "FACT_SUPERSEDE":
                        # A: a newer fact replaces this one → retire the old.
                        fseq = int(payload.get("fact_seq"))
                        by_seq = payload.get("by_fact_seq") or payload.get("to_fact_seq")
                        applied_seq = self.shared_graph.supersede_fact(
                            actor="coordinator", fact_seq=fseq,
                            reason=str(payload.get("reason") or ""),
                            by_fact_seq=int(by_seq) if by_seq is not None else None)
                        await emit_bb("fact_superseded", seq=applied_seq,
                                      fact_seq=fseq,
                                      reason=str(payload.get("reason") or ""),
                                      proposal_seq=seq)
                    elif marker == "ROUTE_REOPEN":
                        info = self.shared_graph.reopen_route(
                            actor="coordinator",
                            route_hash=str(payload.get("route_hash") or ""),
                            reason=str(payload.get("reason") or ""),
                            intent_goal=str(payload.get("intent_goal") or payload.get("goal") or ""),
                        )
                        applied_seq = int(info.get("seq") or 0) or None
                        await emit_bb("route_reopened", **info,
                                      reason=str(payload.get("reason") or ""),
                                      proposal_seq=seq)
                    elif marker == "BRANCH_SPLIT":
                        info = self.shared_graph.split_branch(
                            actor="coordinator",
                            title=str(payload.get("title") or ""),
                            branches=list(payload.get("branches") or []),
                        )
                        applied_seq = int(info.get("seq") or 0) or None
                        await emit_bb("branch_split", **info,
                                      title=str(payload.get("title") or ""),
                                      proposal_seq=seq)
                    elif marker == "BRANCH_RESOLVE":
                        info = self.shared_graph.resolve_branch(  # type: ignore[attr-defined]
                            actor="coordinator",
                            branch_id=str(payload.get("branch_id") or ""),
                            reason=str(payload.get("reason") or ""),
                            status=str(payload.get("status") or "resolved"),
                        )
                        applied_seq = int(info.get("seq") or 0) or None
                        await emit_bb("branch_resolved", **info, proposal_seq=seq)
                    elif marker == "NEXT_INTENT":
                        goal = str(payload.get("goal") or "").strip()
                        if not goal:
                            accepted = False
                            reason = "empty goal"
                        elif fanout_used >= challenge_budget:
                            accepted = False
                            reason = (f"fan-out budget exhausted "
                                      f"({challenge_budget}/cycle)")
                            await emit_bb("review_fanout_skipped", marker=marker,
                                          proposal_seq=seq, budget=challenge_budget)
                        else:
                            iid = str(payload.get("id") or payload.get("intent_id") or "")
                            if not iid:
                                iid = "I-review-" + hashlib.sha1(
                                    goal.encode("utf-8", "ignore")
                                ).hexdigest()[:8]
                            wc = str(payload.get("worker_class") or "code")
                            lane_key = str(payload.get("lane_key") or "").strip()
                            risk_class = str(payload.get("risk_class") or "").strip()
                            if not lane_key:
                                lane_hint = self._lane_hint_from_text(
                                    goal, require_control_hint=True)
                                lane_key = str(lane_hint.get("lane_key") or "")
                                if lane_key and not risk_class:
                                    risk_class = str(lane_hint.get("risk_class") or "")
                            applied_seq = self.shared_graph.propose_intent(
                                actor="coordinator", intent_id=iid, goal=goal,
                                payload={
                                    "worker_class": wc,
                                    "route_hash": str(payload.get("route_hash") or ""),
                                    "branch_id": str(payload.get("branch_id") or ""),
                                    "lane_key": lane_key,
                                    "risk_class": risk_class,
                                    "rationale": str(payload.get("rationale") or "review proposed"),
                                    "depends_on": [
                                        str(x) for x in payload.get("depends_on", []) if x
                                    ],
                                },
                                from_fact_seqs=[
                                    int(x) for x in payload.get("from", [])
                                    if isinstance(x, int)
                                ] or None,
                            )
                            fanout_used += 1
                            await emit_bb("intent_proposed", intent_id=iid,
                                          goal=goal, worker_class=wc,
                                          route_hash=str(payload.get("route_hash") or ""),
                                          branch_id=str(payload.get("branch_id") or ""),
                                          lane_key=lane_key,
                                          risk_class=risk_class,
                                          proposal_seq=seq)
                    else:
                        accepted = False
                        reason = f"unsupported marker {marker}"
                decision = "accepted" if accepted else "deferred"
                self.shared_graph.decide_review_proposal(  # type: ignore[attr-defined]
                    actor="coordinator", proposal_seq=seq, decision=decision,
                    reason=reason, applied_seq=applied_seq)
                await emit_bb("review_proposal_decision", proposal_seq=seq,
                              marker=marker, decision=decision, reason=reason,
                              applied_seq=applied_seq)
                if accepted:
                    applied += 1
            except Exception as exc:  # noqa: BLE001
                try:
                    self.shared_graph.decide_review_proposal(  # type: ignore[attr-defined]
                        actor="coordinator", proposal_seq=seq,
                        decision="rejected", reason=str(exc)[:500])
                    await emit_bb("review_proposal_decision", proposal_seq=seq,
                                  marker=marker, decision="rejected",
                                  reason=str(exc)[:500])
                except Exception:
                    pass
        return applied
