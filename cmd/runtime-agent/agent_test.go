package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"testing"
	"time"
)

// fakeHost is a test stand-in for the host control receiver. It listens on a local
// TCP port; when the supervisor dials in and sends Hello, it validates the token and
// drives commands on that connection. This mirrors the reverse-connect topology
// without docker — the supervisor logic (fork/stream/signal) is what's exercised.
type fakeHost struct {
	ln     net.Listener
	token  string
	mu     sync.Mutex
	conn   net.Conn
	enc    *json.Encoder
	r      *bufio.Reader
	hello  Hello
	reqSeq int64
	frames chan Frame // every frame the supervisor sends, fanned out to tests
}

func newFakeHost(t *testing.T, token string) *fakeHost {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	h := &fakeHost{ln: ln, token: token, frames: make(chan Frame, 1024)}
	t.Cleanup(func() {
		ln.Close()
		if h.conn != nil {
			h.conn.Close()
		}
	})
	return h
}

func (h *fakeHost) addr() string { return h.ln.Addr().String() }

// accept waits for the supervisor to dial in, completes the Hello handshake, and
// starts reading frames into h.frames. Returns the Hello it received.
func (h *fakeHost) accept(t *testing.T) Hello {
	t.Helper()
	conn, err := h.ln.Accept()
	if err != nil {
		t.Fatal(err)
	}
	h.conn = conn
	h.enc = json.NewEncoder(conn)
	h.r = bufio.NewReader(conn)
	line, err := h.r.ReadBytes('\n')
	if err != nil {
		t.Fatalf("read hello: %v", err)
	}
	if err := json.Unmarshal(line, &h.hello); err != nil {
		t.Fatalf("bad hello: %v", err)
	}
	ok := h.token == "" || h.hello.Token == h.token
	_ = h.enc.Encode(HelloAck{OK: ok, Error: errIf(!ok, "unauthorized")})
	if !ok {
		conn.Close()
		return h.hello
	}
	go func() {
		for {
			b, err := h.r.ReadBytes('\n')
			if len(b) > 0 {
				var f Frame
				if json.Unmarshal(b, &f) == nil {
					h.frames <- f
				}
			}
			if err != nil {
				return
			}
		}
	}()
	return h.hello
}

func (h *fakeHost) send(t *testing.T, req Request) int64 {
	t.Helper()
	h.mu.Lock()
	h.reqSeq++
	req.ReqID = h.reqSeq
	id := req.ReqID
	err := h.enc.Encode(req)
	h.mu.Unlock()
	if err != nil {
		t.Fatalf("send: %v", err)
	}
	return id
}

// waitFrame blocks for a frame matching pred (or fails after timeout).
func (h *fakeHost) waitFrame(t *testing.T, pred func(Frame) bool, timeout time.Duration) Frame {
	t.Helper()
	deadline := time.After(timeout)
	for {
		select {
		case f := <-h.frames:
			if pred(f) {
				return f
			}
		case <-deadline:
			t.Fatal("timed out waiting for frame")
		}
	}
}

func errIf(c bool, s string) string {
	if c {
		return s
	}
	return ""
}

func validSupervisorEnv(token string) map[string]string {
	return map[string]string{
		"DSWARM_RUN_ID":           "run-a",
		"DSWARM_POOL_ID":          "pool-v1::abc",
		"DSWARM_POOL_INSTANCE_ID": "11111111-1111-4111-8111-111111111111",
		"DSWARM_POOL_GENERATION":  "3",
		"DSWARM_CONTROL_TOKEN":    token,
	}
}

