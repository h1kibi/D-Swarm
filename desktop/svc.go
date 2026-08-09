// Package desktop — the P6 desktop shell: a Wails window over the dswarm
// backend (FastAPI/uvicorn) + the Next.js command deck.
//
// svc.go is the child-process supervisor: spawn / health-wait / stop the two
// services (uvicorn backend on :8000, Next deck on :3001). Pure Go, no Wails
// dependency, so it is unit-testable with fake commands.
package main

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// ServiceState is the observable lifecycle of one child service.
type ServiceState string

const (
	StateStopped  ServiceState = "stopped"
	StateStarting ServiceState = "starting"
	StateRunning  ServiceState = "running"
	StateStopping ServiceState = "stopping"
	StateError    ServiceState = "error"
)

// Snapshot is what the UI/tray/logs consume (JSON-friendly).
type Snapshot struct {
	Name     string       `json:"name"`
	State    ServiceState `json:"state"`
	Port     int          `json:"port"`
	LastErr  string       `json:"lastErr,omitempty"`
	LogTail  []string     `json:"logTail"`
	LogCount int          `json:"logCount"`
}

// Service runs ONE child process with health polling and log capture.
type Service struct {
	Name          string
	Argv          []string // argv[0] resolved by exec.LookPath at Start (npm.cmd etc.)
	Dir           string
	ExtraEnv      []string // KEY=VALUE appended to os.Environ()
	HealthURL     string   // GET probe; empty = no health wait
	HealthTTL     time.Duration
	LogCap        int
	ReuseExisting bool // adopt an already healthy service on the configured endpoint

	mu       sync.Mutex
	cmd      *exec.Cmd
	managed  bool // true only when this Service started the process
	state    ServiceState
	lastErr  string
	logTail  []string
	logCount int
}

func (s *Service) setState(st ServiceState) {
	s.mu.Lock()
	s.state = st
	s.mu.Unlock()
}

func (s *Service) statusLocked() Snapshot {
	return Snapshot{Name: s.Name, State: s.state, Port: portOf(s.HealthURL),
		LastErr: s.lastErr, LogTail: append([]string(nil), s.logTail...), LogCount: s.logCount}
}

// Status returns a copy of the service's observable state.
func (s *Service) Status() Snapshot {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.statusLocked()
}

func (s *Service) log(line string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.logCount++
	if s.LogCap <= 0 {
		s.LogCap = 200
	}
	s.logTail = append(s.logTail, line)
	if len(s.logTail) > s.LogCap {
		s.logTail = s.logTail[len(s.logTail)-s.LogCap:]
	}
	fmt.Printf("[%s] %s\n", s.Name, line)
}

// Start spawns the child, then (when HealthURL is set) waits for it to answer
// HTTP 200 up to HealthTTL. Returns once the process is up (or health-ready).
func (s *Service) Start(ctx context.Context) error {
	s.mu.Lock()
	if s.state == StateRunning || s.state == StateStarting {
		s.mu.Unlock()
		return fmt.Errorf("%s already running", s.Name)
	}
	s.state = StateStarting
	s.lastErr = ""
	s.managed = false
	s.mu.Unlock()

	// The desktop shell may be started while run.sh web (or another desktop
	// instance) already owns the configured endpoint. Reuse a healthy D-Swarm
	// service instead of launching a second process that immediately dies with
	// EADDRINUSE. The adopted process is deliberately not killed on shutdown.
	if s.ReuseExisting && s.HealthURL != "" && healthy(s.HealthURL, 750*time.Millisecond) {
		s.mu.Lock()
		s.state = StateRunning
		s.managed = false
		s.mu.Unlock()
		s.log(fmt.Sprintf("reusing existing healthy service at %s", s.HealthURL))
		return nil
	}

	bin, err := exec.LookPath(s.Argv[0])
	if err != nil {
		s.setState(StateError)
		s.mu.Lock()
		s.lastErr = fmt.Sprintf("binary %q not found: %v", s.Argv[0], err)
		s.mu.Unlock()
		return fmt.Errorf("%s: %w", s.Name, err)
	}
	cmd := exec.Command(bin, s.Argv[1:]...)
	cmd.Dir = s.Dir
	cmd.Env = append(os.Environ(), s.ExtraEnv...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		s.setState(StateError)
		return err
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		s.setState(StateError)
		return err
	}
	if err := cmd.Start(); err != nil {
		s.setState(StateError)
		s.mu.Lock()
		s.lastErr = err.Error()
		s.mu.Unlock()
		return err
	}
	s.mu.Lock()
	s.cmd = cmd
	s.managed = true
	s.mu.Unlock()

	go pumpLines(stdout, s.log)
	go pumpLines(stderr, s.log)
	go func() {
		_ = cmd.Wait()
		s.mu.Lock()
		was := s.state
		s.cmd = nil
		s.managed = false
		s.mu.Unlock()
		if was == StateRunning || was == StateStarting {
			s.setState(StateStopped)
			s.log(fmt.Sprintf("process exited (was %s)", was))
		}
	}()

	if s.HealthURL != "" {
		deadline := time.Now().Add(s.healthTTL())
		for time.Now().Before(deadline) {
			if err := ctx.Err(); err != nil {
				_ = s.Stop()
				return err
			}
			s.mu.Lock()
			processGone := s.cmd == nil
			processErr := s.lastErr
			s.mu.Unlock()
			if processGone {
				// Another supervisor can win the bind race between our initial
				// probe and cmd.Start. If that winner is healthy, adopt it rather
				// than reporting a false startup failure.
				if s.ReuseExisting && healthy(s.HealthURL, 750*time.Millisecond) {
					s.mu.Lock()
					s.state = StateRunning
					s.managed = false
					s.mu.Unlock()
					s.log(fmt.Sprintf("adopted service that won the startup bind race at %s", s.HealthURL))
					return nil
				}
				if processErr == "" {
					processErr = "process exited before health probe became ready"
				}
				s.setState(StateError)
				s.mu.Lock()
				s.lastErr = processErr
				s.mu.Unlock()
				return fmt.Errorf("%s: %s", s.Name, processErr)
			}
			if healthy(s.HealthURL, 2*time.Second) {
				break
			}
			time.Sleep(500 * time.Millisecond)
		}
		if !healthy(s.HealthURL, 2*time.Second) {
			// A failed health start must not leave a child process behind. This
			// was a source of subsequent port conflicts after a failed boot.
			_ = s.Stop()
			s.setState(StateError)
			s.mu.Lock()
			s.lastErr = fmt.Sprintf("health probe %s never returned 200", s.HealthURL)
			s.mu.Unlock()
			return fmt.Errorf("%s: health probe failed", s.Name)
		}
	}
	s.setState(StateRunning)
	return nil
}

