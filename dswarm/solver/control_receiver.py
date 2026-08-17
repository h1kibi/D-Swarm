"""Host-side control receiver for the reverse-connect Runtime Control Plane.

Topology (see docs/DESIGN_worker_image_clean_rebuild.md §8-9): the in-container
supervisor does NOT listen — it DIALS this receiver. So the host runs ONE long-lived
receiver (a TCP listener bound to DSWARM_CONTROL_BIND, default 127.0.0.1:9100; the
compose layout sets 0.0.0.0 so sibling worker containers can reach it) that every
run's supervisor connects into. Each supervisor sends a Hello {run_id, token}; we validate
the token against what `ensure_container` registered for that run, then keep the
connection as a `_SupervisorLink` keyed by run_id.

The HOST is still the command side ("reverse-connect, forward-control"): worker
threads call `link.start_worker(...)` / `link.signal(...)` which write op frames on
the link and read back the supervisor's replies/stream — multiplexed over the single
connection by req_id (replies) and worker_id (stream frames).

This module is THREADED (plain sockets + threads), NOT asyncio, because it's driven
from the swarm's synchronous worker threads (CliSolver runs each worker in a thread).
A dedicated accept thread + one reader thread per supervisor connection feed
thread-safe queues the worker threads consume. The receiver is a process-wide
singleton started once (lazily, or from the backend lifespan).

The supervisor is a DUMB executor; this receiver is pure transport + routing. It does
NOT touch flag/fact/graph/key business logic — that stays in the swarm/gate.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

# Default host receiver port. The container reaches it via host.docker.internal:<port>.
DEFAULT_CONTROL_PORT = int(os.environ.get("DSWARM_CONTROL_PORT", "9100"))
# What the container dials. host.docker.internal resolves to the host on Docker
# Desktop (mac/win); on Linux we add --add-host host.docker.internal:host-gateway.
CONTROL_HOST_FROM_CONTAINER = os.environ.get(
    "DSWARM_CONTROL_HOST", "host.docker.internal")
# Address the receiver BINDS to. Default 127.0.0.1: the classic single-host
# layout where the coordinator runs on the host and worker containers reach it
# via host.docker.internal (Docker Desktop) or the bridge gateway. In the P2-v3
# compose layout the coordinator runs INSIDE the web container and workers are
# SIBLING containers on a shared compose network — a loopback bind there is
# unreachable by siblings, so compose sets DSWARM_CONTROL_BIND=0.0.0.0. Safe to
# expose on a compose-internal network: every link is still gated by the
# per-run Hello token (see _handle_hello), and the port is not published to the
# host's public interface.
DEFAULT_CONTROL_BIND = os.environ.get("DSWARM_CONTROL_BIND", "127.0.0.1")


class ControlError(RuntimeError):
    """A control-plane failure (no supervisor connected, link dropped, auth failed).
    The caller treats this as `runtime_degraded` — NEVER a silent local fallback."""


class _PendingReply:
    """A one-shot slot a worker thread waits on for a req_id's reply frame."""
    __slots__ = ("event", "frame")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.frame: Optional[dict] = None


