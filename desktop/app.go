package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
)

// App is the Wails binding surface: the embedded page / future deck code can
// inspect and control the two services through these methods.
type App struct {
	sup     *Supervisor
	root    string
	ctx     context.Context
	lastErr string
}

func (a *App) boot() {
	if err := a.sup.StartAll(a.ctx); err != nil {
		a.lastErr = err.Error()
		log.Printf("services failed to start: %v", err)
	}
}

// ── bindings ────────────────────────────────────────────────────────────────

// Status returns {backend: Snapshot, ui: Snapshot}.
func (a *App) Status() map[string]Snapshot {
	return a.sup.Status()
}

func (a *App) LastError() string { return a.lastErr }

func (a *App) Restart() error {
	a.sup.StopAll()
	return a.sup.StartAll(context.Background())
}

func (a *App) BackendPort() int { return portOf(a.sup.Backend.HealthURL) }

func (a *App) UiPort() int { return portOf(a.sup.UI.HealthURL) }

// OpenSessionsDir reveals the sessions dir in the host file manager.
func (a *App) OpenSessionsDir() string {
	dir := filepath.Join(a.root, "sessions")
	_ = os.MkdirAll(dir, 0o755)
	_ = execSh(fmt.Sprintf(`explorer "%s"`, dir))
	return dir
}

func execSh(cmd string) error {
	return exec.Command("cmd", "/c", cmd).Start()
}