func (s *Service) healthTTL() time.Duration {
	if s.HealthTTL > 0 {
		return s.HealthTTL
	}
	return 180 * time.Second
}

// Stop terminates the child AND its whole tree (npm → node → next; python →
// uvicorn workers). Windows: taskkill /T; POSIX: negative process group.
func (s *Service) Stop() error {
	s.mu.Lock()
	cmd := s.cmd
	managed := s.managed
	if !managed {
		s.state = StateStopped
		s.mu.Unlock()
		return nil
	}
	if cmd == nil || cmd.Process == nil {
		s.mu.Unlock()
		return nil
	}
	pid := cmd.Process.Pid
	s.state = StateStopping
	s.mu.Unlock()

	s.log(fmt.Sprintf("stopping (pid %d, tree)", pid))
	if os.PathSeparator == '\\' {
		_ = exec.Command("taskkill", "/PID", fmt.Sprint(pid), "/T", "/F").Run()
	} else {
		_ = exec.Command("kill", "-TERM", fmt.Sprint(pid)).Run()
	}
	// wait for the reaper goroutine to observe the exit
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		s.mu.Lock()
		gone := s.cmd == nil
		s.mu.Unlock()
		if gone {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	s.mu.Lock()
	if s.cmd != nil {
		_ = s.cmd.Process.Kill()
	}
	s.state = StateStopped
	s.mu.Unlock()
	return nil
}

func pumpLines(r io.Reader, emit func(string)) {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for sc.Scan() {
		emit(sc.Text())
	}
}

func healthy(url string, timeout time.Duration) bool {
	client := &http.Client{Timeout: timeout}
	resp, err := client.Get(url)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, io.LimitReader(resp.Body, 4096))
	return resp.StatusCode == 200
}

func portOf(url string) int {
	if i := strings.LastIndex(url, ":"); i >= 0 {
		var p int
		if _, err := fmt.Sscanf(url[i+1:], "%d", &p); err == nil {
			return p
		}
	}
	return 0
}

// Supervisor owns the two services and their start ordering (backend before UI).
type Supervisor struct {
	Backend *Service
	UI      *Service
	mu      sync.Mutex
	started bool
}

func (s *Supervisor) StartAll(ctx context.Context) error {
	s.mu.Lock()
	if s.started {
		s.mu.Unlock()
		return nil
	}
	s.mu.Unlock()
	if err := s.Backend.Start(ctx); err != nil {
		return fmt.Errorf("backend: %w", err)
	}
	if err := s.UI.Start(ctx); err != nil {
		_ = s.Backend.Stop()
		return fmt.Errorf("ui: %w", err)
	}
	s.mu.Lock()
	s.started = true
	s.mu.Unlock()
	return nil
}

func (s *Supervisor) StopAll() {
	_ = s.UI.Stop()
	_ = s.Backend.Stop()
	s.mu.Lock()
	s.started = false
	s.mu.Unlock()
}

func (s *Supervisor) Status() map[string]Snapshot {
	return map[string]Snapshot{
		"backend": s.Backend.Status(),
		"ui":      s.UI.Status(),
	}
}