class _SupervisorLink:
    """One connected supervisor (one run). Owns the socket, a reader thread, and the
    multiplexing state. Thread-safe: many worker threads may drive it concurrently."""

    def __init__(
        self,
        run_id: str,
        conn: socket.socket,
        addr: Any,
        *,
        pool_id: str = "",
        pool_instance_id: str = "",
        generation: int = 0,
        protocol_version: int = 1,
    ):
        self.run_id = run_id
        self.pool_id = pool_id
        self.pool_instance_id = pool_instance_id
        self.generation = generation
        self.protocol_version = protocol_version
        self._conn = conn
        self._addr = addr
        self._wlock = threading.Lock()       # serialize writes onto the socket
        self._req_seq = 0
        self._req_lock = threading.Lock()
        self._pending: dict[int, _PendingReply] = {}   # req_id → waiter (non-stream ops)
        # stream routing: worker_id → queue of frames ("out"/"err"/"exit"); plus the
        # StartWorker "started" reply correlated by req_id.
        self._streams: dict[str, "_FrameQueue"] = {}
        # early-frame buffer: the supervisor may stream a worker's first lines BEFORE
        # the host has processed the "started" reply and registered that worker's
        # queue (it learns worker_id only from "started"). Frames arriving in that gap
        # are stashed here by worker_id and flushed when start_worker registers the
        # queue — otherwise the worker's opening output is silently dropped.
        self._early: dict[str, list[dict]] = {}
        self._streams_lock = threading.Lock()
        self.alive = True
        self._buf = b""
        self._reader = threading.Thread(target=self._read_loop, name=f"rcp-link-{run_id}", daemon=True)
        self._reader.start()

    # ── wire I/O ──────────────────────────────────────────────────────────────
    def _send(self, obj: dict) -> None:
        data = (json.dumps(obj) + "\n").encode()
        with self._wlock:
            try:
                self._conn.sendall(data)
            except OSError as e:
                self.alive = False
                raise ControlError(f"control link send failed (run {self.run_id}): {e}") from e

    def _read_loop(self) -> None:
        """Read frames forever, dispatch each to its waiter (by req_id) or stream
        queue (by worker_id). Runs until the connection closes."""
        try:
            while True:
                while b"\n" not in self._buf:
                    chunk = self._conn.recv(65536)
                    if not chunk:
                        raise ConnectionError("supervisor closed the link")
                    self._buf += chunk
                line, _, self._buf = self._buf.partition(b"\n")
                if not line.strip():
                    continue
                try:
                    f = json.loads(line.decode())
                except ValueError:
                    continue
                self._dispatch_frame(f)
        except (OSError, ConnectionError):
            pass
        finally:
            self.alive = False
            self._fail_all()

    def _dispatch_frame(self, f: dict) -> None:
        t = f.get("t")
        wid = f.get("worker_id")
        if t in ("out", "err", "exit") and wid:
            with self._streams_lock:
                q = self._streams.get(wid)
                if q is not None:
                    q.put(f)
                else:
                    # queue not registered yet (started reply still in flight) —
                    # buffer so the worker's opening frames aren't lost.
                    self._early.setdefault(wid, []).append(f)
            return
        # "started" reply (StartWorker) AND "resp" replies (Signal/Status/Health) are
        # correlated by req_id.
        rid = f.get("req_id")
        if rid is not None:
            with self._req_lock:
                waiter = self._pending.pop(int(rid), None)
            if waiter is not None:
                waiter.frame = f
                waiter.event.set()

    def _fail_all(self) -> None:
        # wake every waiter + close every stream queue so worker threads don't hang.
        with self._req_lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for w in waiters:
            w.frame = None
            w.event.set()
        with self._streams_lock:
            qs = list(self._streams.values())
            self._early.clear()
        for q in qs:
            q.close()

    # ── op API (called by worker threads) ─────────────────────────────────────
    def _next_req(self) -> int:
        with self._req_lock:
            self._req_seq += 1
            return self._req_seq

    def _request(self, op: str, *, timeout: float, **fields: Any) -> dict:
        """Send a non-stream op, block for its reply frame."""
        if not self.alive:
            raise ControlError(f"control link for run {self.run_id} is down")
        rid = self._next_req()
        waiter = _PendingReply()
        with self._req_lock:
            self._pending[rid] = waiter
        self._send({"op": op, "req_id": rid, **fields})
        if not waiter.event.wait(timeout):
            with self._req_lock:
                self._pending.pop(rid, None)
            raise ControlError(f"control op {op} timed out (run {self.run_id})")
        if waiter.frame is None:
            raise ControlError(f"control link dropped during {op} (run {self.run_id})")
        return waiter.frame

    def _stream_for(self, worker_id: str) -> "Optional[_FrameQueue]":
        with self._streams_lock:
            return self._streams.get(worker_id)

    def health(self, *, timeout: float = 5.0) -> dict:
        return self._request("Health", timeout=timeout)

    def signal(self, worker_id: str, name: str, *, timeout: float = 15.0) -> bool:
        try:
            r = self._request("Signal", worker_id=worker_id, signal=name, timeout=timeout)
            return bool(r.get("ok"))
        except ControlError:
            return False

    def status(self, worker_id: str, *, timeout: float = 10.0) -> dict:
        return self._request("Status", worker_id=worker_id, timeout=timeout)

    def teardown(self, *, timeout: float = 15.0) -> Optional[dict]:
        try:
            return self._request("TeardownRun", timeout=timeout)
        except ControlError:
            return None

    def start_worker(self, spec: dict, *, timeout: float) -> "tuple[str, _FrameQueue]":
        """Send StartWorker, register a stream queue for the assigned worker_id, and
        return (worker_id, queue). The caller drains the queue (out/err/exit frames)."""
        if not self.alive:
            raise ControlError(f"control link for run {self.run_id} is down")
        rid = self._next_req()
        waiter = _PendingReply()
        with self._req_lock:
            self._pending[rid] = waiter
        self._send({"op": "StartWorker", "req_id": rid, "spec": spec})
        if not waiter.event.wait(min(60.0, timeout + 30)):
            with self._req_lock:
                self._pending.pop(rid, None)
            raise ControlError(f"StartWorker timed out (run {self.run_id})")
        f = waiter.frame
        if not f or f.get("t") != "started" or not f.get("worker_id"):
            err = (f or {}).get("error") or "supervisor did not start worker"
            raise ControlError(f"StartWorker failed: {err}")
        wid = f["worker_id"]
        q = _FrameQueue()
        with self._streams_lock:
            self._streams[wid] = q
            # flush any frames that arrived before this queue existed, in order.
            for early in self._early.pop(wid, []):
                q.put(early)
        return wid, q

    def drop_stream(self, worker_id: str) -> None:
        with self._streams_lock:
            self._streams.pop(worker_id, None)
            self._early.pop(worker_id, None)

    def close(self) -> None:
        self.alive = False
        try:
            self._conn.close()
        except OSError:
            pass


