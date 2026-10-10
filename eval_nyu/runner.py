"""eval_nyu/runner.py — per-challenge, per-engine benchmark runner (route A, P5).

Reproduces the June-2026 NYU CTF Bench eval as an in-repo, CLI-driven harness:

  python -m eval_nyu --dataset <manifest-or-nyu-root> --engines pi \
      --limit 3 --budget 300 --out eval_nyu/results/<tag>.jsonl --report

Design:
- ONE engine per challenge run (attribution is the point): the ReasonSwarm
  runs one configured engine per challenge.
- pi runs through the P3 container stack (ctf-swarm-pi-<cat> image + host
  model gateway + task token); claude/codex/cursor run local (and are
  skipped with a note when the CLI binary is absent).
- The harness OWNS the target and the ground truth:
    static  — an HTTP server the harness starts, serving the flag verbatim
    docker  — builds the challenge Dockerfile, runs it with the ground-truth
              flag injected (GZCTF_FLAG / FLAG env), maps a free host port;
              the worker reaches it via host.docker.internal:<port>
  The oracle then verifies byte-for-byte against that ground truth.
- Results append to a JSONL (resume-safe: already-measured (challenge,engine)
  pairs are skipped); the report generator consumes the same file.
"""
from __future__ import annotations

import asyncio
import functools
import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval_nyu.oracle import verify  # noqa: E402

# ── data model ───────────────────────────────────────────────────────────────

@dataclass
class EvalChallenge:
    challenge_id: str
    name: str
    category: str = "misc"
    description: str = ""
    flag_format: str = r"flag\{[^}]+\}"
    ground_truth: "list[str]" = field(default_factory=list)
    target: str = ""                      # worker-facing URL (host.docker.internal:...)
    deploy: dict = field(default_factory=dict)   # {"kind": "static"|"docker", ...}
    files: "list[str]" = field(default_factory=list)  # attachment paths (host-side)


@dataclass
class EvalResult:
    challenge_id: str
    engine: str
    category: str = ""
    solved: bool = False
    flags: "list[str]" = field(default_factory=list)
    matched: str = ""
    detail: str = ""                      # stop reason / oracle detail
    elapsed_s: float = 0.0
    tokens: int = 0
    cost_usd: float = 0.0
    ts: float = field(default_factory=time.time)
    note: str = ""                        # engine-missing / deploy-failure etc.


# ── dataset loaders ──────────────────────────────────────────────────────────

