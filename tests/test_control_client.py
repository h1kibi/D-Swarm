"""Host-side reverse-connect Runtime Control Plane tests (no Docker, no Go binary).

We run the real `ControlReceiver` and a fake supervisor that DIALS it (the reverse
topology), sends a Hello, then answers commands — mirroring cmd/runtime-agent. This
locks the host half: receiver handshake + token auth + run_id routing, the
_SupervisorLink op/stream multiplexing, and run_cli_streaming_rcp consuming the
stream (out lines → driver.parse_stream_steps, exit → result), plus the cancel path.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from dswarm.solver.cli_driver import CliResult, StreamStep
from dswarm.solver import control_client as cc
from dswarm.solver import control_receiver as cr


def test_control_bind_defaults_to_loopback_and_honors_env(monkeypatch):
    # P2-v3: the receiver bind address is env-driven (DSWARM_CONTROL_BIND). Default
    # stays 127.0.0.1 (classic single-host) so this is a pure additive knob; the
    # compose layout sets 0.0.0.0 so sibling worker containers can reach it. An
    # explicit host always wins over the env default (tests pass host=...).
    # __init__ reads the module global at call time, so patch the attribute.
    monkeypatch.setattr(cr, "DEFAULT_CONTROL_BIND", "127.0.0.1")
    assert cr.ControlReceiver(port=0).host == "127.0.0.1"      # default
    monkeypatch.setattr(cr, "DEFAULT_CONTROL_BIND", "0.0.0.0")
    assert cr.ControlReceiver(port=0).host == "0.0.0.0"        # env → compose
    # explicit host beats the env default
    assert cr.ControlReceiver(host="127.0.0.1", port=0).host == "127.0.0.1"


class _FakeSupervisor:
    """A stand-in supervisor: dials the receiver, sends Hello, then services ops on
    that one connection (reverse-connect). Scriptable per-worker stream + started
    error. Records signals."""

    def __init__(self, receiver_port: int, run_id: str, token: str, *,
                 stream=None, started_error: str = ""):
        self.run_id = run_id
        self.token = token
        self.stream = stream or []          # frames to emit after 'started' (out/err/exit)
        self.started_error = started_error
        self.signals: list[dict] = []
        self._wlock = threading.Lock()
        self._s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._s.connect(("127.0.0.1", receiver_port))
        # Hello
        self._send({"hello": 1, "run_id": run_id, "token": token, "version": "fake/1"})
        ack = self._readline()
        self.ack = json.loads(ack) if ack else {}
        self._buf = b""
        self._worker_seq = 0
        if self.ack.get("ok"):
            self._t = threading.Thread(target=self._serve, daemon=True)
            self._t.start()

    def _send(self, obj: dict) -> None:
        with self._wlock:
            self._s.sendall((json.dumps(obj) + "\n").encode())

    def _readline(self) -> str:
        buf = b""
        while b"\n" not in buf:
            c = self._s.recv(4096)
            if not c:
                return ""
            buf += c
        line, _, _ = buf.partition(b"\n")
        return line.decode()

    def _serve(self) -> None:
        try:
            while True:
                while b"\n" not in self._buf:
                    c = self._s.recv(65536)
                    if not c:
                        return
                    self._buf += c
                line, _, self._buf = self._buf.partition(b"\n")
                if not line.strip():
                    continue
                req = json.loads(line.decode())
                self._handle(req)
        except OSError:
            return

    def _handle(self, req: dict) -> None:
        op = req.get("op")
        rid = req.get("req_id")
        if op == "StartWorker":
            if self.started_error:
                self._send({"t": "started", "req_id": rid, "error": self.started_error})
                return
            self._worker_seq += 1
            wid = f"w-{self._worker_seq}-test"
            self._send({"t": "started", "req_id": rid, "worker_id": wid})
            for ev in self.stream:
                ev = dict(ev)
                ev["worker_id"] = wid
                self._send(ev)
                time.sleep(0.005)
        elif op == "Signal":
            self.signals.append(req)
            self._send({"t": "resp", "req_id": rid, "ok": True})
        elif op == "Health":
            self._send({"t": "resp", "req_id": rid, "ok": True, "version": "dswarm-runtime-agent/2"})
        else:
            self._send({"t": "resp", "req_id": rid, "ok": True})


class _Driver:
    name = "pi"

    def parse_stream_steps(self, line):
        return [StreamStep(kind="reasoning", text=line)]

    def parse(self, out, err):
        return CliResult(text=out.strip())


@pytest.fixture
def receiver():
    """A fresh receiver on an ephemeral port (NOT the singleton, to isolate tests)."""
    rcv = cr.ControlReceiver(host="127.0.0.1", port=0)
    rcv.start()
    # discover the bound port
    port = rcv._srv.getsockname()[1]
    rcv._test_port = port
    # make the module-level helpers resolve THIS receiver
    cr.ControlReceiver._instance = rcv
    yield rcv
    rcv.stop()
    cr.ControlReceiver._instance = None


def test_handshake_routing_and_stream(receiver):
    receiver.expect("run-1", "tok-1")
    sup = _FakeSupervisor(receiver._test_port, "run-1", "tok-1", stream=[
        {"t": "out", "line": "hello"},
        {"t": "out", "line": "world"},
        {"t": "err", "line": "warn"},
        {"t": "exit", "rc": 0, "oom": False, "timed_out": False},
    ])
    assert sup.ack.get("ok") is True
    steps = []
    res = cc.run_cli_streaming_rcp(
        _Driver(), ["claude", "-p"], run_id="run-1",
        container_cwd="/home/kali/workspace", timeout=30,
        on_step=lambda s: steps.append(s))
    assert [s.text for s in steps] == ["hello", "world"]
    assert res.text == "hello\nworld"
    assert res.runtime_status["status"] == "finished"
    assert res.runtime_status["rc"] == 0


def test_oom_from_exit_frame(receiver):
    receiver.expect("run-oom", "t")
    _FakeSupervisor(receiver._test_port, "run-oom", "t", stream=[
        {"t": "out", "line": "x"},
        {"t": "exit", "rc": 137, "oom": True, "timed_out": False},
    ])
    res = cc.run_cli_streaming_rcp(
        _Driver(), ["claude"], run_id="run-oom",
        container_cwd="/w", timeout=30, on_step=lambda s: None)
    assert res.oom_killed is True
    assert res.timed_out is False
    assert res.runtime_status["status"] == "oom"


def test_token_handshake_rejects_wrong(receiver):
    receiver.expect("run-auth", "right")
    # wrong token → receiver rejects the Hello → no link bound
    sup = _FakeSupervisor(receiver._test_port, "run-auth", "wrong")
    assert sup.ack.get("ok") is False
    # await_link must time out (no valid supervisor)
    with pytest.raises(cc.ControlError):
        cc.run_cli_streaming_rcp(_Driver(), ["claude"], run_id="run-auth",
                                 container_cwd="/w", timeout=2, on_step=lambda s: None)


def test_started_error_raises(receiver):
    receiver.expect("run-err", "t")
    _FakeSupervisor(receiver._test_port, "run-err", "t", started_error="exec: claude: not found")
    with pytest.raises(cc.ControlError):
        cc.run_cli_rcp(_Driver(), ["claude"], run_id="run-err",
                       container_cwd="/w", timeout=10)


def test_cancel_event_issues_kill(receiver):
    receiver.expect("run-cancel", "t")
    # a stream that starts but never exits → the watcher must KILL it
    sup = _FakeSupervisor(receiver._test_port, "run-cancel", "t", stream=[
        {"t": "out", "line": "begin"},
        # no exit — simulate a long-running worker
    ])
    cancel = threading.Event()
    cancel.set()
    res = cc.run_cli_streaming_rcp(
        _Driver(), ["claude"], run_id="run-cancel",
        container_cwd="/w", timeout=30, on_step=lambda s: None, cancel_event=cancel)
    time.sleep(0.05)
    assert any(s.get("signal") == "KILL" for s in sup.signals)
    assert res.cancelled is True


def test_await_link_times_out_when_no_supervisor(receiver):
    receiver.expect("run-nobody", "t")
    # nobody dials in → await_link / wait_supervisor_ready must fail (degraded), not hang
    assert cc.wait_supervisor_ready("run-nobody", deadline_s=1.0) is False


def test_link_drop_mid_worker_raises_control_error(receiver):
    # supervisor sends an opening line then DROPS the connection with no exit frame
    # (supervisor died / container lost the link). The host must raise ControlError
    # (→ swarm marks runtime_degraded), NOT return a silent empty result.
    receiver.expect("run-drop", "t")
    sup = _FakeSupervisor(receiver._test_port, "run-drop", "t", stream=[
        {"t": "out", "line": "started-work"},
        # NO exit frame
    ])

    # close the supervisor's socket shortly after it streams the opening line, so the
    # host's queue.get returns None with link.alive False and no exit seen.
    # shutdown(SHUT_RDWR) BEFORE close: a bare close() only drops this thread's fd
    # reference, but the fake's _serve thread is blocked in recv() on the SAME socket
    # and keeps the fd (hence the connection) alive — so the receiver never sees EOF
    # and the host hangs until the q.get timeout. A real dying supervisor is a process
    # exit that closes every fd at once; shutdown() reproduces that by sending FIN
    # immediately and unblocking _serve's recv, so the drop is detected at once.
    def drop():
        time.sleep(0.1)
        try:
            sup._s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sup._s.close()
        except OSError:
            pass
    threading.Thread(target=drop, daemon=True).start()

    with pytest.raises(cc.ControlError):
        cc.run_cli_streaming_rcp(
            _Driver(), ["claude"], run_id="run-drop",
            container_cwd="/w", timeout=30, on_step=lambda s: None)


def test_early_frames_before_started_are_not_lost(receiver):
    # the supervisor streams a worker's opening frames immediately after "started";
    # if any arrive before the host registers the worker's queue they must be buffered
    # and flushed, not dropped (the 'world'-only regression). A burst with no gap is
    # the stress case.
    receiver.expect("run-early", "t")
    sup = _FakeSupervisor(receiver._test_port, "run-early", "t", stream=[
        {"t": "out", "line": "first"},
        {"t": "out", "line": "second"},
        {"t": "out", "line": "third"},
        {"t": "exit", "rc": 0},
    ])
    steps = []
    res = cc.run_cli_streaming_rcp(
        _Driver(), ["claude"], run_id="run-early",
        container_cwd="/w", timeout=30, on_step=lambda s: steps.append(s))
    assert [s.text for s in steps] == ["first", "second", "third"]
    assert res.runtime_status["status"] == "finished"



def test_teardown_run_forgets_receiver_link(receiver):
    receiver.expect("run-teardown", "tok")
    sup = _FakeSupervisor(receiver._test_port, "run-teardown", "tok")
    assert sup.ack.get("ok") is True
    assert receiver.await_link("run-teardown", deadline_s=2).alive is True

    cc.teardown_run("run-teardown")

    assert receiver.get_link("run-teardown") is None
    assert receiver.has_link("run-teardown") is False

def test_filter_env_only_allowed_keys():
    out = cc._filter_env({
        "DSWARM_X": "1", "ANTHROPIC_KEY": "k", "DEEPSEEK_API_KEY_FILE": "/f",
        "OPENAI_API_KEY": "v", "PATH": "/leak", "HOME": "/leak", "HOME_OK": "x",
        "PI_CODING_AGENT_DIR": "/home/kali/workspace/homes/cli-pi/.pi/agent",
        "PI_UNRELATED_SECRET": "nope",
    })
    assert out == {"DSWARM_X": "1", "ANTHROPIC_KEY": "k", "DEEPSEEK_API_KEY_FILE": "/f",
                   "OPENAI_API_KEY": "v",
                   "PI_CODING_AGENT_DIR": "/home/kali/workspace/homes/cli-pi/.pi/agent"}
    assert cc._filter_env({"HOME": "/home/kali/workspace/h"}) == {"HOME": "/home/kali/workspace/h"}

# ---------------------------------------------------------------------------
# M9a-2: pool-instance-scoped RCP v2 identity

_UUID_A = "11111111-1111-4111-8111-111111111111"
_UUID_B = "22222222-2222-4222-8222-222222222222"


def _runtime_identity(
    *,
    run_id: str = "run-pool",
    pool_id: str = "pool-v1::alpha",
    pool_instance_id: str = _UUID_A,
    generation: int = 1,
) -> cr.ExpectedRuntimeIdentity:
    return cr.ExpectedRuntimeIdentity(
        run_id=run_id,
        pool_id=pool_id,
        pool_instance_id=pool_instance_id,
        generation=generation,
        expected_image_id="sha256:abc",
        protocol_version=2,
    )


def _pool_hello(identity: cr.ExpectedRuntimeIdentity, token: str, **overrides) -> dict:
    hello = {
        "protocol_version": identity.protocol_version,
        "run_id": identity.run_id,
        "pool_id": identity.pool_id,
        "pool_instance_id": identity.pool_instance_id,
        "generation": identity.generation,
        "token": token,
        "version": "fake/2",
    }
    hello.update(overrides)
    return hello


def _raw_pool_hello(receiver, hello: dict) -> tuple[socket.socket, dict]:
    conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    conn.settimeout(2.0)
    conn.connect(("127.0.0.1", receiver._test_port))
    conn.sendall((json.dumps(hello) + "\n").encode())
    buf = b""
    while b"\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf += chunk
    line, _, _ = buf.partition(b"\n")
    ack = json.loads(line.decode()) if line else {}
    return conn, ack


def _connect_pool(receiver, identity: cr.ExpectedRuntimeIdentity, token: str):
    conn, ack = _raw_pool_hello(receiver, _pool_hello(identity, token))
    assert ack == {"ok": True}
    return conn, receiver.wait_pool(identity.pool_instance_id, timeout=2.0)


def test_same_run_can_hold_two_independent_pool_links(receiver):
    identity_a = _runtime_identity(pool_id="pool-v1::alpha", pool_instance_id=_UUID_A)
    identity_b = _runtime_identity(pool_id="pool-v1::beta", pool_instance_id=_UUID_B)
    token_a = receiver.issue_pool(identity_a)
    token_b = receiver.issue_pool(identity_b)

    conn_a, link_a = _connect_pool(receiver, identity_a, token_a)
    conn_b, link_b = _connect_pool(receiver, identity_b, token_b)
    try:
        assert link_a is receiver.link_for(_UUID_A)
        assert link_b is receiver.link_for(_UUID_B)
        assert link_a is not link_b
    finally:
        conn_a.close()
        conn_b.close()


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("run_id", "run-other"),
        ("pool_id", "pool-v1::other"),
        ("pool_instance_id", _UUID_B),
        ("generation", 2),
        ("protocol_version", 1),
    ],
)
def test_pool_hello_rejects_every_identity_mismatch(receiver, field, bad_value):
    identity = _runtime_identity()
    token = receiver.issue_pool(identity)
    conn, ack = _raw_pool_hello(
        receiver,
        _pool_hello(identity, token, **{field: bad_value}),
    )
    conn.close()

    assert ack == {"ok": False, "error": "runtime_identity_mismatch"}
    assert receiver.link_for(identity.pool_instance_id) is None


def test_pool_hello_rejects_wrong_token_without_leaking_identity(receiver):
    identity = _runtime_identity()
    receiver.issue_pool(identity)
    conn, ack = _raw_pool_hello(receiver, _pool_hello(identity, "secret-wrong-token"))
    conn.close()

    assert ack == {"ok": False, "error": "runtime_identity_mismatch"}
    encoded = json.dumps(ack)
    assert identity.run_id not in encoded
    assert identity.pool_id not in encoded
    assert identity.pool_instance_id not in encoded
    assert "secret-wrong-token" not in encoded


def test_pool_hello_rejects_malformed_uuid_and_explicit_v1(receiver):
    identity = _runtime_identity()
    token = receiver.issue_pool(identity)

    bad_uuid, uuid_ack = _raw_pool_hello(
        receiver,
        _pool_hello(identity, token, pool_instance_id="not-a-uuid"),
    )
    bad_uuid.close()
    v1, v1_ack = _raw_pool_hello(
        receiver,
        {"hello": 1, "run_id": identity.run_id, "token": token, "version": "fake/1"},
    )
    v1.close()

    assert uuid_ack == {"ok": False, "error": "runtime_identity_mismatch"}
    assert v1_ack == {"ok": False, "error": "runtime_identity_mismatch"}
    assert receiver.link_for(identity.pool_instance_id) is None


def test_duplicate_live_pool_link_is_rejected_without_replacing_original(receiver):
    identity = _runtime_identity()
    token = receiver.issue_pool(identity)
    first_conn, first_link = _connect_pool(receiver, identity, token)
    second_conn, second_ack = _raw_pool_hello(receiver, _pool_hello(identity, token))
    second_conn.close()
    try:
        assert second_ack == {"ok": False, "error": "runtime_identity_mismatch"}
        assert receiver.link_for(identity.pool_instance_id) is first_link
        assert first_link.alive is True
    finally:
        first_conn.close()


def test_revoke_pool_instance_only_closes_target_link(receiver):
    identity_a = _runtime_identity(pool_id="pool-v1::alpha", pool_instance_id=_UUID_A)
    identity_b = _runtime_identity(pool_id="pool-v1::beta", pool_instance_id=_UUID_B)
    conn_a, link_a = _connect_pool(receiver, identity_a, receiver.issue_pool(identity_a))
    conn_b, link_b = _connect_pool(receiver, identity_b, receiver.issue_pool(identity_b))
    try:
        receiver.revoke_pool_instance(_UUID_A)
        assert receiver.link_for(_UUID_A) is None
        assert link_a.alive is False
        assert receiver.link_for(_UUID_B) is link_b
    finally:
        conn_a.close()
        conn_b.close()


def test_revoke_pool_and_run_have_scoped_effects(receiver):
    run_a_pool_a = _runtime_identity(
        run_id="run-a", pool_id="pool-v1::alpha", pool_instance_id=_UUID_A
    )
    run_a_pool_b = _runtime_identity(
        run_id="run-a", pool_id="pool-v1::beta", pool_instance_id=_UUID_B
    )
    run_b_pool_a = _runtime_identity(
        run_id="run-b",
        pool_id="pool-v1::alpha",
        pool_instance_id="33333333-3333-4333-8333-333333333333",
    )
    opened = []
    try:
        for expected in (run_a_pool_a, run_a_pool_b, run_b_pool_a):
            opened.append(_connect_pool(receiver, expected, receiver.issue_pool(expected))[0])

        receiver.revoke_pool("pool-v1::beta")
        assert receiver.link_for(_UUID_B) is None
        assert receiver.link_for(_UUID_A) is not None
        assert receiver.link_for(run_b_pool_a.pool_instance_id) is not None

        receiver.revoke_run("run-a")
        assert receiver.link_for(_UUID_A) is None
        assert receiver.link_for(run_b_pool_a.pool_instance_id) is not None
    finally:
        for conn in opened:
            conn.close()


def test_receiver_shutdown_wakes_pool_waiter(receiver):
    identity = _runtime_identity()
    receiver.issue_pool(identity)
    observed = []
    finished = threading.Event()

    def wait_for_pool():
        try:
            receiver.wait_pool(identity.pool_instance_id, timeout=30.0)
        except Exception as exc:  # asserted below from the waiting thread
            observed.append(exc)
        finally:
            finished.set()

    waiter = threading.Thread(target=wait_for_pool, daemon=True)
    waiter.start()
    time.sleep(0.05)
    receiver.stop()

    assert finished.wait(2.0) is True
    assert len(observed) == 1
    assert isinstance(observed[0], cr.ControlError)
    assert str(observed[0]) == "control_receiver_stopped"