class _FrameQueue:
    """A simple closeable blocking queue of stream frames for one worker."""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._items: list[dict] = []
        self._closed = False

    def put(self, f: dict) -> None:
        with self._cv:
            self._items.append(f)
            self._cv.notify()

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def get(self, timeout: float) -> Optional[dict]:
        """Return the next frame, or None if closed+drained or on timeout."""
        deadline = time.time() + timeout
        with self._cv:
            while not self._items and not self._closed:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._cv.wait(remaining)
            if self._items:
                return self._items.pop(0)
            return None  # closed and drained


@dataclass(frozen=True)
class ExpectedRuntimeIdentity:
    """Immutable identity the host expects from one runtime pool instance."""

    run_id: str
    pool_id: str
    pool_instance_id: str
    generation: int
    expected_image_id: str
    protocol_version: int = 2

    def __post_init__(self) -> None:
        _validate_identity_text(self.run_id, field="run_id", max_length=256)
        _validate_identity_text(self.pool_id, field="pool_id", max_length=256)
        _validate_canonical_uuid4(self.pool_instance_id)
        if isinstance(self.generation, bool) or not isinstance(self.generation, int):
            raise ValueError("generation must be a positive integer")
        if self.generation <= 0 or self.generation > 2_147_483_647:
            raise ValueError("generation must be a positive integer")
        _validate_identity_text(
            self.expected_image_id,
            field="expected_image_id",
            max_length=512,
        )
        if self.protocol_version != 2:
            raise ValueError("protocol_version must be 2")


