# desktop — P6 Wails shell (optional milestone)

A native Windows shell over the dswarm backend + the Next.js command deck:
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
|  └ UiService:     npm run start -- -p 3001 (dev: set DSWARM_UI_MODE=dev)
+------------------------------+
```

## Build

Prereqs: Go ≥1.26, Node ≥20, Wails CLI (`go install
github.com/wailsapp/wails/v2/cmd/wails@latest`), WebView2 runtime (Windows 10/11
default). The deck UI's `node_modules` is bootstrapped automatically on first
run (npm install, one-time).

```bat
desktop\build.bat          :: wails build → desktop\build\bin\d-swarm-desktop.exe
```

## Run

```bat
build\bin\d-swarm-desktop.exe
```

Env knobs (all optional):

| var | default | meaning |
|---|---|---|
| `DSWARM_REPO_ROOT` | auto (exe/cwd ancestor search) | repo root (requires `pyproject.toml` and `apps/web/ui/package.json`) |
| `DSWARM_BACKEND_PORT` | 8000 | FastAPI port |
| `DSWARM_UI_PORT` | 3001 | deck port |
| `DSWARM_UI_MODE` | prod | set `dev` only when developing with Next HMR; desktop normally uses `next start` |
| `DSWARM_PYTHON` | `.venv\Scripts\python.exe` | python for uvicorn |

Closing the window stops both services (tree-kill — no orphans). Backend and
UI logs stream to the console (build with `-windowsconsole` for debugging).

If the configured backend/UI endpoint is already healthy (for example, a service
started earlier with `run.sh web`), the desktop shell adopts it instead of
starting a duplicate. Adopted services are not killed when the shell closes.


## Verification (as performed)

- `go test ./desktop/...` — spawn/health/stop state machine with fake commands.
- Real run: window titled "D-Swarm"; `:8000/api/runs` → 200;
  `:3001` → 200 (deck HTML); `CloseMainWindow()` → clean exit, both ports
  freed, no orphan python/node processes.

## Notes / limits

- Wails v2.13's classic API has no public tray registration — closing the
  window quits (no close-to-tray). The deck itself is the full run manager
  (SSE, blackboard, HITL) — the shell only owns process lifecycle.
- The desktop supervisor passes the configured UI port once to Next; direct
  `npm run dev` uses Next's default port unless `-p <port>` is supplied.
- The compose stack (`docker compose up`) remains the server deployment; this
  shell is the bare-host/desktop alternative.
