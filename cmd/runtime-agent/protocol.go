package main

// Runtime Control Plane wire protocol: newline-delimited JSON over one reverse
// connection per pool instance. It remains standard-library-only so the
// supervisor can ship as a single static binary.
//
// The host-side Python receiver in dswarm/solver/control_receiver.py validates
// the same frames; protocol changes must remain synchronized across both sides.
//
// Topology (reverse-connect, forward-control):
//   - The supervisor opens no listening port. It dials the host receiver and sends
//     {protocol_version, run_id, pool_id, pool_instance_id, generation, token}.
//   - The host validates the frozen pool-generation identity and token, then sends
//     HelloAck.
//   - The established connection multiplexes every worker in that pool instance
//     through WorkerID-tagged request, response, and stream frames.
//
// Reverse-connect keeps the worker from driving a supervisor network service and
// lets all pool containers share one host receiver port. The token prevents a
// stray or stale container from attaching to the wrong pool generation; it is not
// a security boundary against the trusted in-container worker.

// Op codes (host → supervisor).
const (
	OpStartWorker = "StartWorker"
	OpSignal      = "Signal"
	OpStatus      = "Status"
	OpTeardownRun = "TeardownRun"
	OpHealth      = "Health"
)

// Hello is the FIRST frame the supervisor sends after dialing the host receiver.
// RCP v2 routes links by immutable pool-generation identity, not merely by run_id.
type Hello struct {
	ProtocolVersion int    `json:"protocol_version"`
	RunID           string `json:"run_id"`
	PoolID          string `json:"pool_id"`
	PoolInstanceID  string `json:"pool_instance_id"`
	Generation      int    `json:"generation"`
	Token           string `json:"token"`
	Version         string `json:"version,omitempty"`
}

// HelloAck is the host's reply to Hello.
type HelloAck struct {
	OK    bool   `json:"ok"`
	Error string `json:"error,omitempty"`
}

// Request is one command frame the HOST sends on the established connection.
type Request struct {
	Op    string `json:"op"`
	ReqID int64  `json:"req_id"` // correlation id; the reply echoes it

	// StartWorker
	Spec *WorkerSpec `json:"spec,omitempty"`

	// Signal / Status — the worker to act on.
	WorkerID string `json:"worker_id,omitempty"`
	// Signal — one of "STOP" | "CONT" | "TERM" | "KILL".
	Signal string `json:"signal,omitempty"`
}

// WorkerSpec is everything the supervisor needs to fork+exec a worker.
type WorkerSpec struct {
	Argv []string `json:"argv"` // resolved container-side argv (argv[0] = bin)
	Cwd  string   `json:"cwd"`  // absolute path inside the container
	// Env overlays the worker's environment (NOT the supervisor's). Only keys the
	// host chooses to pass arrive here; the supervisor adds nothing of its own
	// except a sane PATH/HOME default if absent.
	Env map[string]string `json:"env,omitempty"`
	// TimeoutSec is the authoritative wall-clock cap; the supervisor SIGKILLs the
	// worker tree at this many seconds (mirrors the old in-container `timeout -s KILL`).
	TimeoutSec int `json:"timeout_sec"`
	// Tag is an opaque per-worker label the host uses for its own bookkeeping; the
	// supervisor echoes it back in the Started reply for correlation.
	Tag string `json:"tag,omitempty"`
}

// Frame is the tagged union the supervisor sends back on the connection. Exactly one
// of the embedded shapes is meaningful, keyed by T. All carry ReqID (which command
// they answer) and, for worker output, WorkerID (which worker on the multiplexed
// connection).
//
//	T == "started"  -> StartWorker reply: WorkerID set (or Error on spawn failure)
//	T == "out"|"err" -> one raw line of worker stdout/stderr (Line), WorkerID set
//	T == "exit"     -> worker terminated: Rc/OOM/TimedOut/Signalled, WorkerID set
//	T == "resp"     -> generic Response payload (Signal/Status/Teardown/Health)
type Frame struct {
	T        string `json:"t"`
	ReqID    int64  `json:"req_id"`
	WorkerID string `json:"worker_id,omitempty"`
	Tag      string `json:"tag,omitempty"`

	// t == "out" | "err"
	Line string `json:"line,omitempty"`

	// t == "started"
	Error string `json:"error,omitempty"`

	// t == "exit"
	Rc        int  `json:"rc,omitempty"`
	OOM       bool `json:"oom,omitempty"`
	TimedOut  bool `json:"timed_out,omitempty"`
	Signalled int  `json:"signalled,omitempty"`

	// t == "resp" (Signal / Status / TeardownRun / Health)
	OK      bool   `json:"ok,omitempty"`
	State   string `json:"state,omitempty"` // Status: running | exited | timed_out | oom | unknown
	RcPtr   *int   `json:"rc_ptr,omitempty"`
	Paused  bool   `json:"paused,omitempty"`
	Version string `json:"version,omitempty"` // Health
	Workers int    `json:"workers,omitempty"` // Health: running worker count
	Uptime  int64  `json:"uptime_sec,omitempty"`
}
