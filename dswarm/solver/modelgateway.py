"""Model gateway: OpenAI-compatible reverse proxy with per-worker task tokens (route A, P3).

Why: worker containers must NEVER see the real upstream API key. The worker's pi
CLI is pointed at a gateway provider (baseUrl=http://host.docker.internal:9101/v1,
baked into the worker image) and authenticates with `Authorization: Bearer
<task-token>` 鈥?an independent token is issued per worker, injected as DEEPSEEK_API_KEY into
that worker env, and revoked at worker completion (with run teardown as a safety net). The gateway validates the token, swaps in
the REAL upstream key (from the credential account store, falling back to the host
env), forwards the OpenAI-compatible request (streaming SSE transparent), parses the
usage from the stream, and records it per run.

Workers always speak the OpenAI chat-completions dialect to this gateway. A task token
can nevertheless be bound to a native Anthropic Messages upstream, which the gateway
translates without ever projecting that upstream credential into the container.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

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

# bigmodel (GLM open platform) rejects reasoning_effort values outside
# low/high/max on always-thinking models (code 1210, "请使用 low、high 或 max").
# pi's openai-completions path sends its default effort ("medium") regardless of
# the model spec's thinkingLevelMap, so the gateway normalizes the dialect here
# — it is the single controlled upstream funnel.
_BIGMODEL_HOST = "open.bigmodel.cn"
_VALID_EFFORT_LEVELS = {"low", "high", "max"}


def normalize_upstream_request(req: dict, *, upstream_base: str = _UPSTREAM_BASE) -> dict:
    """Adapt OpenAI-style request fields to the configured upstream's dialect."""
    effort = req.get("reasoning_effort")
    if isinstance(effort, str):
        normalized = effort.strip().lower()
        if normalized in {"minimal", "low", "medium", "high", "max"}:
            req["reasoning_effort"] = normalized
    if _BIGMODEL_HOST in str(upstream_base or ""):
        effort = req.get("reasoning_effort")
        if isinstance(effort, str) and effort.strip().lower() not in _VALID_EFFORT_LEVELS:
            req["reasoning_effort"] = "low"
    return req


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


@dataclass(frozen=True)
class GatewayUpstreamBinding:
    """Private, immutable upstream transport captured with one task token.

    This object only lives in the host-side gateway token table. In particular it is
    never included in :class:`WorkerClaims`, usage events, HTTP responses, or a Run
    binding snapshot: ``api_key`` must never cross the host/container boundary.
    """

    base_url: str
    api_key: str = field(repr=False, compare=False)
    wire_api: str = "openai-chat"
    auth_mode: str = "bearer"
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer"
    provider_id: str = ""

    def __post_init__(self) -> None:
        base_url = str(self.base_url or "").strip().rstrip("/")
        wire_api = str(self.wire_api or "openai-chat").strip().lower()
        if wire_api in {"anthropic", "anthropic-messages", "messages"}:
            wire_api = "anthropic"
            if base_url.endswith("/messages"):
                base_url = base_url[:-len("/messages")]
        elif wire_api in {"", "auto", "openai", "openai-chat", "chat-completions"}:
            wire_api = "openai-chat"
        else:
            raise ValueError("unsupported gateway upstream protocol")
        if not base_url:
            raise ValueError("gateway upstream base_url is required")
        if not str(self.api_key or "").strip():
            raise ValueError("gateway upstream API key is required")
        auth_mode = str(self.auth_mode or "bearer").strip().lower()
        auth_header = str(self.auth_header or "Authorization").strip()
        auth_prefix = str(self.auth_prefix if self.auth_prefix is not None else "Bearer").strip()
        # Native Anthropic uses x-api-key by default. Explicit custom relay
        # settings still win, exactly as they do for the host-side LLMClient.
        if (
            wire_api == "anthropic"
            and auth_mode == "bearer"
            and auth_header == "Authorization"
            and auth_prefix == "Bearer"
        ):
            auth_mode, auth_header, auth_prefix = "x-api-key", "x-api-key", ""
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "wire_api", wire_api)
        object.__setattr__(self, "auth_mode", auth_mode)
        object.__setattr__(self, "auth_header", auth_header)
        object.__setattr__(self, "auth_prefix", auth_prefix)

    def headers(self) -> dict[str, str]:
        value = f"{self.auth_prefix} {self.api_key}".strip() if self.auth_prefix else self.api_key
        headers = {self.auth_header: value, "Content-Type": "application/json"}
        if self.wire_api == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        return headers


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