func TestHelloV2CarriesPoolInstanceIdentity(t *testing.T) {
	got := Hello{
		ProtocolVersion: 2,
		RunID:           "run-a",
		PoolID:          "pool-v1::abc",
		PoolInstanceID:  "11111111-1111-4111-8111-111111111111",
		Generation:      3,
		Token:           "opaque",
	}
	raw, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	var wire map[string]any
	if err := json.Unmarshal(raw, &wire); err != nil {
		t.Fatal(err)
	}
	if wire["protocol_version"] != float64(2) || wire["generation"] != float64(3) {
		t.Fatalf("bad hello: %s", raw)
	}
	if wire["pool_id"] != "pool-v1::abc" || wire["pool_instance_id"] != "11111111-1111-4111-8111-111111111111" {
		t.Fatalf("missing pool identity: %s", raw)
	}
	if _, legacy := wire["hello"]; legacy {
		t.Fatalf("legacy hello marker leaked into v2: %s", raw)
	}
}

func TestSupervisorIdentityFromEnvAcceptsCanonicalV2Identity(t *testing.T) {
	got, err := supervisorIdentityFromEnv(validSupervisorEnv("opaque"))
	if err != nil {
		t.Fatal(err)
	}
	if got.ProtocolVersion != 2 || got.RunID != "run-a" || got.PoolID != "pool-v1::abc" ||
		got.PoolInstanceID != "11111111-1111-4111-8111-111111111111" || got.Generation != 3 || got.Token != "opaque" {
		t.Fatalf("unexpected identity: %+v", got)
	}
}

