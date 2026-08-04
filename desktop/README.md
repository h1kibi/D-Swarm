# desktop — P6 Wails shell (optional milestone)

A native Windows shell over the muteki backend + the Next.js command deck:
the window loads the deck (React UI), and the Go side supervises the two
services — FastAPI/uvicorn (`:8000`) and Next (`:3001`) — as child processes.

```
+------------------------------+
| Wails window (WebView2)      |
|  └ redirect page → deck :3001|
+--------------+---------------+
               | spawn / health / stop (taskkill /T tree-kill)
+--------------v---------------+
| Supervisor (desktop/svc.go)  |
|  ├ BackendService: uvicorn apps.web.server --port 8000
|  └ UiService:     npm run dev -- -p 3001   (prod: next start)
+------------------------------+
```

## Build

Prereqs: Go ≥1.26, Node ≥20, Wails CLI (`go install
github.com/wailsapp/wails/v2/cmd/wails@latest`), WebView2 runtime (Windows 10/11
default). The deck UI's `node_modules` is bootstrapped automatically on first
run (npm install, one-time).

```bat
desktop\build.bat          :: wails build → desktop\build\bin\muteki-desktop.exe
```

## Run

```bat
build\bin\muteki-desktop.exe
```

Env knobs (all optional):

| var | default | meaning |
|---|---|---|
| `MUTEKI_REPO_ROOT` | auto (exe walk-up / cwd) | repo root (pyproject.toml) |
| `MUTEKI_BACKEND_PORT` | 8000 | FastAPI port |
| `MUTEKI_UI_PORT` | 3001 | deck port |
| `MUTEKI_UI_MODE` | dev | `prod` = `next build` + `next start` |
| `MUTEKI_PYTHON` | `.venv\Scripts\python.exe` | python for uvicorn |

Closing the window stops both services (tree-kill — no orphans). Backend and
UI logs stream to the console (build with `-windowsconsole` for debugging).

## Verification (as performed)

- `go test ./desktop/...` — spawn/health/stop state machine with fake commands.
- Real run: window titled "Muteki — Command Deck"; `:8000/api/runs` → 200;
  `:3001` → 200 (deck HTML); `CloseMainWindow()` → clean exit, both ports
  freed, no orphan python/node processes.

## Notes / limits

- Wails v2.13's classic API has no public tray registration — closing the
  window quits (no close-to-tray). The deck itself is the full run manager
  (SSE, blackboard, HITL) — the shell only owns process lifecycle.
- `npm run dev -- -p <port>` passes two `-p` flags (the script pins 3001);
  Next honors the last one — cosmetic only.
- The compose stack (`docker compose up`) remains the server deployment; this
  shell is the bare-host/desktop alternative.
