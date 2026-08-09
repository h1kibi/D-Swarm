"""Model gateway: OpenAI-compatible reverse proxy with per-run task tokens (route A, P3).

Why: worker containers must NEVER see the real upstream API key. The worker's pi
CLI is pointed at a gateway provider (baseUrl=http://host.docker.internal:9101/v1,
baked into the worker image) and authenticates with `Authorization: Bearer
<task-token>` — the token is issued per run, injected as DEEPSEEK_API_KEY into the
worker env, and revoked at run teardown. The gateway validates the token, swaps in
the REAL upstream key (from the credential account store, falling back to the host
env), forwards the OpenAI-compatible request (streaming SSE transparent), parses the
usage from the stream, and records it per run.

This mirrors BTFly's modelgateway (task token + reverse proxy + usage capture),
implemented as a small threading HTTP server so it can live in the daemon process
alongside the ControlReceiver — no extra service to deploy. One gateway per process,
lazily started on first container-mode run; tokens are registered per run and
revoked on teardown.

Endpoints:
  POST /v1/chat/completions   (OpenAI-compatible, streaming or not)
  GET  /health               (liveness for container probes)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger("modelgateway")

DEFAULT_GATEWAY_PORT = 9101

# env knobs
_GATEWAY_PORT = int(os.environ.get("DSWARM_MODEL_GATEWAY_PORT", str(DEFAULT_GATEWAY_PORT)) or DEFAULT_GATEWAY_PORT)
_GATEWAY_BIND = os.environ.get("DSWARM_MODEL_GATEWAY_BIND", "127.0.0.1").strip() or "127.0.0.1"
# which upstream the gateway forwards to. Default: the deepseek endpoint pi's
# provider config points at (api.deepseek.com, no /v1 — pi appends it).
_UPSTREAM_BASE = os.environ.get(
    "DSWARM_MODEL_GATEWAY_UPSTREAM", "https://api.deepseek.com").strip().rstrip("/")

_UPSTREAM_PATH = "/chat/completions"


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


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ctf-swarm-modelgateway/0.1"
    # silence default stderr logging
    def log_message(self, *a):  # noqa: D401
        pass

    @property
    def _gw(self) -> "ModelGateway":
        return self.server.gateway  # type: ignore[attr-defined]

    # ── helpers ────────────────────────────────────────────────────────────────
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

    # ── routes ─────────────────────────────────────────────────────────────────
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
        run_id = self._gw.run_for_token(token)
        log.info("gateway POST %s run=%s token=%s...%s", self.path,
                 run_id, token[:4] if token else "none", token[-4:] if token else "")
        if run_id is None:
            self._write(401, b'{"error":{"message":"invalid or revoked task token","type":"authentication_error"}}')
            return
        body = self._read_body()
        if not body:
            self._write(400, b'{"error":{"message":"empty request body"}}')
            return
        self._gw.proxy(run_id, body, self)

    # ── keep-alive / streaming plumbing ───────────────────────────────────────
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
    """Process-wide OpenAI-compatible gateway. Lazily started; per-run tokens
    registered by the container backend and revoked at teardown."""

    def __init__(self, host: Optional[str] = None, port: int = _GATEWAY_PORT):
        self.host = host if host is not None else _GATEWAY_BIND
        self.port = port
        self._tokens: dict[str, str] = {}          # token -> run_id
        self._runs: dict[str, str] = {}            # run_id -> token
        self._lock = threading.Lock()
        self._srv: Optional[ThreadingHTTPServer] = None
        self._started = False
        self._usage_log: Optional[Path] = None
        self.account_root: Optional[str] = None   # credential account store root (set by the caller)
        self.sessions_root: Optional[str] = None  # per-run usage ledgers land here

    # ── lifecycle ─────────────────────────────────────────────────────────────
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

    # ── token management ───────────────────────────────────────────────────────
    def issue(self, run_id: str) -> str:
        """Issue a fresh 256-bit task token for a run; any previous token for the
        run is revoked first (a re-run must not inherit the old credential)."""
        self.revoke(run_id)
        token = secrets.token_hex(32)
        with self._lock:
            self._tokens[token] = run_id
            self._runs[run_id] = token
        return token

    def revoke(self, run_id: str) -> None:
        with self._lock:
            old = self._runs.pop(run_id, None)
            if old:
                self._tokens.pop(old, None)

    def run_for_token(self, token: str) -> Optional[str]:
        # constant-time compare to avoid token oracle timing
        with self._lock:
            for known, run_id in self._tokens.items():
                if hmac.compare_digest(known, token):
                    return run_id
        return None

    def token_for_run(self, run_id: str) -> Optional[str]:
        with self._lock:
            return self._runs.get(run_id)

    # ── proxy ──────────────────────────────────────────────────────────────────
    def proxy(self, run_id: str, body: bytes, handler: _Handler) -> None:
        real_key = _real_api_key(self.account_root, run_id)
        if not real_key:
            self._write_json(handler, 502, {"error": {"message": "gateway has no upstream key configured"}})
            return
        try:
            req = json.loads(body)
        except json.JSONDecodeError:
            self._write_json(handler, 400, {"error": {"message": "invalid JSON body"}})
            return
        stream = bool(req.get("stream", False))
        # request telemetry (INFO): WHAT actually went upstream — size, model,
        # stream flag, per-message roles and content lengths. This is the
        # authoritative record for "did the worker's full prompt arrive" (the
        # smoke's whole point: a perfunctory worker showed a TINY body here while
        # the host-side prompt dump was 4KB, meaning pi never sent the real one).
        try:
            msgs = req.get("messages") or []
            lens = ", ".join(
                f"{str(m.get('role'))}={len(str(m.get('content') or ''))}" for m in msgs)
            log.info("gateway req run=%s stream=%s model=%s msgs=%d [%s] body_bytes=%d",
                     run_id, stream, req.get("model"), len(msgs), lens or "-", len(body))
        except Exception:  # noqa: BLE001 — telemetry must never break the proxy
            pass
        upstream_url = f"{_UPSTREAM_BASE}{_UPSTREAM_PATH}"
        headers = {
            "Authorization": f"Bearer {real_key}",
            "Content-Type": "application/json",
        }
        t0 = time.time()
        try:
            with httpx.stream("POST", upstream_url, json=req, headers=headers,
                              timeout=httpx.Timeout(300.0, connect=20.0)) as resp:
                log.info("gateway upstream status=%s ttfb=%.2fs (run %s)",
                         resp.status_code, time.time() - t0, run_id)
                if stream:
                    self._proxy_stream(run_id, handler, resp, req)
                else:
                    self._proxy_json(run_id, handler, resp)
        except Exception as e:  # noqa: BLE001
            log.warning("gateway upstream error (run %s): %s", run_id, e)
            self._write_json(handler, 502, {"error": {"message": f"upstream error: {e}"}})

    def _proxy_json(self, run_id: str, handler: _Handler, resp) -> None:
        data = resp.read()
        self._record_usage(run_id, data.decode("utf-8", "replace"))
        handler.send_response(resp.status_code)
        for k, v in resp.headers.items():
            if k.lower() in ("content-type",):
                handler.send_header(k, v)
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    def _proxy_stream(self, run_id: str, handler: _Handler, resp, req) -> None:
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
            # SSE event separator: `data: {...}\n\n`. iter_lines() strips the
            # trailing newline, so an EMPTY line is the separator between events —
            # forwarding it is REQUIRED (pi's SSE parser splits on \n\n and would
            # otherwise never see the finish_reason frame as its own event).
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
            # SSE terminator: the upstream keeps the connection alive (HTTP/1.1),
            # so iter_lines() would block forever after the final frame — stop at
            # [DONE] like any SSE client.
            if is_done:
                break
        log.info("gateway stream done run=%s chunks=%d chars=%d elapsed=%.2fs",
                 run_id, n_chunks, n_chars, time.time() - t0)
        # final chunk with usage (deepseek streams a usage-bearing final data frame;
        # parse the LAST data frame's usage block for the ledger).
        self._record_usage(run_id, "\n".join(usage_parts))
        handler._end_chunked()

    def _record_usage(self, run_id: str, payload: str) -> None:
        try:
            usage: dict = {}
            for line in payload.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line and line != "[DONE]":
                    try:
                        frame = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    u = frame.get("usage")
                    if isinstance(u, dict):
                        usage = u
            if not usage:
                return
            row = {
                "ts": time.time(),
                "run_id": run_id,
                "usage": usage,
            }
            log_path = self._usage_log
            if log_path is None and self.sessions_root:
                log_path = Path(self.sessions_root) / f"{run_id}-gateway-usage.jsonl"
            if log_path is not None:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
        except Exception:  # noqa: BLE001 — usage accounting must never break the proxy
            pass

    @staticmethod
    def _write_json(handler: _Handler, status: int, obj: dict) -> None:
        handler._write(status, json.dumps(obj).encode("utf-8"))


ModelGateway._instance: Optional["ModelGateway"] = None
ModelGateway._instance_lock = threading.Lock()


def gateway_usage_log(run_id: str, sessions_root: Path) -> Path:
    """The per-run usage ledger path (sessions_root/<run_id>-gateway-usage.jsonl)."""
    return Path(sessions_root) / f"{run_id}-gateway-usage.jsonl"