func TestSupervisorIdentityFromEnvReadsTokenFile(t *testing.T) {
	tokenPath := filepath.Join(t.TempDir(), "control-token")
	if err := os.WriteFile(tokenPath, []byte("opaque-file\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	env := validSupervisorEnv("")
	delete(env, "DSWARM_CONTROL_TOKEN")
	env["DSWARM_CONTROL_TOKEN_FILE"] = tokenPath
	got, err := supervisorIdentityFromEnv(env)
	if err != nil {
		t.Fatal(err)
	}
	if got.Token != "opaque-file" {
		t.Fatalf("unexpected token from file: %q", got.Token)
	}
}

func TestSupervisorIdentityFromEnvRejectsMalformedOrMissingV2Identity(t *testing.T) {
	tests := []struct {
		name   string
		field  string
		value  string
		remove bool
	}{
		{name: "missing run", field: "DSWARM_RUN_ID", remove: true},
		{name: "missing pool", field: "DSWARM_POOL_ID", remove: true},
		{name: "malformed uuid", field: "DSWARM_POOL_INSTANCE_ID", value: "not-a-uuid"},
		{name: "uppercase uuid", field: "DSWARM_POOL_INSTANCE_ID", value: "11111111-1111-4111-8111-11111111111A"},
		{name: "non-v4 uuid", field: "DSWARM_POOL_INSTANCE_ID", value: "11111111-1111-1111-8111-111111111111"},
		{name: "zero generation", field: "DSWARM_POOL_GENERATION", value: "0"},
		{name: "negative generation", field: "DSWARM_POOL_GENERATION", value: "-1"},
		{name: "noncanonical generation", field: "DSWARM_POOL_GENERATION", value: "03"},
		{name: "nonnumeric generation", field: "DSWARM_POOL_GENERATION", value: "three"},
		{name: "missing token", field: "DSWARM_CONTROL_TOKEN", remove: true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			env := validSupervisorEnv("opaque")
			if tc.remove {
				delete(env, tc.field)
			} else {
				env[tc.field] = tc.value
			}
			if _, err := supervisorIdentityFromEnv(env); err == nil {
				t.Fatal("expected identity validation error")
			}
		})
	}
}

// startSupervisorDialing runs a supervisor that dials the fake host in a background
// goroutine (the dial blocks until the host accepts). Safe to call as `go
// startSupervisorDialing(...)`: it uses t.Errorf (goroutine-safe) not t.Fatal.
func startSupervisorDialing(t *testing.T, host *fakeHost, runID, token string) {
	t.Helper()
	t.Setenv("DSWARM_WORKER_DRAIN_GRACE_SECONDS", "1")
	ws := filepath.Join(t.TempDir(), "workspace")
	if err := os.MkdirAll(ws, 0o755); err != nil {
		t.Errorf("mkdir workspace: %v", err)
		return
	}
	s := &supervisor{
		protocolVersion: 2,
		runID:           runID,
		poolID:          "pool-v1::test",
		poolInstanceID:  "11111111-1111-4111-8111-111111111111",
		generation:      1,
		token:           token,
		workspace:       ws,
		workers:         map[string]*worker{},
	}
	conn := s.dialHost(host.addr(), 5*time.Second)
	if conn == nil {
		t.Errorf("supervisor could not dial fake host")
		return
	}
	s.serve(conn)
}

func fileSize(p string) int64 {
	fi, err := os.Stat(p)
	if err != nil {
		return 0
	}
	return fi.Size()
}

func TestHelloHandshakeAndStartWorker(t *testing.T) {
	host := newFakeHost(t, "tok123")
	// supervisor dials in a goroutine; host accepts.
	go startSupervisorDialing(t, host, "run-A", "tok123")
	hello := host.accept(t)
	if hello.ProtocolVersion != 2 || hello.RunID != "run-A" || hello.PoolID != "pool-v1::test" ||
		hello.PoolInstanceID != "11111111-1111-4111-8111-111111111111" || hello.Generation != 1 || hello.Token != "tok123" {
		t.Fatalf("bad hello: %+v", hello)
	}

	// StartWorker: echo two lines.
	reqID := host.send(t, Request{Op: OpStartWorker, Spec: &WorkerSpec{
		Argv: []string{"sh", "-c", "echo hello-stream; echo line2"}, Cwd: "/tmp", TimeoutSec: 10,
	}})
	started := host.waitFrame(t, func(f Frame) bool { return f.T == "started" && f.ReqID == reqID }, 5*time.Second)
	if started.WorkerID == "" || started.Error != "" {
		t.Fatalf("bad started: %+v", started)
	}
	wid := started.WorkerID

	var gotHello, gotLine2, gotExit bool
	deadline := time.After(8 * time.Second)
	for !gotExit {
		select {
		case f := <-host.frames:
			if f.WorkerID != wid {
				continue
			}
			switch f.T {
			case "out":
				if f.Line == "hello-stream" {
					gotHello = true
				}
				if f.Line == "line2" {
					gotLine2 = true
				}
			case "exit":
				gotExit = true
				if f.Rc != 0 {
					t.Fatalf("bad exit rc=%d", f.Rc)
				}
			}
		case <-deadline:
			t.Fatal("no exit frame")
		}
	}
	if !gotHello || !gotLine2 {
		t.Fatalf("missing stdout lines hello=%v line2=%v", gotHello, gotLine2)
	}
}

func TestSignalKill(t *testing.T) {
	host := newFakeHost(t, "")
	go startSupervisorDialing(t, host, "run-K", "")
	host.accept(t)

	reqID := host.send(t, Request{Op: OpStartWorker, Spec: &WorkerSpec{
		Argv: []string{"sh", "-c", "echo started; sleep 60"}, Cwd: "/tmp", TimeoutSec: 120,
	}})
	started := host.waitFrame(t, func(f Frame) bool { return f.T == "started" && f.ReqID == reqID }, 5*time.Second)
	wid := started.WorkerID
	host.waitFrame(t, func(f Frame) bool { return f.WorkerID == wid && f.T == "out" && f.Line == "started" }, 5*time.Second)

	// KILL it.
	host.send(t, Request{Op: OpSignal, WorkerID: wid, Signal: "KILL"})

	exit := host.waitFrame(t, func(f Frame) bool { return f.WorkerID == wid && f.T == "exit" }, 6*time.Second)
	if exit.Signalled != 9 {
		t.Fatalf("expected SIGKILL(9), got signalled=%d rc=%d", exit.Signalled, exit.Rc)
	}
}

func TestStopContPauseResume(t *testing.T) {
	host := newFakeHost(t, "")
	go startSupervisorDialing(t, host, "run-P", "")
	host.accept(t)

	reqID := host.send(t, Request{Op: OpStartWorker, Spec: &WorkerSpec{
		Argv: []string{"sh", "-c", "echo up; sleep 30"}, Cwd: "/tmp", TimeoutSec: 60,
	}})
	started := host.waitFrame(t, func(f Frame) bool { return f.T == "started" && f.ReqID == reqID }, 5*time.Second)
	wid := started.WorkerID
	host.waitFrame(t, func(f Frame) bool { return f.WorkerID == wid && f.T == "out" }, 5*time.Second)

	check := func(sig string, wantPaused bool) {
		host.send(t, Request{Op: OpSignal, WorkerID: wid, Signal: sig})
		host.waitFrame(t, func(f Frame) bool { return f.T == "resp" && f.OK }, 3*time.Second)
		statReq := host.send(t, Request{Op: OpStatus, WorkerID: wid})
		st := host.waitFrame(t, func(f Frame) bool { return f.T == "resp" && f.ReqID == statReq }, 3*time.Second)
		if st.Paused != wantPaused {
			t.Fatalf("after %s: paused=%v want %v (state=%s)", sig, st.Paused, wantPaused, st.State)
		}
	}
	check("STOP", true)
	check("CONT", false)
	host.send(t, Request{Op: OpSignal, WorkerID: wid, Signal: "KILL"})
}

// TestDrainKillsSurvivingGrandchildren: when the worker's MAIN process exits
// normally, its still-running grandchildren (an untimed background loop) must be
// SIGKILLed after the drain grace instead of leaking.
func TestDrainKillsSurvivingGrandchildren(t *testing.T) {
	t.Setenv("DSWARM_WORKER_DRAIN_GRACE_SECONDS", "1")
	marker := filepath.Join(t.TempDir(), "marker")
	w, events, err := startWorker("t", &WorkerSpec{
		Argv: []string{"sh", "-c",
			fmt.Sprintf("while true; do date >> %s; sleep 0.1; done & echo hi; exit 0", marker)},
		Cwd: "/tmp", TimeoutSec: 60,
	})
	if err != nil {
		t.Fatal(err)
	}
	gotExit := false
	for ev := range events {
		if ev.T == "exit" {
			gotExit = true
			break
		}
	}
	if !gotExit {
		t.Fatal("no exit frame")
	}
	// grace (1s) + margin so the SIGKILL has landed.
	time.Sleep(2300 * time.Millisecond)
	size1 := fileSize(marker)
	time.Sleep(600 * time.Millisecond)
	size2 := fileSize(marker)
	if size2 != size1 {
		t.Fatalf("grandchild still running after drain (marker grew %d -> %d)", size1, size2)
	}
	_ = syscall.Kill(-w.pgid, syscall.SIGKILL) // best-effort cleanup
}

// TestWallClockTimeoutKillsGroup: the supervisor's wall-clock cap SIGKILLs the
// whole group, so a runaway command dies at the budget even without a per-command
// timeout.
func TestWallClockTimeoutKillsGroup(t *testing.T) {
	t.Setenv("DSWARM_WORKER_DRAIN_GRACE_SECONDS", "1")
	marker := filepath.Join(t.TempDir(), "marker")
	_, events, err := startWorker("t", &WorkerSpec{
		Argv: []string{"sh", "-c",
			fmt.Sprintf("while true; do date >> %s; sleep 0.1; done", marker)},
		Cwd: "/tmp", TimeoutSec: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	var exit Frame
	for ev := range events {
		if ev.T == "exit" {
			exit = ev
			break
		}
	}
	if !exit.TimedOut {
		t.Fatalf("expected timed_out exit, got %+v", exit)
	}
	size1 := fileSize(marker)
	time.Sleep(600 * time.Millisecond)
	size2 := fileSize(marker)
	if size2 != size1 {
		t.Fatalf("grandchild survived wall-clock timeout kill (marker grew %d -> %d)", size1, size2)
	}
}

// TestSweepKillsDetachedGrandchild: a grandchild that setsid'd away from the
// worker's pgid is invisible to the pgid drain; the orphan sweep's alive-time
// descendant snapshot must still kill it after the worker exits.
func TestSweepKillsDetachedGrandchild(t *testing.T) {
	t.Setenv("DSWARM_WORKER_DRAIN_GRACE_SECONDS", "1")
	marker := filepath.Join(t.TempDir(), "marker")
	w, events, err := startWorker("t", &WorkerSpec{
		Argv: []string{"sh", "-c",
			fmt.Sprintf("setsid sh -c 'while true; do date >> %s; sleep 0.1; done' & echo hi; sleep 0.5; exit 0", marker)},
		Cwd: "/tmp", TimeoutSec: 60,
	})
	if err != nil {
		t.Fatal(err)
	}
	s := &supervisor{workers: map[string]*worker{"w": w}}
	// Wait until the detached writer is actually running (it appends to the
	// marker) before snapshotting descendants, so the sweep records it.
	deadline := time.Now().Add(3 * time.Second)
	for fileSize(marker) == 0 && time.Now().Before(deadline) {
		time.Sleep(50 * time.Millisecond)
	}
	if fileSize(marker) == 0 {
		t.Fatal("detached grandchild never started (marker missing)")
	}
	s.sweepOnce() // snapshot descendants while the worker is alive
	gotExit := false
	for ev := range events {
		if ev.T == "exit" {
			gotExit = true
			break
		}
	}
	if !gotExit {
		t.Fatal("no exit frame")
	}
	s.sweepOnce() // kill the recorded detached descendant
	time.Sleep(500 * time.Millisecond)
	size1 := fileSize(marker)
	time.Sleep(600 * time.Millisecond)
	size2 := fileSize(marker)
	if size2 != size1 {
		t.Fatalf("detached grandchild survived the orphan sweep (marker grew %d -> %d)", size1, size2)
	}
}

func TestHealth(t *testing.T) {
	host := newFakeHost(t, "")
	go startSupervisorDialing(t, host, "run-H", "")
	host.accept(t)
	reqID := host.send(t, Request{Op: OpHealth})
	f := host.waitFrame(t, func(f Frame) bool { return f.T == "resp" && f.ReqID == reqID }, 3*time.Second)
	if !f.OK || f.Version != agentVersion {
		t.Fatalf("bad health: %+v", f)
	}
}

func TestTokenRejected(t *testing.T) {
	host := newFakeHost(t, "right")
	// supervisor dials with the WRONG token → host rejects in accept().
	done := make(chan struct{})
	go func() {
		ws := filepath.Join(t.TempDir(), "ws")
		_ = os.MkdirAll(ws, 0o755)
		s := &supervisor{
			protocolVersion: 2,
			runID:           "run-X",
			poolID:          "pool-v1::test",
			poolInstanceID:  "11111111-1111-4111-8111-111111111111",
			generation:      1,
			token:           "wrong",
			workspace:       ws,
			workers:         map[string]*worker{},
		}
		conn := s.dialHost(host.addr(), 3*time.Second)
		if conn != nil {
			t.Errorf("dial should have failed on bad token")
		}
		close(done)
	}()
	hello := host.accept(t)
	if hello.Token != "wrong" {
		t.Fatalf("expected wrong token in hello, got %q", hello.Token)
	}
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("supervisor did not abort on rejected hello")
	}
}

func TestSeedWorkspaceDocsIdempotent(t *testing.T) {
	ws := filepath.Join(t.TempDir(), "workspace")
	if err := os.MkdirAll(ws, 0o755); err != nil {
		t.Fatal(err)
	}
	dst := filepath.Join(ws, "CLAUDE.md")
	if err := os.WriteFile(dst, []byte("worker-edited"), 0o644); err != nil {
		t.Fatal(err)
	}
	s := &supervisor{workspace: ws, workers: map[string]*worker{}}
	s.seedWorkspaceDocs() // /opt/dswarm absent → must NOT clobber dst
	got, _ := os.ReadFile(dst)
	if string(got) != "worker-edited" {
		t.Fatalf("seed clobbered worker-edited file: %q", got)
	}
}
