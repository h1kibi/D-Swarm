"""Model gateway: OpenAI-compatible reverse proxy with per-worker task tokens (route A, P3).

Why: worker containers must NEVER see the real upstream API key. The worker's pi
CLI is pointed at a gateway provider (baseUrl=http://host.docker.internal:9101/v1,
baked into the worker image) and authenticates with `Authorization: Bearer
<task-token>` 鈥?an independent token is issued per worker, injected as DEEPSEEK_API_KEY into
that worker env, and revoked at worker completion (with run teardown as a safety net). The gateway validates the token, swaps in
the REAL upstream key (from the credential account store, falling back to the host
env), forwards the OpenAI-compatible request (streaming SSE transparent), parses the
usage from the stream, and records it per run.

This mirrors BTFly's modelgateway (task token + reverse proxy + usage capture),
implemented as a small threading HTTP server so it can live in the daemon process
alongside the ControlReceiver 鈥?no extra service to deploy. One gateway per process,
lazily started on first container-mode run; tokens carry immutable WorkerClaims
and are revoked independently.

Endpoints:
  POST /v1/chat/completions   (OpenAI-compatible, streaming or not)
  GET  /health               (liveness for container probes)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

from dswarm.core.events import Event, EventType
from dswarm.core.usage_journal import (
    AccountingUnavailable,
    UsageCall,
    UsageJournal,
    UsageRecord,
)

log = logging.getLogger("modelgateway")

DEFAULT_GATEWAY_PORT = 9101

# env knobs
_GATEWAY_PORT = int(os.environ.get("DSWARM_MODEL_GATEWAY_PORT", str(DEFAULT_GATEWAY_PORT)) or DEFAULT_GATEWAY_PORT)
_GATEWAY_BIND = os.environ.get("DSWARM_MODEL_GATEWAY_BIND", "127.0.0.1").strip() or "127.0.0.1"
# which upstream the gateway forwards to. Default: the deepseek endpoint pi's
# provider config points at (api.deepseek.com, no /v1 鈥?pi appends it).
_UPSTREAM_BASE = os.environ.get(
    "DSWARM_MODEL_GATEWAY_UPSTREAM", "https://api.deepseek.com").strip().rstrip("/")

_UPSTREAM_PATH = "/chat/completions"


_TOKEN_SCOPES = frozenset({"worker", "review", "recon", "btw"})


@dataclass(frozen=True)
class WorkerClaims:
    """Immutable identity captured when a worker gateway token is issued."""

    run_id: str
    challenge_id: str | None
    worker_instance_id: str
    solver_id: str | None
    profile_id: str
    configured_account_id: str | None
    token_scope: str

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("run_id is required")
        if not self.worker_instance_id:
            raise ValueError("worker_instance_id is required")
        if not self.profile_id:
            raise ValueError("profile_id is required")
        if self.configured_account_id is not None and not self.configured_account_id.strip():
            raise ValueError("configured_account_id must be None or non-empty")
        if self.token_scope not in _TOKEN_SCOPES:
            raise ValueError(f"invalid token_scope: {self.token_scope}")


class TokenCapError(RuntimeError):
    """The hard active-token cap rejected a new worker token without eviction."""

    def __init__(self, *, active_tokens: int, max_active_tokens: int) -> None:
        super().__init__(
            f"model gateway token cap reached ({active_tokens}/{max_active_tokens})"
        )
        self.alert_payload = {
            "level": "error",
            "reason": "token_cap",
            "active_tokens": active_tokens,
            "max_active_tokens": max_active_tokens,
        }


def _real_api_key(account_root: Optional[str], run_id: str) -> str:
    """The REAL upstream key: the run's pi-main credential account, else the host env."""
    if account_root:
        p = Path(account_root) / "pi-main" / "API_KEY"
        try:
            val = p.read_text(encoding="utf-8").strip()
            if val:
                return val
        except OSError:
            pass
    for var in ("DEEPSEEK_API_KEY",):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return ""