def load_local_manifest(path: "str | Path") -> "list[EvalChallenge]":
    """Local manifest format: a JSON list (or {challenges: [...]}) of full specs:
      {challenge_id, name, category, description, flag_format, ground_truth: [..],
       deploy: {kind: "static"|"docker", dir, port, flag_env: ["GZCTF_FLAG","FLAG"]},
       files: [...]}
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = raw.get("challenges") if isinstance(raw, dict) else raw
    out: list[EvalChallenge] = []
    for e in entries or []:
        e = dict(e)
        e.setdefault("challenge_id", str(e.get("name", "")).strip() or None)
        if not e.get("challenge_id"):
            raise ValueError(f"manifest entry missing challenge_id: {e!r}")
        gt = e.get("ground_truth") or []
        if isinstance(gt, str):
            gt = [gt]
        out.append(EvalChallenge(
            challenge_id=str(e["challenge_id"]),
            name=str(e.get("name") or e["challenge_id"]),
            category=str(e.get("category") or "misc"),
            description=str(e.get("description") or ""),
            flag_format=str(e.get("flag_format") or r"flag\{[^}]+\}"),
            ground_truth=[str(t) for t in gt],
            target=str(e.get("target") or ""),
            deploy=dict(e.get("deploy") or {}),
            files=[str(f) for f in (e.get("files") or [])],
        ))
    return out


def load_nyu_dataset(root: "str | Path") -> "list[EvalChallenge]":
    """NYU CTF Bench layout: <root>/test_dataset.json lists challenge entries;
    each entry's challenge dir holds challenge.json {name, category, description,
    flag, files}. Attachments are staged into the worker cwd; service challenges
    (docker-compose.yml present) are deployed via `docker compose up` with the
    ground-truth flag injected."""
    root = Path(root)
    index = root / "test_dataset.json"
    if not index.exists():
        raise FileNotFoundError(f"NYU dataset index not found: {index}")
    entries = json.loads(index.read_text(encoding="utf-8"))
    if isinstance(entries, dict):
        entries = entries.get("challenges") or entries.get("test") or []
    out: list[EvalChallenge] = []
    for ent in entries:
        cid = str(ent.get("challenge_id") or ent.get("id") or ent.get("name") or "")
        if not cid:
            continue
        ch_dir = root / cid
        ch_file = ch_dir / "challenge.json"
        meta: dict = {}
        if ch_file.exists():
            meta = json.loads(ch_file.read_text(encoding="utf-8"))
        gt = meta.get("flag") or ent.get("flag") or ""
        gt_list = [gt] if gt else []
        if isinstance(meta.get("flags"), list):
            gt_list = [str(f) for f in meta["flags"]]
        files = [str(ch_dir / f) for f in (meta.get("files") or ent.get("files") or [])]
        deploy: dict = {"kind": "static"}
        if (ch_dir / "docker-compose.yml").exists() or (ch_dir / "docker-compose.yaml").exists():
            deploy = {"kind": "docker", "dir": str(ch_dir), "port": 80,
                      "flag_env": ["GZCTF_FLAG", "FLAG"]}
        out.append(EvalChallenge(
            challenge_id=cid,
            name=str(meta.get("name") or ent.get("name") or cid),
            category=str(meta.get("category") or ent.get("category") or "misc"),
            description=str(meta.get("description") or ent.get("description") or ""),
            flag_format=str(meta.get("flag_format") or r"flag\{[^}]+\}"),
            ground_truth=gt_list,
            deploy=deploy,
            files=[f for f in files if Path(f).exists()],
        ))
    return out


def load_dataset(spec: "str | Path") -> "list[EvalChallenge]":
    spec = Path(spec)
    if spec.is_dir():
        return load_nyu_dataset(spec)
    return load_local_manifest(spec)


# ── engine availability ──────────────────────────────────────────────────────

_ENGINE_BIN = {"pi": "pi", "claude": "claude", "codex": "codex", "cursor": "cursor-agent"}


def engine_available(engine: str) -> "tuple[bool, str]":
    """Local-run engines need their CLI on PATH; pi runs IN the worker container
    (the P3 stack) so the host binary is not required for it."""
    if engine == "pi":
        return True, "container backend (ctf-swarm-pi image + model gateway)"
    bin_name = _ENGINE_BIN.get(engine, engine)
    found = shutil.which(bin_name)
    if found:
        return True, f"local backend ({found})"
    return False, f"CLI '{bin_name}' not found on PATH — engine skipped"


# ── target lifecycle ─────────────────────────────────────────────────────────

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _TargetHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D401
        pass


def start_static_target(challenge: EvalChallenge) -> "tuple[threading.Thread, str, int]":
    """Serve a tiny page carrying the ground-truth flag; returns (thread, url, port)."""
    root = Path(challenge.deploy.get("dir") or ROOT / "eval_nyu" / "_static_target")
    root.mkdir(parents=True, exist_ok=True)
    flag = (challenge.ground_truth or ["flag{missing_ground_truth}"])[0]
    (root / "index.html").write_text(
        "<html><body><h1>eval target</h1>"
        f"<p>FLAG: {flag}</p></body></html>", encoding="utf-8")

    class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    port = int(challenge.deploy.get("port") or _free_port())
    httpd = _Server(("0.0.0.0", port),
                    functools.partial(_TargetHandler, directory=str(root)))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return t, f"http://host.docker.internal:{port}/", port


def _docker(*args: str, timeout: float = 600.0) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=timeout)


class DockerTarget:
    """Build + run a challenge Dockerfile with the ground-truth flag injected;
    teardown removes the container. The worker reaches it via host.docker.internal."""

    def __init__(self, challenge: EvalChallenge):
        self.challenge = challenge
        self.container = f"eval-{challenge.challenge_id.replace('/', '_')}"
        self.image = f"eval-img-{challenge.challenge_id.replace('/', '_')}"
        self.port = 0

    def up(self) -> str:
        """Returns the worker-facing target URL. Raises RuntimeError on failure."""
        ch = self.challenge
        d = Path(ch.deploy.get("dir") or "")
        if not d.exists():
            raise RuntimeError(f"docker target dir missing: {d}")
        flag = (ch.ground_truth or ["flag{missing_ground_truth}"])[0]
        self._down(silent=True)  # a leftover from a killed run must not linger
        build = _docker("build", "-q", "-t", self.image, str(d))
        if build.returncode != 0:
            raise RuntimeError(f"docker build failed: {build.stderr.strip()[:300]}")
        self.port = int(ch.deploy.get("port") or _free_port())
        host_port = int(ch.deploy.get("host_port") or _free_port())
        envs = []
        for name in ch.deploy.get("flag_env") or ["GZCTF_FLAG", "FLAG"]:
            envs += ["-e", f"{name}={flag}"]
        run = _docker("run", "-d", "--name", self.container, *envs,
                      "-p", f"{host_port}:{self.port}", self.image)
        if run.returncode != 0:
            self._down(silent=True)
            raise RuntimeError(f"docker run failed: {run.stderr.strip()[:300]}")
        # wait for HTTP 200 (bounded)
        import urllib.request
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{host_port}/", timeout=2) as resp:
                    if resp.status == 200:
                        self.port = host_port
                        return f"http://host.docker.internal:{host_port}/"
            except Exception:
                pass
            time.sleep(1.0)
        self._down(silent=True)
        raise RuntimeError(f"target at :{host_port} never answered HTTP 200")

    def _down(self, *, silent: bool) -> None:
        r = _docker("rm", "-f", self.container, timeout=30)
        if not silent and r.returncode != 0:
            print(f"  [warn] docker rm -f {self.container}: {r.stderr.strip()[:200]}",
                  flush=True)

    def down(self) -> None:
        self._down(silent=False)


# ── gateway usage aggregation ────────────────────────────────────────────────

# deepseek-v4-flash list prices, USD per 1M tokens (mirrors the worker image's
# models-store.json) — used to price the per-challenge gateway usage ledger.
_FLASH_PRICE_IN = 0.14
_FLASH_PRICE_OUT = 0.28
_FLASH_PRICE_CACHE_READ = 0.0028


def _gateway_usage_summary(out_root: Path) -> "tuple[int, float]":
    """Sum the model-gateway usage ledgers written under out_root for the run:
    (tokens, cost_usd). The ledgers are authoritative for the pi worker's real
    upstream usage (the coordinator's CostController only sees ITS own calls)."""
    total_tokens = 0
    total_cost = 0.0
    for p in out_root.rglob("*-gateway-usage.jsonl"):
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                u = row.get("usage") or {}
                inp = int(u.get("prompt_tokens") or 0)
                outp = int(u.get("completion_tokens") or 0)
                cached = int(u.get("prompt_cache_hit_tokens")
                             or (u.get("prompt_tokens_details") or {}).get("cached_tokens")
                             or 0)
                total_tokens += inp + outp
                miss = max(0, inp - cached)
                total_cost += (
                    miss * _FLASH_PRICE_IN + cached * _FLASH_PRICE_CACHE_READ
                    + outp * _FLASH_PRICE_OUT) / 1_000_000.0
        except Exception:
            continue
    return total_tokens, round(total_cost, 6)


# ── the swarm run (one engine, one challenge) ────────────────────────────────

def _build_challenge_model(ch: EvalChallenge, *, workdir: Path) -> Any:
    from dswarm.models.solve_graph import Challenge
    attachments = []
    for f in ch.files:
        src = Path(f)
        if not src.exists():
            continue
        dst = workdir / src.name
        try:
            shutil.copy2(src, dst)
            attachments.append(str(dst))
        except OSError:
            pass
    return Challenge(
        id=ch.challenge_id,
        name=ch.name,
        category=ch.category,
        points=100,
        description=ch.description or f"Solve {ch.name} and recover the flag.",
        target=ch.target,
        flag_format=ch.flag_format,
        expected_flags=1,
        attachments=attachments,
    )


async def run_challenge(
    ch: EvalChallenge,
    engine: str,
    *,
    out_root: Path,
    budget_s: int,
    sessions_root: Path,
    credential_accounts_root: "Optional[str]" = None,
    worker_backend: str = "container",
) -> EvalResult:
    """One engine on one challenge. Mirrors scripts/smoke_pi_container.py's
    verified swarm wiring (single engine with clean attribution)."""
    from dswarm.core.cost import CostController
    from dswarm.core.event_bus import EventBus
    from dswarm.core.llm import LLMClient
    from dswarm.sandbox.manager import SandboxManager
    from dswarm.solver.result import ArtifactStore
    from dswarm.swarm.swarm import Swarm

    t0 = time.time()
    root = out_root / ch.challenge_id
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    model = _build_challenge_model(ch, workdir=root)
    bus = EventBus()
    llm = LLMClient()
    sandbox = SandboxManager(root=root / "sbx")
    arts = ArtifactStore(root=root / "arts")
    cost = CostController()
    sw = Swarm(
        model,
        llm=llm, sandbox=sandbox, bus=bus, cost=cost, artifacts=arts,
        run_id=f"eval-{ch.challenge_id}",
        executor="cli",
        engines=[engine],
        web_access=True,
        max_workers=2,
        worker_root=root / "workspace" / "workers",
        graph_dir=root / "workspace" / "graph",
        credential_accounts_root=credential_accounts_root,
        worker_backend=worker_backend,
        reason_model="deepseek-v4-flash",
        wall_clock_budget=max(30, int(budget_s)),
        barren_limit=3,
    )
    try:
        outcome = await sw.run()
    finally:
        try:
            snap = cost.snapshot()
        except Exception:
            snap = {}
        # teardown the run container whatever happened (mirrors the smoke script)
        try:
            from dswarm.solver.container_exec import teardown_container
            teardown_container(f"eval-{ch.challenge_id}", remove=True)
        except Exception:
            pass
    solved, matched, detail = verify(
        list(outcome.flags or []), ch.ground_truth, flag_format=ch.flag_format)
    # pi worker usage lands in the model-gateway ledger (the coordinator's
    # CostController only prices ITS OWN deepseek calls) — aggregate it.
    tok, usd = _gateway_usage_summary(root)
    if not tok:
        tok = int(snap.get("global_tokens") or 0)
    if not usd:
        usd = round(float(snap.get("global_usd") or 0.0), 6)
    return EvalResult(
        challenge_id=ch.challenge_id,
        engine=engine,
        category=ch.category,
        solved=solved,
        flags=list(outcome.flags or []),
        matched=matched,
        detail=detail or str(getattr(outcome, "reason", "") or ""),
        elapsed_s=round(time.time() - t0, 2),
        tokens=tok,
        cost_usd=usd,
    )


# ── top-level driver ─────────────────────────────────────────────────────────

def _already_measured(path: Path) -> set:
    seen: set = set()
    if not path.exists():
        return seen
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            seen.add((row.get("challenge_id"), row.get("engine")))
        except Exception:
            pass
    return seen


async def _run_all(
    challenges: "list[EvalChallenge]",
    engines: "list[str]",
    *,
    out_path: Path,
    budget_s: int,
    sessions_root: Path,
    credential_accounts_root: "Optional[str]",
    deploy: bool,
) -> "list[EvalResult]":
    results: list[EvalResult] = []
    done = _already_measured(out_path)
    for engine in engines:
        ok, note = engine_available(engine)
        for ch in challenges:
            if (ch.challenge_id, engine) in done:
                print(f"  [skip] {ch.challenge_id} × {engine} (already measured)",
                      flush=True)
                continue
            print(f"== {ch.challenge_id} [{ch.category}] × {engine} ==", flush=True)
            if not ok:
                results.append(EvalResult(
                    challenge_id=ch.challenge_id, engine=engine,
                    note=f"engine unavailable: {note}"))
                continue
            deployer = None
            try:
                kind = (ch.deploy or {}).get("kind", "static")
                if kind == "docker" and deploy:
                    deployer = DockerTarget(ch)
                    ch.target = deployer.up()
                elif kind == "static":
                    if not ch.target:
                        t, url, port = start_static_target(ch)
                        ch.target = url
                        deployer = ("static", t, port)
                res = await run_challenge(
                    ch, engine, out_root=sessions_root / "eval",
                    budget_s=budget_s, sessions_root=sessions_root,
                    credential_accounts_root=credential_accounts_root,
                    worker_backend="container" if engine == "pi" else "local")
            except Exception as exc:  # noqa: BLE001
                res = EvalResult(
                    challenge_id=ch.challenge_id, engine=engine,
                    note=f"run/deploy failed: {str(exc)[:300]}")
            finally:
                if deployer is not None and not isinstance(deployer, tuple):
                    deployer.down()
                elif isinstance(deployer, tuple):
                    deployer[1].join(timeout=1.0)
            print(f"  -> {'SOLVED' if res.solved else 'unsolved'} "
                  f"({res.detail or res.note}) {res.elapsed_s:.0f}s "
                  f"cost=${res.cost_usd}", flush=True)
            results.append(res)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(res), ensure_ascii=False) + "\n")
    return results