// RepoRoot resolves a usable D-Swarm checkout.  It deliberately validates both
// the Python project marker and the deck package: accepting an arbitrary cwd
// makes npm/Next inherit the drive root and Watchpack then tries to watch C:\.
func RepoRoot(exe string, cwd string) (string, error) {
	if v := strings.TrimSpace(os.Getenv("DSWARM_REPO_ROOT")); v != "" {
		if root, ok := validRepoRoot(v); ok {
			return root, nil
		}
		return "", fmt.Errorf("DSWARM_REPO_ROOT is not a D-Swarm checkout: %s", v)
	}

	starts := []string{cwd}
	if exe != "" {
		starts = append(starts, filepath.Dir(exe))
	}
	seen := make(map[string]struct{})
	for _, start := range starts {
		for dir := strings.TrimSpace(start); dir != ""; {
			absolute, err := filepath.Abs(dir)
			if err != nil {
				break
			}
			key := filepath.Clean(absolute)
			if _, duplicate := seen[key]; !duplicate {
				seen[key] = struct{}{}
				if root, ok := validRepoRoot(key); ok {
					return root, nil
				}
			}
			parent := filepath.Dir(key)
			if parent == key { // volume root
				break
			}
			dir = parent
		}
	}
	return "", fmt.Errorf("could not find a D-Swarm checkout from cwd %q or executable %q; set DSWARM_REPO_ROOT", cwd, exe)
}

func validRepoRoot(dir string) (string, bool) {
	if strings.TrimSpace(dir) == "" {
		return "", false
	}
	root, err := filepath.Abs(dir)
	if err != nil {
		return "", false
	}
	if _, err := os.Stat(filepath.Join(root, "pyproject.toml")); err != nil {
		return "", false
	}
	if _, err := os.Stat(filepath.Join(root, "apps", "web", "ui", "package.json")); err != nil {
		return "", false
	}
	return root, true
}

// BackendService builds the uvicorn service for the repo root.
func BackendService(root string, python string, port int) *Service {
	if python == "" {
		python = "python"
		venv := filepath.Join(root, ".venv", "Scripts", "python.exe")
		if _, err := os.Stat(venv); err == nil {
			python = venv
		}
	}
	return &Service{
		Name:          "backend",
		Argv:          []string{python, "-m", "uvicorn", "apps.web.server:create_app", "--host", "127.0.0.1", "--port", fmt.Sprint(port)},
		Dir:           root,
		HealthURL:     fmt.Sprintf("http://127.0.0.1:%d/api/runs", port),
		ReuseExisting: true,
		ExtraEnv:      []string{"DSWARM_BACKEND_PORT=" + fmt.Sprint(port)},
	}
}

// UiService builds the Next deck service. The desktop defaults to a production
// Next server; development mode is opt-in with DSWARM_UI_MODE=dev.
// DSWARM_BACKEND points the deck's /api proxy at OUR backend (default 8000 is
// what next.config assumes, but the port override must reach the right one).
func UiService(root string, port int, backendPort int, mode string) *Service {
	uiDir := filepath.Join(root, "apps", "web", "ui")
	argv := []string{"npm.cmd", "run", "start", "--", "-p", fmt.Sprint(port)}
	extra := []string{"DSWARM_UI_PORT=" + fmt.Sprint(port),
		"DSWARM_BACKEND=http://127.0.0.1:" + fmt.Sprint(backendPort)}
	if strings.EqualFold(strings.TrimSpace(mode), "dev") {
		argv = []string{"npm.cmd", "run", "dev", "--", "-p", fmt.Sprint(port)}
	}
	return &Service{
		Name:          "ui",
		Argv:          argv,
		Dir:           uiDir,
		HealthURL:     fmt.Sprintf("http://127.0.0.1:%d/", port),
		HealthTTL:     240 * time.Second, // first Next compile can take a while
		ReuseExisting: true,
		ExtraEnv:      extra,
	}
}

// uiDir validates the deck directory before any child process is launched.
func uiDir(root string) (string, error) {
	dir := filepath.Join(root, "apps", "web", "ui")
	if _, err := os.Stat(filepath.Join(dir, "package.json")); err != nil {
		return "", fmt.Errorf("deck UI is missing at %s: %w", dir, err)
	}
	return dir, nil
}

// EnsureUiDeps runs npm install in the ui dir when node_modules is missing.
func EnsureUiDeps(root string) error {
	uiDir, err := uiDir(root)
	if err != nil {
		return err
	}
	if _, err := os.Stat(filepath.Join(uiDir, "node_modules")); err == nil {
		return nil
	}
	fmt.Println("[ui] node_modules missing — running npm install (one-time)")
	cmd := exec.Command("npm.cmd", "install", "--no-audit", "--no-fund")
	cmd.Dir = uiDir
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

// EnsureUiBuild materializes a production Next build. next start refuses a dev
// .next directory, so this also gives a clear startup error rather than a proxy
// that appears alive but cannot serve the deck.
func EnsureUiBuild(root string) error {
	uiDir, err := uiDir(root)
	if err != nil {
		return err
	}
	if _, err := os.Stat(filepath.Join(uiDir, ".next", "BUILD_ID")); err == nil {
		return nil
	}
	fmt.Println("[ui] production build missing — running npm run build (one-time)")
	cmd := exec.Command("npm.cmd", "run", "build")
	cmd.Dir = uiDir
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}
