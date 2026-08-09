package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// fakeCmd returns a tiny script/binary that sleeps and can be killed — the
// stand-in for uvicorn/npm in unit tests.
func fakeCmd(t *testing.T, dir string) string {
	t.Helper()
	if runtime.GOOS == "windows" {
		bat := filepath.Join(dir, "fake.bat")
		os.WriteFile(bat, []byte("@echo off\r\nping -n 300 127.0.0.1 >NUL\r\n"), 0o644)
		return bat
	}
	sh := filepath.Join(dir, "fake.sh")
	os.WriteFile(sh, []byte("#!/bin/sh\nsleep 300\n"), 0o755)
	return sh
}

func TestServiceStartHealthAndStop(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
	}))
	defer srv.Close()

	dir := t.TempDir()
	svc := &Service{
		Name:      "fake",
		Argv:      []string{fakeCmd(t, dir)},
		Dir:       dir,
		HealthURL: srv.URL,
		HealthTTL: 10 * time.Second,
		LogCap:    16,
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := svc.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	st := svc.Status()
	if st.State != StateRunning {
		t.Fatalf("expected running, got %s", st.State)
	}
	if err := svc.Stop(); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	st = svc.Status()
	if st.State != StateStopped {
		t.Fatalf("expected stopped, got %s", st.State)
	}
}

func TestServiceReusesHealthyExistingService(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
	}))
	defer srv.Close()

	// A missing binary proves Start adopted the healthy endpoint rather than
	// spawning a second process. The adopted service must also survive Stop.
	svc := &Service{
		Name:          "existing",
		Argv:          []string{"definitely-not-a-real-binary-xyz"},
		HealthURL:     srv.URL,
		ReuseExisting: true,
	}
	if err := svc.Start(context.Background()); err != nil {
		t.Fatalf("Start should adopt healthy service: %v", err)
	}
	if st := svc.Status(); st.State != StateRunning {
		t.Fatalf("expected running adopted service, got %s", st.State)
	}
	if err := svc.Stop(); err != nil {
		t.Fatalf("Stop adopted service: %v", err)
	}
	if st := svc.Status(); st.State != StateStopped {
		t.Fatalf("expected stopped state after releasing adopted service, got %s", st.State)
	}
}

func TestUiServicePassesPortOnlyAsRuntimeArgument(t *testing.T) {
	dev := UiService(`C:\repo`, 4321, 8765, "dev")
	if got := strings.Join(dev.Argv, " "); got != "npm.cmd run dev -- -p 4321" {
		t.Fatalf("unexpected dev argv: %q", got)
	}
	for _, mode := range []string{"", "prod", "unexpected"} {
		svc := UiService(`C:\repo`, 4321, 8765, mode)
		if got := strings.Join(svc.Argv, " "); got != "npm.cmd run start -- -p 4321" {
			t.Fatalf("mode %q should use production argv, got %q", mode, got)
		}
	}
}

func TestServiceHealthTimeoutMarksError(t *testing.T) {
	// a health URL that never answers → Start must fail and mark error.
	dir := t.TempDir()
	svc := &Service{
		Name:      "fake",
		Argv:      []string{fakeCmd(t, dir)},
		Dir:       dir,
		HealthURL: "http://127.0.0.1:1/nope", // nothing listens there
		HealthTTL: 2 * time.Second,
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	err := svc.Start(ctx)
	if err == nil {
		t.Fatal("expected health-timeout error")
	}
	st := svc.Status()
	if st.State != StateError {
		t.Fatalf("expected error state, got %s", st.State)
	}
	if !strings.Contains(st.LastErr, "health probe") {
		t.Fatalf("unexpected LastErr: %q", st.LastErr)
	}
	_ = svc.Stop() // cleanup the stray child
}

func TestServiceMissingBinaryMarksError(t *testing.T) {
	svc := &Service{Name: "ghost", Argv: []string{"definitely-not-a-real-binary-xyz"}}
	err := svc.Start(context.Background())
	if err == nil {
		t.Fatal("expected start error for missing binary")
	}
	if svc.Status().State != StateError {
		t.Fatalf("expected error state, got %s", svc.Status().State)
	}
}

func TestSupervisorStartsBothAndStopsBoth(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
	}))
	defer srv.Close()
	dir := t.TempDir()
	sup := &Supervisor{
		Backend: &Service{Name: "backend", Argv: []string{fakeCmd(t, dir)},
			Dir: dir, HealthURL: srv.URL, HealthTTL: 10 * time.Second},
		UI: &Service{Name: "ui", Argv: []string{fakeCmd(t, dir)},
			Dir: dir, HealthURL: srv.URL, HealthTTL: 10 * time.Second},
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	if err := sup.StartAll(ctx); err != nil {
		t.Fatalf("StartAll: %v", err)
	}
	status := sup.Status()
	if status["backend"].State != StateRunning || status["ui"].State != StateRunning {
		t.Fatalf("both should be running: %+v", status)
	}
	sup.StopAll()
	status = sup.Status()
	if status["backend"].State != StateStopped || status["ui"].State != StateStopped {
		t.Fatalf("both should be stopped: %+v", status)
	}
}

func TestRepoRootResolution(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "pyproject.toml"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	uiDir := filepath.Join(root, "apps", "web", "ui")
	if err := os.MkdirAll(uiDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(uiDir, "package.json"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("DSWARM_REPO_ROOT", "")
	if got, err := RepoRoot("", filepath.Join(root, "desktop")); err != nil || got != root {
		t.Fatalf("cwd ancestor resolution failed: root=%q err=%v", got, err)
	}
	// explicit environment override wins only when it is a valid checkout.
	t.Setenv("DSWARM_REPO_ROOT", root)
	if got, err := RepoRoot("", t.TempDir()); err != nil || got != root {
		t.Fatalf("env override failed: root=%q err=%v", got, err)
	}
	// exe walk-up: <root>/desktop/build/bin/exe → root.
	t.Setenv("DSWARM_REPO_ROOT", "")
	bin := filepath.Join(root, "desktop", "build", "bin", "d-swarm-desktop.exe")
	if got, err := RepoRoot(bin, t.TempDir()); err != nil || got != root {
		t.Fatalf("exe walk-up failed: root=%q err=%v", got, err)
	}
	// Never silently return a non-repository cwd: doing so starts Next in an
	// invalid directory and can make Watchpack scan the Windows drive root.
	if _, err := RepoRoot("", t.TempDir()); err == nil {
		t.Fatal("expected an error when no valid D-Swarm checkout exists")
	}
	t.Setenv("DSWARM_REPO_ROOT", filepath.Join(root, "desktop"))
	if _, err := RepoRoot("", root); err == nil {
		t.Fatal("expected invalid DSWARM_REPO_ROOT to fail clearly")
	}
}

func TestPortOf(t *testing.T) {
	if got := portOf("http://127.0.0.1:8000/api/runs"); got != 8000 {
		t.Fatalf("portOf: %d", got)
	}
	if got := portOf("http://127.0.0.1:3001/"); got != 3001 {
		t.Fatalf("portOf: %d", got)
	}
}