def resolve_upstream_key(
    explicit: Optional[str], account_root: Optional[str], run_id: str,
    environ: Optional[dict] = None,
) -> str:
    """The REAL upstream key, most-specific first: the caller-resolved key for
    the provider the run's workers are actually bound to (run_manager injects
    it per run), else the legacy pi-main credential account file, else the
    host env. Order matters — a stale pi-main file must not shadow the bound
    provider's key (that mismatch was every worker 401-ing at the upstream)."""
    val = (explicit or "").strip()
    if val:
        return val
    if account_root:
        p = Path(account_root) / "pi-main" / "API_KEY"
        try:
            val = p.read_text(encoding="utf-8").strip()
            if val:
                return val
        except OSError:
            pass
    env = environ if environ is not None else os.environ
    for var in ("DEEPSEEK_API_KEY",):
        val = str(env.get(var, "") or "").strip()
        if val:
            return val
    return ""


def _real_api_key(account_root: Optional[str], run_id: str) -> str:
    """Back-compat shim: the legacy pi-main-account → env resolution."""
    return resolve_upstream_key(None, account_root, run_id)


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
        log.info("gateway POST %s run=%s authenticated=%s", self.path, run_id, bool(claims))
        if claims is None:
            self._write(401, b'{"error":{"message":"invalid or revoked task token","type":"authentication_error"}}')
            return
        body = self._read_body()
        if not body:
            self._write(400, b'{"error":{"message":"empty request body"}}')
            return
        self._gw.proxy(
            run_id, body, self, claims=claims,
            upstream=self._gw.upstream_for_token(token),
        )

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

    # Class-level upstream-key resolver: installed at app startup WITHOUT
    # instantiating the gateway (the server must only come up when a run/probe
    # actually needs it). Re-read on every proxy request, so settings edits and
    # separate managers (startup tests) resolve bindings fresh.
    _class_upstream_key_resolver: Optional[Callable[[], str]] = None
    _class_upstream_binding_resolver: Optional[Callable[[str], GatewayUpstreamBinding | None]] = None

    @classmethod
    def set_upstream_key_resolver(cls, resolver: Optional[Callable[[], str]]) -> None:
        cls._class_upstream_key_resolver = resolver

    @classmethod
    def set_upstream_binding_resolver(
        cls, resolver: Optional[Callable[[str], GatewayUpstreamBinding | None]],
    ) -> None:
        cls._class_upstream_binding_resolver = resolver

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
        self._token_upstreams: dict[str, GatewayUpstreamBinding] = {}
        self._run_tokens: dict[str, set[str]] = {}
        self._lock = threading.Lock()
        self._srv: Optional[ThreadingHTTPServer] = None
        self._started = False
        self._usage_log: Optional[Path] = None
        self.account_root: Optional[str] = None   # credential account store root (set by the caller)
        # The upstream key for the provider the run's workers are BOUND to —
        # resolved by run_manager from the enabled worker profiles' provider_ref.
        # The RESOLVER is the primary wiring: it is re-read on every proxy call,
        # so settings changes and startup-test managers (which never run the
        # per-run bus wiring) resolve correctly too. `upstream_key` remains as
        # an eager override set per run; both beat the legacy pi-main file.
        self.upstream_key: Optional[str] = None
        self.upstream_key_resolver: Optional[Callable[[], str]] = None
        self.upstream_binding_resolver: Optional[Callable[[str], GatewayUpstreamBinding | None]] = None
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
    def issue_worker(
        self, claims: WorkerClaims, *, upstream: GatewayUpstreamBinding | None = None,
    ) -> str:
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
            if upstream is not None:
                self._token_upstreams[token] = upstream
            self._run_tokens.setdefault(claims.run_id, set()).add(token)
            return token

    def claims_for_token(self, token: str) -> Optional[WorkerClaims]:
        """Authenticate a token and return its immutable entrance snapshot."""
        with self._lock:
            for known, claims in self._tokens.items():
                if hmac.compare_digest(known, token):
                    return claims
        return None

    def upstream_for_token(self, token: str) -> GatewayUpstreamBinding | None:
        """Return the host-private transport snapshot for a still-live token."""
        with self._lock:
            for known, upstream in self._token_upstreams.items():
                if hmac.compare_digest(known, token):
                    return upstream
        return None

    def resolve_upstream_binding(self, provider_id: str) -> GatewayUpstreamBinding | None:
        """Resolve a probe-time provider binding without exposing its credential."""
        resolver = self.upstream_binding_resolver or type(self)._class_upstream_binding_resolver
        if resolver is None:
            return None
        try:
            return resolver(str(provider_id or ""))
        except Exception:
            return None

    def revoke_token(self, token: str) -> None:
        with self._lock:
            claims = self._tokens.pop(token, None)
            if claims is None:
                return
            self._token_upstreams.pop(token, None)
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
                self._token_upstreams.pop(token, None)
                tokens = self._run_tokens.get(claims.run_id)
                if tokens is not None:
                    tokens.discard(token)
                    if not tokens:
                        self._run_tokens.pop(claims.run_id, None)

    def revoke_run(self, run_id: str) -> None:
        with self._lock:
            for token in self._run_tokens.pop(run_id, set()):
                self._tokens.pop(token, None)
                self._token_upstreams.pop(token, None)

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

    @staticmethod
    def _anthropic_request_from_openai(req: dict[str, Any]) -> dict[str, Any]:
        # Keep the gateway's conversion semantically identical to the host-side
        # client. The worker still talks only OpenAI chat-completions to us.
        from dswarm.core.llm import LLMClient

        return LLMClient._anthropic_request_body(req)

    @staticmethod
    def _openai_finish_reason(stop_reason: Any) -> str:
        value = str(stop_reason or "")
        if value == "tool_use":
            return "tool_calls"
        if value == "max_tokens":
            return "length"
        if value in {"", "end_turn", "stop_sequence"}:
            return "stop"
        return value

    @classmethod
    def _anthropic_response_as_openai(cls, value: dict[str, Any], *, fallback_model: str) -> dict[str, Any]:
        """Translate a complete native Messages response for an OpenAI client."""
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in value.get("content") or []:
            if not isinstance(block, dict):
                continue
            kind = str(block.get("type") or "")
            if kind == "text":
                text_parts.append(str(block.get("text") or ""))
            elif kind in {"thinking", "reasoning"}:
                thinking_parts.append(str(block.get("thinking") or block.get("text") or ""))
            elif kind == "tool_use":
                tool_calls.append({
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                })
        message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
        if thinking_parts:
            # Pi's OpenAI-compatible provider understands the reasoning_content
            # extension used by DeepSeek-style reasoning endpoints.
            message["reasoning_content"] = "".join(thinking_parts)
        if tool_calls:
            message["tool_calls"] = tool_calls
        usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
        prompt_tokens = int(usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("output_tokens") or 0)
        return {
            "id": str(value.get("id") or f"msg_{uuid.uuid4().hex}"),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": str(value.get("model") or fallback_model),
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": cls._openai_finish_reason(value.get("stop_reason")),
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    @staticmethod
    def _anthropic_usage(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, Any] = {}
        if value.get("input_tokens") is not None:
            result["input_tokens"] = int(value["input_tokens"])
        if value.get("output_tokens") is not None:
            result["output_tokens"] = int(value["output_tokens"])
        return result

    @staticmethod
    def _write_openai_sse(handler: _Handler, value: dict[str, Any]) -> None:
        handler._chunk(
            b"data: " + json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + b"\n\n"
        )

    def proxy(
        self, run_id: str, body: bytes, handler: _Handler, *,
        claims: WorkerClaims | None = None,
        upstream: GatewayUpstreamBinding | None = None,
    ) -> None:
        # A token-bound transport is authoritative and immutable for the life of
        # that worker token. Legacy task tokens retain the old resolver path.
        if upstream is not None:
            real_key = upstream.api_key
        else:
            explicit = self.upstream_key
            if explicit is None:
                resolver = self.upstream_key_resolver or type(self)._class_upstream_key_resolver
                if resolver is not None:
                    try:
                        explicit = resolver()
                    except Exception:
                        explicit = None  # resolver failure -> legacy resolution paths
            real_key = resolve_upstream_key(explicit, self.account_root, run_id)
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

        wire_api = upstream.wire_api if upstream is not None else "openai-chat"
        try:
            msgs = req.get("messages") or []
            lens = ", ".join(
                f"{str(m.get('role'))}={len(str(m.get('content') or ''))}" for m in msgs
            )
            log.info("gateway req run=%s stream=%s protocol=%s model=%s msgs=%d [%s] body_bytes=%d",
                     run_id, stream, wire_api, req.get("model"), len(msgs), lens or "-", len(body))
            if wire_api == "anthropic":
                upstream_req = self._anthropic_request_from_openai(req)
                upstream_url = f"{upstream.base_url}/messages"  # type: ignore[union-attr]
                headers = upstream.headers()  # type: ignore[union-attr]
            else:
                upstream_req = normalize_upstream_request(
                    req, upstream_base=upstream.base_url if upstream is not None else _UPSTREAM_BASE,
                )
                upstream_url = f"{upstream.base_url if upstream is not None else _UPSTREAM_BASE}{_UPSTREAM_PATH}"
                headers = upstream.headers() if upstream is not None else {
                    "Authorization": f"Bearer {real_key}", "Content-Type": "application/json",
                }
        except Exception as exc:
            log.warning("gateway request conversion failed run=%s type=%s", run_id, type(exc).__name__)
            self._finish_gateway_call(call, outcome="provider_error", usage={})
            self._write_json(handler, 400, {"error": {"message": "unsupported provider request"}})
            return
        try:
            log.info("gateway request fields run=%s keys=%s", run_id, sorted(upstream_req.keys()))
        except Exception:
            pass
        t0 = time.time()
        finished = False
        try:
            with httpx.stream(
                "POST", upstream_url, json=upstream_req, headers=headers,
                timeout=httpx.Timeout(300.0, connect=20.0),
                trust_env=False,
            ) as resp:
                log.info("gateway upstream status=%s ttfb=%.2fs (run %s)",
                         resp.status_code, time.time() - t0, run_id)
                if wire_api == "anthropic":
                    if stream:
                        self._proxy_anthropic_stream(run_id, handler, resp, req, call=call)
                    else:
                        self._proxy_anthropic_json(run_id, handler, resp, req, call=call)
                elif stream:
                    self._proxy_stream(run_id, handler, resp, upstream_req, call=call)
                else:
                    self._proxy_json(run_id, handler, resp, call=call)
                finished = True
        except Exception as exc:
            outcome = "timeout" if isinstance(exc, (TimeoutError, httpx.TimeoutException)) else "transport_error"
            if not finished:
                try:
                    self._finish_gateway_call(call, outcome=outcome, usage={})
                except Exception as finish_exc:
                    log.error("gateway terminal usage write failed: %s", finish_exc)
            # Do not reflect an SDK exception: it can contain a URL or request
            # headers. The worker only needs a stable retryable diagnostic.
            log.warning("gateway upstream error run=%s type=%s", run_id, type(exc).__name__)
            try:
                self._write_json(handler, 502, {"error": {"message": "upstream error"}})
            except Exception:
                pass

    def _proxy_anthropic_json(
        self, run_id: str, handler: _Handler, resp: Any, original_req: dict[str, Any], *, call: UsageCall,
    ) -> None:
        data = resp.read()
        payload = data.decode("utf-8", "replace")
        usage = self._extract_usage(payload)
        outcome = "provider_error" if resp.status_code >= 400 else "succeeded"
        self._finish_gateway_call(call, outcome=outcome, usage=usage, legacy_payload=payload)
        if resp.status_code >= 400:
            # Native provider diagnostics are not relayed because a custom relay
            # may echo request headers. Keep credentials and endpoints private.
            self._write_json(handler, resp.status_code, {"error": {"message": "upstream provider rejected request", "type": "provider_error"}})
            return
        try:
            translated = self._anthropic_response_as_openai(
                json.loads(payload), fallback_model=str(original_req.get("model") or ""),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            self._write_json(handler, 502, {"error": {"message": "invalid upstream response"}})
            return
        self._write_json(handler, 200, translated)

    def _proxy_anthropic_stream(
        self, run_id: str, handler: _Handler, resp: Any, original_req: dict[str, Any], *, call: UsageCall,
    ) -> None:
        handler.send_response(resp.status_code)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        if resp.status_code >= 400:
            # See the non-streaming path: never pass arbitrary relay diagnostics
            # (which may include sensitive request metadata) into a container.
            self._write_openai_sse(handler, {"error": {"message": "upstream provider rejected request", "type": "provider_error"}})
            handler._chunk(b"data: [DONE]\n\n")
            self._finish_gateway_call(call, outcome="provider_error", usage={})
            handler._end_chunked()
            return

        usage: dict[str, Any] = {}
        message_id = f"chatcmpl_{uuid.uuid4().hex}"
        model = str(original_req.get("model") or "")
        tool_indices: dict[int, int] = {}
        next_tool_index = 0
        completed = False
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            try:
                event = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            kind = str(event.get("type") or "")
            if kind == "message_start":
                message = event.get("message") if isinstance(event.get("message"), dict) else {}
                message_id = str(message.get("id") or message_id)
                model = str(message.get("model") or model)
                usage.update(self._anthropic_usage(message.get("usage")))
                self._write_openai_sse(handler, {
                    "id": message_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                })
            elif kind == "content_block_start":
                block = event.get("content_block") if isinstance(event.get("content_block"), dict) else {}
                if block.get("type") == "tool_use":
                    block_index = int(event.get("index") or 0)
                    tool_index = tool_indices.setdefault(block_index, next_tool_index)
                    if tool_index == next_tool_index:
                        next_tool_index += 1
                    self._write_openai_sse(handler, {
                        "id": message_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                        "choices": [{"index": 0, "delta": {"tool_calls": [{
                            "index": tool_index, "id": str(block.get("id") or ""), "type": "function",
                            "function": {"name": str(block.get("name") or ""), "arguments": ""},
                        }]}, "finish_reason": None}],
                    })
            elif kind == "content_block_delta":
                delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
                delta_kind = str(delta.get("type") or "")
                choice_delta: dict[str, Any] = {}
                if delta_kind == "text_delta":
                    choice_delta["content"] = str(delta.get("text") or "")
                elif delta_kind == "thinking_delta":
                    choice_delta["reasoning_content"] = str(delta.get("thinking") or "")
                elif delta_kind == "input_json_delta":
                    block_index = int(event.get("index") or 0)
                    tool_index = tool_indices.setdefault(block_index, next_tool_index)
                    if tool_index == next_tool_index:
                        next_tool_index += 1
                    choice_delta["tool_calls"] = [{"index": tool_index, "function": {
                        "arguments": str(delta.get("partial_json") or ""),
                    }}]
                if choice_delta:
                    self._write_openai_sse(handler, {
                        "id": message_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                        "choices": [{"index": 0, "delta": choice_delta, "finish_reason": None}],
                    })
            elif kind == "message_delta":
                usage.update(self._anthropic_usage(event.get("usage")))
                delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
                self._write_openai_sse(handler, {
                    "id": message_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": self._openai_finish_reason(delta.get("stop_reason"))}],
                })
            elif kind == "message_stop":
                completed = True
                break
        if usage:
            prompt_tokens = int(usage.get("input_tokens") or 0)
            completion_tokens = int(usage.get("output_tokens") or 0)
            self._write_openai_sse(handler, {
                "id": message_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                "choices": [],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                          "total_tokens": prompt_tokens + completion_tokens},
            })
        handler._chunk(b"data: [DONE]\n\n")
        self._finish_gateway_call(
            call, outcome="succeeded" if completed else "transport_error", usage=usage,
            legacy_payload=json.dumps({"usage": usage}),
        )
        handler._end_chunked()

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
                    # keep the raw "data:" prefix: _extract_usage parses SSE
                    # lines (stripping them here made every streamed call's
                    # usage unknown even when the provider sent the tail chunk)
                    usage_parts.append(line)
            handler._chunk((line + "\n").encode("utf-8", "replace"))
            n_chunks += 1
            n_chars += len(line) + 1
            if is_done:
                break
        payload = "\n".join(usage_parts)
        outcome = "provider_error" if resp.status_code >= 400 else "succeeded"
        if resp.status_code >= 400:
            # The upstream rejected the request: persist WHICH field/shape it
            # disliked (worker 400s were previously undiagnosable — the body is
            # the only evidence and it died with the worker process).
            try:
                dump = Path(self.sessions_root or ".") / f"{call.run_id}-rejected-request.json"
                dump.write_text(json.dumps(req, ensure_ascii=False, indent=1), encoding="utf-8")
            except Exception:
                dump = None
            log.warning(
                "gateway upstream %d body run=%s: %s ; request keys=%s ; dump=%s",
                resp.status_code, call.run_id, payload[:400], sorted(req.keys()), dump,
            )
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