def _validate_identity_text(value: object, *, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ValueError(f"{field} must be a bounded non-empty string")
    if value != value.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{field} contains invalid characters")
    return value


def _validate_canonical_uuid4(value: object) -> str:
    _validate_identity_text(value, field="pool_instance_id", max_length=36)
    assert isinstance(value, str)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError("pool_instance_id must be a canonical UUID4") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("pool_instance_id must be a canonical UUID4")
    return value


def _wire_string(value: object, *, max_length: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > max_length:
        return None
    if value != value.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    return value


class _LegacyRunControlAdapter:
    """Compatibility boundary for the pre-M9 run-keyed control API.

    New runtime-pool code must use ``issue_pool`` / ``wait_pool`` / ``link_for``.
    This adapter exists only while the legacy container execution path is migrated.
    """

    def __init__(self, owner: "ControlReceiver") -> None:
        self._owner = owner

    def expect(self, run_id: str, token: str) -> None:
        with self._owner._lock:
            self._owner._legacy_tokens[run_id] = token

    def expected_token(self, run_id: str) -> str | None:
        with self._owner._lock:
            return self._owner._legacy_tokens.get(run_id)

    def install(self, run_id: str, link: _SupervisorLink) -> None:
        with self._owner._lock:
            old = self._owner._legacy_links.get(run_id)
            self._owner._legacy_links[run_id] = link
            self._owner._link_event.notify_all()
        if old is not None:
            old.close()

    def await_link(
        self, run_id: str, *, deadline_s: Optional[float] = None
    ) -> _SupervisorLink:
        if deadline_s is None:
            deadline_s = self._owner._CONTROL_LINK_DEADLINE
        deadline = time.monotonic() + deadline_s
        with self._owner._lock:
            while True:
                if self._owner._stopped:
                    raise ControlError("control_receiver_stopped")
                link = self._owner._legacy_links.get(run_id)
                if link is not None and link.alive:
                    return link
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ControlError(
                        f"no supervisor connected for run {run_id} within "
                        f"{deadline_s:.0f}s (container up but control plane never dialed back)"
                    )
                self._owner._link_event.wait(remaining)

    def get_link(self, run_id: str) -> Optional[_SupervisorLink]:
        with self._owner._lock:
            link = self._owner._legacy_links.get(run_id)
            return link if (link and link.alive) else None

    def forget(self, run_id: str) -> None:
        with self._owner._lock:
            link = self._owner._legacy_links.pop(run_id, None)
            self._owner._legacy_tokens.pop(run_id, None)
            self._owner._link_event.notify_all()
        if link is not None:
            link.close()


class ControlReceiver:
    """Host listener for legacy run links and RCP-v2 pool-instance links."""

    _instance: "Optional[ControlReceiver]" = None
    _instance_lock = threading.Lock()

    _CONTROL_LINK_DEADLINE = float(
        os.environ.get("DSWARM_CONTROL_LINK_DEADLINE", "40") or 40
    )
    _MAX_HELLO_BYTES = 16 * 1024

    def __init__(self, host: Optional[str] = None, port: int = DEFAULT_CONTROL_PORT):
        self.host = host if host is not None else DEFAULT_CONTROL_BIND
        self.port = port
        self._legacy_tokens: dict[str, str] = {}
        self._legacy_links: dict[str, _SupervisorLink] = {}
        self._pool_identities: dict[str, ExpectedRuntimeIdentity] = {}
        self._pool_tokens: dict[str, str] = {}
        self._pool_links: dict[str, _SupervisorLink] = {}
        self._pool_instances_by_run: dict[str, set[str]] = {}
        self._current_pool_instance: dict[tuple[str, str], str] = {}
        self._connecting_pool_instances: set[str] = set()
        self._lock = threading.Lock()
        self._link_event = threading.Condition(self._lock)
        self._srv: Optional[socket.socket] = None
        self._started = False
        self._stopped = False
        self._legacy = _LegacyRunControlAdapter(self)

    @classmethod
    def instance(cls) -> "ControlReceiver":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = ControlReceiver()
                cls._instance.start()
            return cls._instance

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._stopped = False
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(64)
        with self._lock:
            self._srv = srv
            self._started = True
        threading.Thread(
            target=self._accept_loop,
            name="rcp-receiver-accept",
            daemon=True,
        ).start()

    def stop(self) -> None:
        """Stop accepting links, close live links, and wake every waiter."""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._started = False
            srv = self._srv
            self._srv = None
            links = [*self._legacy_links.values(), *self._pool_links.values()]
            self._legacy_links.clear()
            self._pool_links.clear()
            self._connecting_pool_instances.clear()
            self._link_event.notify_all()
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        for link in links:
            link.close()

    def _accept_loop(self) -> None:
        with self._lock:
            srv = self._srv
        if srv is None:
            return
        while True:
            try:
                conn, addr = srv.accept()
            except OSError:
                return
            threading.Thread(
                target=self._handshake,
                args=(conn, addr),
                name="rcp-handshake",
                daemon=True,
            ).start()

    def _read_hello(self, conn: socket.socket) -> dict | None:
        conn.settimeout(30.0)
        try:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return None
                buf += chunk
                if len(buf) > self._MAX_HELLO_BYTES:
                    return None
            line, _, _ = buf.partition(b"\n")
            hello = json.loads(line.decode("utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None
        return hello if isinstance(hello, dict) else None

    @staticmethod
    def _send_ack(conn: socket.socket, *, ok: bool, error: str = "") -> bool:
        payload = {"ok": ok}
        if not ok:
            payload["error"] = error
        try:
            conn.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())
            return True
        except OSError:
            return False

    def _handshake(self, conn: socket.socket, addr: Any) -> None:
        hello = self._read_hello(conn)
        if hello is None:
            conn.close()
            return

        token = hello.get("token")
        with self._lock:
            token_is_pool_token = isinstance(token, str) and any(
                hmac.compare_digest(token, expected)
                for expected in self._pool_tokens.values()
            )
        has_pool_shape = any(
            key in hello
            for key in (
                "protocol_version",
                "pool_id",
                "pool_instance_id",
                "generation",
            )
        )
        if has_pool_shape or token_is_pool_token:
            self._handshake_pool(conn, addr, hello)
        else:
            self._handshake_legacy(conn, addr, hello)

    def _handshake_legacy(self, conn: socket.socket, addr: Any, hello: dict) -> None:
        run_id = hello.get("run_id") or ""
        token = hello.get("token") or ""
        expected = self._legacy.expected_token(run_id)
        ok = (
            isinstance(token, str)
            and expected is not None
            and hmac.compare_digest(token, expected)
        )
        if not self._send_ack(conn, ok=ok, error="unauthorized") or not ok:
            conn.close()
            return
        conn.settimeout(None)
        self._legacy.install(run_id, _SupervisorLink(run_id, conn, addr))

    def _handshake_pool(self, conn: socket.socket, addr: Any, hello: dict) -> None:
        instance_id = _wire_string(hello.get("pool_instance_id"), max_length=36)
        try:
            if instance_id is not None:
                _validate_canonical_uuid4(instance_id)
        except ValueError:
            instance_id = None

        run_id = _wire_string(hello.get("run_id"), max_length=256)
        pool_id = _wire_string(hello.get("pool_id"), max_length=256)
        token = _wire_string(hello.get("token"), max_length=512)
        generation = hello.get("generation")
        protocol_version = hello.get("protocol_version")
        if isinstance(generation, bool) or not isinstance(generation, int):
            generation = None
        if isinstance(protocol_version, bool) or not isinstance(protocol_version, int):
            protocol_version = None

        with self._lock:
            expected = self._pool_identities.get(instance_id or "")
            existing = self._pool_links.get(instance_id or "")
            if existing is not None and not existing.alive:
                self._pool_links.pop(instance_id or "", None)
                existing = None
            ok = (
                not self._stopped
                and expected is not None
                and protocol_version == expected.protocol_version
                and run_id == expected.run_id
                and pool_id == expected.pool_id
                and instance_id == expected.pool_instance_id
                and generation == expected.generation
                and token is not None
                and hmac.compare_digest(token, self._pool_tokens.get(instance_id, ""))
                and existing is None
                and instance_id not in self._connecting_pool_instances
            )
            if ok:
                assert instance_id is not None
                self._connecting_pool_instances.add(instance_id)

        if not ok:
            self._send_ack(
                conn,
                ok=False,
                error="runtime_identity_mismatch",
            )
            conn.close()
            return
        assert expected is not None and instance_id is not None
        if not self._send_ack(conn, ok=True):
            with self._lock:
                self._connecting_pool_instances.discard(instance_id)
                self._link_event.notify_all()
            conn.close()
            return

        conn.settimeout(None)
        try:
            link = _SupervisorLink(
                expected.run_id,
                conn,
                addr,
                pool_id=expected.pool_id,
                pool_instance_id=expected.pool_instance_id,
                generation=expected.generation,
                protocol_version=expected.protocol_version,
            )
        except Exception:
            with self._lock:
                self._connecting_pool_instances.discard(instance_id)
                self._link_event.notify_all()
            conn.close()
            return

        install = False
        with self._lock:
            current_expected = self._pool_identities.get(instance_id)
            if not self._stopped and current_expected == expected:
                self._pool_links[instance_id] = link
                install = True
            self._connecting_pool_instances.discard(instance_id)
            self._link_event.notify_all()
        if not install:
            link.close()

    def issue_pool(self, expected_identity: ExpectedRuntimeIdentity) -> str:
        if not isinstance(expected_identity, ExpectedRuntimeIdentity):
            raise TypeError("expected_identity must be ExpectedRuntimeIdentity")
        token = secrets.token_urlsafe(32)
        old_link: _SupervisorLink | None = None
        with self._lock:
            if self._stopped:
                raise ControlError("control_receiver_stopped")
            key = (expected_identity.run_id, expected_identity.pool_id)
            old_instance = self._current_pool_instance.get(key)
            if old_instance and old_instance != expected_identity.pool_instance_id:
                old_link = self._remove_pool_instance_locked(old_instance)
            instance_id = expected_identity.pool_instance_id
            self._pool_identities[instance_id] = expected_identity
            self._pool_tokens[instance_id] = token
            self._pool_instances_by_run.setdefault(expected_identity.run_id, set()).add(
                instance_id
            )
            self._current_pool_instance[key] = instance_id
            self._link_event.notify_all()
        if old_link is not None:
            old_link.close()
        return token

    def wait_pool(self, pool_instance_id: str, timeout: float) -> _SupervisorLink:
        _validate_canonical_uuid4(pool_instance_id)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + float(timeout)
        with self._lock:
            while True:
                if self._stopped:
                    raise ControlError("control_receiver_stopped")
                link = self._pool_links.get(pool_instance_id)
                if link is not None and link.alive:
                    return link
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ControlError(
                        f"no supervisor connected for pool instance within {timeout:g}s"
                    )
                self._link_event.wait(remaining)

    def link_for(self, pool_instance_id: str) -> Optional[_SupervisorLink]:
        try:
            _validate_canonical_uuid4(pool_instance_id)
        except ValueError:
            return None
        with self._lock:
            link = self._pool_links.get(pool_instance_id)
            return link if (link and link.alive) else None

    def _remove_pool_instance_locked(
        self, pool_instance_id: str
    ) -> Optional[_SupervisorLink]:
        expected = self._pool_identities.pop(pool_instance_id, None)
        self._pool_tokens.pop(pool_instance_id, None)
        self._connecting_pool_instances.discard(pool_instance_id)
        link = self._pool_links.pop(pool_instance_id, None)
        if expected is not None:
            run_instances = self._pool_instances_by_run.get(expected.run_id)
            if run_instances is not None:
                run_instances.discard(pool_instance_id)
                if not run_instances:
                    self._pool_instances_by_run.pop(expected.run_id, None)
            key = (expected.run_id, expected.pool_id)
            if self._current_pool_instance.get(key) == pool_instance_id:
                self._current_pool_instance.pop(key, None)
        self._link_event.notify_all()
        return link

    def revoke_pool_instance(self, pool_instance_id: str) -> None:
        try:
            _validate_canonical_uuid4(pool_instance_id)
        except ValueError:
            return
        with self._lock:
            link = self._remove_pool_instance_locked(pool_instance_id)
        if link is not None:
            link.close()

    def revoke_pool(self, pool_id: str) -> None:
        with self._lock:
            instance_ids = [
                instance_id
                for instance_id, expected in self._pool_identities.items()
                if expected.pool_id == pool_id
            ]
            links = [
                link
                for instance_id in instance_ids
                if (link := self._remove_pool_instance_locked(instance_id)) is not None
            ]
        for link in links:
            link.close()

    def revoke_run(self, run_id: str) -> None:
        with self._lock:
            instance_ids = list(self._pool_instances_by_run.get(run_id, ()))
            links = [
                link
                for instance_id in instance_ids
                if (link := self._remove_pool_instance_locked(instance_id)) is not None
            ]
            legacy_link = self._legacy_links.pop(run_id, None)
            self._legacy_tokens.pop(run_id, None)
            self._link_event.notify_all()
        for link in links:
            link.close()
        if legacy_link is not None:
            legacy_link.close()

    # Legacy compatibility API. New pool code must not call these methods.
    def expect(self, run_id: str, token: str) -> None:
        self._legacy.expect(run_id, token)

    def await_link(
        self, run_id: str, *, deadline_s: Optional[float] = None
    ) -> _SupervisorLink:
        return self._legacy.await_link(run_id, deadline_s=deadline_s)

    def get_link(self, run_id: str) -> Optional[_SupervisorLink]:
        return self._legacy.get_link(run_id)

    def has_link(self, run_id: str) -> bool:
        return self._legacy.get_link(run_id) is not None

    def forget(self, run_id: str) -> None:
        self._legacy.forget(run_id)
