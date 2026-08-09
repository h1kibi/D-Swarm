// The P6 desktop shell: a Wails window over the dswarm backend + Next deck.
// The window loads the embedded redirect page, which points at the deck's
// dev/prod server on :3001 (the deck is the React UI — this shell wraps it).
package main

import (
	"context"
	"embed"
	"log"
	"os"
	"strconv"
	"strings"

	"github.com/wailsapp/wails/v2"
	"github.com/wailsapp/wails/v2/pkg/options"
	"github.com/wailsapp/wails/v2/pkg/options/assetserver"
)

//go:embed all:frontend/dist
var assets embed.FS

func envInt(name string, def int) int {
	if raw := os.Getenv(name); raw != "" {
		if n, err := strconv.Atoi(raw); err == nil && n > 0 {
			return n
		}
	}
	return def
}

func main() {
	exe, _ := os.Executable()
	cwd, _ := os.Getwd()
	root, err := RepoRoot(exe, cwd)
	if err != nil {
		log.Fatalf("desktop startup aborted: %v", err)
	}
	backendPort := envInt("DSWARM_BACKEND_PORT", 8000)
	uiPort := envInt("DSWARM_UI_PORT", 3001)
	uiMode := "prod"
	if raw := os.Getenv("DSWARM_UI_MODE"); raw != "" {
		uiMode = raw
	}

	log.Printf("dswarm desktop: repo=%s backend=:%d ui=:%d mode=%s",
		root, backendPort, uiPort, uiMode)

	// Bootstrap the deck before spawning it. Production mode avoids Next's
	// Watchpack/HMR watcher in a desktop process; dev remains explicitly opt-in.
	if err := EnsureUiDeps(root); err != nil {
		log.Fatalf("desktop startup aborted: UI dependencies: %v", err)
	}
	if !strings.EqualFold(strings.TrimSpace(uiMode), "dev") {
		if err := EnsureUiBuild(root); err != nil {
			log.Fatalf("desktop startup aborted: UI production build: %v", err)
		}
	}

	sup := &Supervisor{
		Backend: BackendService(root, os.Getenv("DSWARM_PYTHON"), backendPort),
		UI:      UiService(root, uiPort, backendPort, uiMode),
	}
	app := &App{sup: sup, root: root}

	// Closing the window quits the app (no tray in wails v2.13's public API):
	// OnShutdown stops BOTH child services (backend + UI), so no orphans.
	err = wails.Run(&options.App{
		Title:       "D-Swarm",
		Width:       1280,
		Height:      800,
		MinWidth:    960,
		MinHeight:   640,
		AssetServer: &assetserver.Options{Assets: assets},
		OnStartup: func(ctx context.Context) {
			app.ctx = ctx
			go app.boot()
		},
		OnShutdown: func(ctx context.Context) { sup.StopAll() },
		Bind:       []interface{}{app},
	})
	if err != nil {
		log.Fatalf("wails: %v", err)
	}
}
