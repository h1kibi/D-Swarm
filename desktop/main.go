// The P6 desktop shell: a Wails window over the muteki backend + Next deck.
// The window loads the embedded redirect page, which points at the deck's
// dev/prod server on :3001 (the deck is the React UI — this shell wraps it).
package main

import (
	"context"
	"embed"
	"log"
	"os"
	"strconv"

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
	root := RepoRoot(exe, cwd)
	backendPort := envInt("MUTEKI_BACKEND_PORT", 8000)
	uiPort := envInt("MUTEKI_UI_PORT", 3001)
	uiMode := os.Getenv("MUTEKI_UI_MODE") // "" → dev

	log.Printf("muteki desktop: repo=%s backend=:%d ui=:%d mode=%s",
		root, backendPort, uiPort, orDefault(uiMode, "dev"))

	// one-time bootstrap: the Next deck needs its node_modules.
	if err := EnsureUiDeps(root); err != nil {
		log.Printf("[ui] npm install failed (the deck UI may not start): %v", err)
	}

	sup := &Supervisor{
		Backend: BackendService(root, os.Getenv("MUTEKI_PYTHON"), backendPort),
		UI:      UiService(root, uiPort, backendPort, uiMode),
	}
	app := &App{sup: sup, root: root}

	// Closing the window quits the app (no tray in wails v2.13's public API):
	// OnShutdown stops BOTH child services (backend + UI), so no orphans.
	err := wails.Run(&options.App{
		Title:     "Muteki — Command Deck",
		Width:     1280,
		Height:    800,
		MinWidth:  960,
		MinHeight: 640,
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

func orDefault(v, d string) string {
	if v == "" {
		return d
	}
	return v
}