class GatewayUsageBridge:
    """Synchronous acknowledgement bridge from gateway threads to an asyncio bus."""

    def __init__(
        self, *, loop: Any = None, bus: Any = None, timeout: float = 10.0,
        max_inflight: int = 128,
    ) -> None:
        if timeout <= 0:
            raise ValueError("bridge timeout must be positive")
        if max_inflight <= 0:
            raise ValueError("bridge max_inflight must be positive")
        self.loop = loop
        self.bus = bus
        self.timeout = float(timeout)
        self._slots = threading.BoundedSemaphore(max_inflight)
        self._route_lock = threading.Lock()
        self._routes: dict[str, tuple[Any, Any]] = {}
        # Explicitly detached runs must fail closed; late callbacks must not
        # fall back to the process-wide default bus.
        self._unregistered_runs: set[str] = set()

    def register(self, run_id: str, *, bus: Any, loop: Any = None) -> None:
        """Register the owner bus/loop for one run without replacing other runs."""
        if not run_id:
            raise ValueError("run_id is required")
        if bus is None:
            raise ValueError("bus is required")
        target_loop = loop if loop is not None else self.loop
        with self._route_lock:
            key = str(run_id)
            self._routes[key] = (target_loop, bus)
            self._unregistered_runs.discard(key)

    def unregister(self, run_id: str) -> None:
        with self._route_lock:
            key = str(run_id)
            self._routes.pop(key, None)
            self._unregistered_runs.add(key)

    def _target_for(self, run_id: str | None) -> tuple[Any, Any]:
        with self._route_lock:
            key = str(run_id) if run_id else None
            target = self._routes.get(key) if key else None
            detached = key in self._unregistered_runs if key else False
        if target is not None:
            return target
        if detached:
            return None, None
        return self.loop, self.bus

    def publish(self, event: Event) -> Event:
        """Publish one critical event and wait until the owning loop acknowledges it."""
        loop, bus = self._target_for(getattr(event, "run_id", None))
        if bus is None:
            raise RuntimeError("gateway event route is unavailable")
        if loop is None or loop.is_closed() or not loop.is_running():
            raise RuntimeError("gateway event loop is unavailable")
        if not self._slots.acquire(timeout=self.timeout):
            raise TimeoutError("gateway usage bridge backpressure timeout")
        future = None
        try:
            future = asyncio.run_coroutine_threadsafe(bus.emit_checked(event), loop)
            return future.result(timeout=self.timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            if future is not None:
                future.cancel()
            raise TimeoutError("gateway usage bridge acknowledgement timed out") from exc
        finally:
            self._slots.release()

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ctf-swarm-modelgateway/0.1"
    # silence default stderr logging
    def log_message(self, *a):  # noqa: D401
        pass

    @property
    def _gw(self) -> "ModelGateway":
        return self.server.gateway  # type: ignore[attr-defined]

    # 鈹€鈹€ helpers 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    def _read_body(self, limit: int = 16 * 1024 * 1024) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > limit:
            return b""
        return self.rfile.read(length)

    def _token(self) -> str:
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return ""

    def _write(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # 鈹€鈹€ routes 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") == "/health":
            self._write(200, b'{"ok":true}')
            return
        self._write(404, b'{"error":"not found"}')

    def do_POST(self):  # noqa: N802
        if not self.path.rstrip("/").endswith(_UPSTREAM_PATH):
            self._write(404, b'{"error":"not found"}')
            return
        token = self._token()
        claims = self._gw.claims_for_token(token)
        run_id = claims.run_id if claims is not None else None
        log.info("gateway POST %s run=%s token=%s...%s", self.path,
                 run_id, token[:4] if token else "none", token[-4:] if token else "")
        if claims is None:
            self._write(401, b'{"error":{"message":"invalid or revoked task token","type":"authentication_error"}}')
            return
        body = self._read_body()
        if not body:
            self._write(400, b'{"error":{"message":"empty request body"}}')
            return
        self._gw.proxy(run_id, body, self, claims=claims)

    # 鈹€鈹€ keep-alive / streaming plumbing 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    def _chunk(self, data: bytes) -> None:
        try:
            self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _end_chunked(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class ModelGateway:
    """Process-wide OpenAI-compatible gateway with independent worker tokens."""

    def __init__(
        self, host: Optional[str] = None, port: int = _GATEWAY_PORT,
        *, max_active_tokens: int = 1024,
    ):
        self.host = host if host is not None else _GATEWAY_BIND
        self.port = port
        if max_active_tokens <= 0:
            raise ValueError("max_active_tokens must be positive")
        self.max_active_tokens = int(max_active_tokens)
        self._tokens: dict[str, WorkerClaims] = {}
        self._run_tokens: dict[str, set[str]] = {}
        self._lock = threading.Lock()
        self._srv: Optional[ThreadingHTTPServer] = None
        self._started = False
        self._usage_log: Optional[Path] = None
        self.account_root: Optional[str] = None   # credential account store root (set by the caller)
        self.sessions_root: Optional[str] = None  # per-run usage ledgers land here
        self.usage_bridge: GatewayUsageBridge | Any | None = None

    # 鈹€鈹€ lifecycle 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    @classmethod
    def instance(cls) -> "ModelGateway":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = ModelGateway()
                cls._instance.start()
            return cls._instance

    def start(self) -> None:
        if self._started:
            return
        srv = ThreadingHTTPServer((self.host, self.port), _Handler)
        srv.gateway = self  # type: ignore[attr-defined]
        self._srv = srv
        self._started = True
        log.info("model gateway listening on %s:%d", self.host, self.port)
        threading.Thread(target=srv.serve_forever, name="model-gateway", daemon=True).start()

    def stop(self) -> None:
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None
        self._started = False

    # 鈹€鈹€ token management 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    def issue_worker(self, claims: WorkerClaims) -> str:
        """Issue one independent token; never revoke or evict another worker."""
        with self._lock:
            active = len(self._tokens)
            if active >= self.max_active_tokens:
                raise TokenCapError(
                    active_tokens=active, max_active_tokens=self.max_active_tokens
                )
            token = secrets.token_hex(32)
            while token in self._tokens:
                token = secrets.token_hex(32)
            self._tokens[token] = claims
            self._run_tokens.setdefault(claims.run_id, set()).add(token)
            return token

    def claims_for_token(self, token: str) -> Optional[WorkerClaims]:
        """Authenticate a token and return its immutable entrance snapshot."""
        with self._lock:
            for known, claims in self._tokens.items():
                if hmac.compare_digest(known, token):
                    return claims
        return None

    def revoke_token(self, token: str) -> None:
        with self._lock:
            claims = self._tokens.pop(token, None)
            if claims is None:
                return
            tokens = self._run_tokens.get(claims.run_id)
            if tokens is not None:
                tokens.discard(token)
                if not tokens:
                    self._run_tokens.pop(claims.run_id, None)

    def revoke_worker(self, worker_instance_id: str) -> None:
        with self._lock:
            doomed = [
                token for token, claims in self._tokens.items()
                if claims.worker_instance_id == worker_instance_id
            ]
            for token in doomed:
                claims = self._tokens.pop(token)
                tokens = self._run_tokens.get(claims.run_id)
                if tokens is not None:
                    tokens.discard(token)
                    if not tokens:
                        self._run_tokens.pop(claims.run_id, None)

    def revoke_run(self, run_id: str) -> None:
        with self._lock:
            for token in self._run_tokens.pop(run_id, set()):
                self._tokens.pop(token, None)

    def run_for_token(self, token: str) -> Optional[str]:
        claims = self.claims_for_token(token)
        return claims.run_id if claims is not None else None

    # Compatibility for callers/tests while Phase 3 migrates all runtime paths.
    def issue(self, run_id: str) -> str:
        self.revoke_run(run_id)
        return self.issue_worker(WorkerClaims(
            run_id=run_id, challenge_id=None,
            worker_instance_id=f"legacy::{run_id}", solver_id=None,
            profile_id="legacy", configured_account_id=None, token_scope="worker",
        ))

    def revoke(self, run_id: str) -> None:
        self.revoke_run(run_id)

    def token_for_run(self, run_id: str) -> Optional[str]:
        with self._lock:
            tokens = self._run_tokens.get(run_id)
            return next(iter(tokens)) if tokens else None

    # 鈹€鈹€ usage / proxy 鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€鈹€
    def configure_usage_bridge(
        self, *, bus: Any, loop: Any = None, run_id: str | None = None,
    ) -> None:
        """Attach the asyncio owner used for checked canonical usage events.

        The HTTP gateway is process-wide while runs own separate EventBus instances,
        so a bridge keeps a run-id route table instead of letting the last run replace
        the previous run's bus.
        """
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
        bridge = self.usage_bridge
        if not isinstance(bridge, GatewayUsageBridge):
            bridge = GatewayUsageBridge(loop=loop, bus=bus)
            self.usage_bridge = bridge
        elif run_id is None:
            bridge.loop = loop
            bridge.bus = bus
        if run_id is not None:
            bridge.register(run_id, bus=bus, loop=loop)

    def unregister_usage_bridge(self, run_id: str) -> None:
        bridge = self.usage_bridge
        if isinstance(bridge, GatewayUsageBridge):
            bridge.unregister(run_id)

    def _journal_for(self, run_id: str) -> UsageJournal:
        if not self.sessions_root:
            raise AccountingUnavailable("gateway sessions root is not configured")
        return UsageJournal(Path(self.sessions_root) / f"{run_id}-usage-journal.jsonl")

    @staticmethod
    def _usage_call(claims: WorkerClaims) -> UsageCall:
        return UsageCall(
            provider_call_id=uuid.uuid4().hex,
            producer="gateway",
            run_id=claims.run_id,
            challenge_id=claims.challenge_id,
            worker_instance_id=claims.worker_instance_id,
            solver_id=claims.solver_id,
            profile_id=claims.profile_id,
            configured_account_id=claims.configured_account_id,
            billing_account_id="pi-main",
        )

    @staticmethod
    def _extract_usage(payload: str) -> dict[str, Any]:
        usage: dict[str, Any] = {}
        candidates: list[str] = [payload]
        candidates.extend(
            line.strip()[5:].strip() for line in payload.splitlines()
            if line.strip().startswith("data:")
        )
        for raw in candidates:
            if not raw or raw == "[DONE]":
                continue
            try:
                value = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and isinstance(value.get("usage"), dict):
                usage = dict(value["usage"])
        return usage

    @staticmethod
    def _normalized_usage(usage: dict[str, Any]) -> dict[str, Any]:
        if not usage:
            return {}
        normalized: dict[str, Any] = {}
        input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
        if input_tokens is not None:
            normalized["input_tokens"] = int(input_tokens)
        if output_tokens is not None:
            normalized["output_tokens"] = int(output_tokens)
        if usage.get("usd") is not None:
            normalized["usd"] = float(usage["usd"])
        return normalized if normalized else {}

    def _finish_gateway_call(
        self, call: UsageCall, *, outcome: str, usage: dict[str, Any],
        legacy_payload: str = "",
    ) -> UsageRecord:
        normalized = self._normalized_usage(usage)
        record = UsageRecord.from_call(
            call,
            call_outcome=outcome,
            usage_status="measured" if normalized else "unknown",
            input_tokens=normalized.get("input_tokens"),
            output_tokens=normalized.get("output_tokens"),
            usd=normalized.get("usd"),
        )
        self._journal_for(call.run_id).append_finished(record)
        self._record_usage(
            call.run_id, legacy_payload, usage=usage,
            provider_call_id=call.provider_call_id,
        )
        bridge = self.usage_bridge
        if bridge is not None:
            event = Event(
                event_type=EventType.USAGE_RECORDED,
                run_id=record.run_id,
                challenge_id=record.challenge_id,
                solver_id=record.solver_id,
                payload=record.__dict__.copy(),
            )
            try:
                bridge.publish(event)
            except Exception as exc:  # terminal journal remains recovery truth
                log.error("gateway canonical usage publish failed: %s", exc)
        return record

    def proxy(
        self, run_id: str, body: bytes, handler: _Handler, *,
        claims: WorkerClaims | None = None,
    ) -> None:
        real_key = _real_api_key(self.account_root, run_id)
        if not real_key:
            self._write_json(handler, 502, {"error": {"message": "gateway has no upstream key configured"}})
            return
        try:
            req = json.loads(body)
        except json.JSONDecodeError:
            self._write_json(handler, 400, {"error": {"message": "invalid JSON body"}})
            return
        if not isinstance(req, dict):
            self._write_json(handler, 400, {"error": {"message": "request body must be an object"}})
            return
        if claims is None:
            claims = WorkerClaims(
                run_id=run_id, challenge_id=None,
                worker_instance_id=f"legacy::{run_id}", solver_id=None,
                profile_id="legacy", configured_account_id=None, token_scope="worker",
            )
        stream = bool(req.get("stream", False))
        try:
            call = self._usage_call(claims)
            self._journal_for(run_id).append_started(call)
        except AccountingUnavailable as exc:
            log.error("gateway usage preflight failed for run %s: %s", run_id, exc)
            self._write_json(handler, exc.status_code, {"error": {"code": exc.code, "message": str(exc)}})
            return
        except Exception as exc:
            log.error("gateway usage preflight failed for run %s: %s", run_id, exc)
            self._write_json(handler, 503, {"error": {"code": "accounting_unavailable", "message": str(exc)}})
            return

        try:
            msgs = req.get("messages") or []
            lens = ", ".join(
                f"{str(m.get('role'))}={len(str(m.get('content') or ''))}" for m in msgs
            )
            log.info("gateway req run=%s stream=%s model=%s msgs=%d [%s] body_bytes=%d",
                     run_id, stream, req.get("model"), len(msgs), lens or "-", len(body))
        except Exception:
            pass
        upstream_url = f"{_UPSTREAM_BASE}{_UPSTREAM_PATH}"
        headers = {
            "Authorization": f"Bearer {real_key}",
            "Content-Type": "application/json",
        }
        t0 = time.time()
        try:
            with httpx.stream(
                "POST", upstream_url, json=req, headers=headers,
                timeout=httpx.Timeout(300.0, connect=20.0),
                # The gateway is the controlled upstream funnel: route DIRECTLY to
                # the provider, never through the operator's ambient HTTP(S)_PROXY.
                # Routing through a local proxy turns connection-refused into a
                # proxy 502 with an empty body, which both corrupts the terminal
                # usage semantics and made the transport-error path untestable
                # (test_gateway_transport_error_gets_terminal_unknown).
                trust_env=False,
            ) as resp:
                log.info("gateway upstream status=%s ttfb=%.2fs (run %s)",
                         resp.status_code, time.time() - t0, run_id)
                if stream:
                    self._proxy_stream(run_id, handler, resp, req, call=call)
                else:
                    self._proxy_json(run_id, handler, resp, call=call)
        except Exception as exc:
            outcome = "timeout" if isinstance(exc, (TimeoutError, httpx.TimeoutException)) else "transport_error"
            try:
                self._finish_gateway_call(call, outcome=outcome, usage={})
            except Exception as finish_exc:
                log.error("gateway terminal usage write failed: %s", finish_exc)
            log.warning("gateway upstream error (run %s): %s", run_id, exc)
            self._write_json(handler, 502, {"error": {"message": f"upstream error: {exc}"}})

    def _proxy_json(self, run_id: str, handler: _Handler, resp, *, call: UsageCall) -> None:
        data = resp.read()
        payload = data.decode("utf-8", "replace")
        usage = self._extract_usage(payload)
        outcome = "provider_error" if resp.status_code >= 400 else "succeeded"
        self._finish_gateway_call(call, outcome=outcome, usage=usage, legacy_payload=payload)
        handler.send_response(resp.status_code)
        for k, v in resp.headers.items():
            if k.lower() in ("content-type",):
                handler.send_header(k, v)
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def _proxy_stream(self, run_id: str, handler: _Handler, resp, req, *, call: UsageCall) -> None:
        handler.send_response(resp.status_code)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        usage_parts: list[str] = []
        n_chunks = 0
        n_chars = 0
        t0 = time.time()
        for line in resp.iter_lines():
            if line == "":
                handler._chunk(b"\n")
                continue
            is_done = False
            if line.startswith("data:"):
                data = line[5:].strip()
                if data == "[DONE]":
                    is_done = True
                elif data:
                    usage_parts.append(data)
            handler._chunk((line + "\n").encode("utf-8", "replace"))
            n_chunks += 1
            n_chars += len(line) + 1
            if is_done:
                break
        payload = "\n".join(usage_parts)
        outcome = "provider_error" if resp.status_code >= 400 else "succeeded"
        self._finish_gateway_call(
            call, outcome=outcome, usage=self._extract_usage(payload),
            legacy_payload=payload,
        )
        log.info("gateway stream done run=%s chunks=%d chars=%d elapsed=%.2fs",
                 call.run_id, n_chunks, n_chars, time.time() - t0)
        handler._end_chunked()

    def _record_usage(
        self, run_id: str, payload: str, *, usage: dict[str, Any] | None = None,
        provider_call_id: str | None = None, claims: WorkerClaims | None = None,
    ) -> None:
        try:
            usage = usage or self._extract_usage(payload)
            if not usage:
                return
            row: dict[str, Any] = {"ts": time.time(), "run_id": run_id, "usage": usage}
            if provider_call_id:
                row["provider_call_id"] = provider_call_id
            if claims is not None:
                row.update({
                    "challenge_id": claims.challenge_id,
                    "worker_instance_id": claims.worker_instance_id,
                    "solver_id": claims.solver_id,
                    "profile_id": claims.profile_id,
                    "configured_account_id": claims.configured_account_id,
                    "billing_account_id": "pi-main",
                })
            log_path = self._usage_log
            if log_path is None and self.sessions_root:
                log_path = Path(self.sessions_root) / f"{run_id}-gateway-usage.jsonl"
            if log_path is not None:
                Path(log_path).parent.mkdir(parents=True, exist_ok=True)
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
        except Exception:
            pass

    @staticmethod
    def _write_json(handler: _Handler, status: int, obj: dict) -> None:
        handler._write(status, json.dumps(obj).encode("utf-8"))


ModelGateway._instance: Optional["ModelGateway"] = None
ModelGateway._instance_lock = threading.Lock()


def gateway_usage_log(run_id: str, sessions_root: Path) -> Path:
    """The per-run usage ledger path (sessions_root/<run_id>-gateway-usage.jsonl)."""
    return Path(sessions_root) / f"{run_id}-gateway-usage.jsonl"
