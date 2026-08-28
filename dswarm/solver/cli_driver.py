"""Shelled-CLI worker drivers — claude / codex as full agentic executors.

Why: the local DeepSeek code-driven kernel (one run_python tool-call per step)
lacks the execute→observe→refine depth to actually land an exploit. EXP-AB proved
a shelled `claude -p` solves challenges the code-driven swarm misses, and its flag
still passes the real provenance gate. So we delegate a focused intent to a CLI
agent that runs its OWN shell loop, and gate its output exactly as before.

Each driver is a thin per-CLI adapter: it builds the argv + manages a session id so
the single conclude-fallback turn (on a timeout) can resume the SAME session — there
is no multi-turn resume loop; a worker runs one execute pass and is then discarded.
We run bare-host against the
SUBSCRIPTION CLIs (full-strength model — the reason it solves). codex is included
but may be usage-limited; the swarm degrades to claude-only when a driver's
healthcheck fails.

This module is pure (builds argv + parses output); the solver runs the subprocess.
"""

from __future__ import annotations

import abc
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path

from dswarm.solver.endpoint_probe import probe_endpoint
from typing import Any, Callable, Optional

from dswarm.solver.worker_profiles import base_engine_for_profile, profile_uses_endpoint


# ── engine binary resolution ─────────────────────────────────────────────────
# A worker shells `subprocess.run(["claude", ...])`, which resolves the FIRST
# `claude` on PATH. On this host (and easily on others) that can be a BROKEN
# third-party repackage — e.g. `@cometix/claude-code`, a Node "restored" build
# that crashes at parse time (`SyntaxError: Unexpected identifier`) under an
# older Node, never reaching the CLI. A worker pointed at it dies before it can
# solve, and the healthcheck just sees a non-zero exit and silently degrades the
# swarm. So we DON'T trust bare PATH order: resolve each engine to a real,
# runnable OFFICIAL binary and pin it.
#
# Precedence:
#   1. explicit override  — env DSWARM_PI_BIN (operator wins)
#   2. known official install locations, in order
#   3. every `name` on PATH, skipping ones whose realpath looks like a known
#      bad repackage (cometix), taking the first that actually runs
#   4. bare `name` as a last resort (preserves old behavior if nothing else found)
_ENV_OVERRIDE = {
    "pi": "DSWARM_PI_BIN",
}

# Official / first-party install locations we trust, highest first. `~` expanded
# at resolve time. The local native installer and Homebrew cask are the two
# blessed macOS paths; /usr/local/bin covers a plain npm global on Linux.
_KNOWN_GOOD = {
    # pi ships as a self-contained binary; the official Windows install lives
    # under Program Files and is on PATH (btfly images bake it at /usr/local/bin).
    "pi": [
        "~/.local/bin/pi",
        "/opt/homebrew/bin/pi",
        "/usr/local/bin/pi",
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "pi-windows-x64", "pi.exe"),
    ],
}

# realpath substrings that mark a KNOWN-BAD repackage we must never select.
_BAD_REALPATH_MARKERS = ("@cometix", "cometix")

# Optional knowledge-base MCP. D-Swarm can let a worker query a KB MCP (your own
# security-intel / CVE / writeup index) as a first-class tool. There is no bundled
# KB service — set DSWARM_KB_MCP_NAME to the server key from your .mcp.json (and
# enable kb on the run) to use one. Empty (the default) means "no KB", so the
# whole KB path is inert out of the box.
KB_MCP_NAME = os.environ.get("DSWARM_KB_MCP_NAME", "").strip()


def _looks_bad(path: str) -> bool:
    try:
        real = os.path.realpath(path)
    except OSError:
        real = path
    low = real.lower()
    return any(m in low for m in _BAD_REALPATH_MARKERS)


def _runs_ok(path: str) -> bool:
    """Does this binary actually execute (vs crash at load like the cometix build)?
    `--version` is the cheapest probe that distinguishes a real CLI from a binary
    that dies before parsing argv."""
    try:
        r = subprocess.run([path, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def resolve_engine_bin(name: str) -> str:
    """Resolve an engine name to a pinned, runnable binary path (see precedence
    above). Falls back to the bare name so callers always get *something*."""
    # 1. operator override — trusted as-is (don't second-guess an explicit path)
    env = _ENV_OVERRIDE.get(name)
    if env and os.environ.get(env):
        return os.path.expanduser(os.environ[env])

    # 2. known-good install locations
    for cand in _KNOWN_GOOD.get(name, []):
        p = os.path.expanduser(cand)
        if Path(p).exists() and not _looks_bad(p) and _runs_ok(p):
            return p

    # 3. PATH scan, skipping known-bad repackages, first that runs wins.
    bin_basename = name
    for p in _which_all(bin_basename):
        if not _looks_bad(p) and _runs_ok(p):
            return p

    # 4. last resort — bare basename (old behavior). If everything is broken we
    #    at least fail the same way we used to, not worse.
    return bin_basename


def resolve_engine_bin_source(name: str) -> str:
    """Where would resolve_engine_bin() get this engine's binary from?

    Returns one of: "env" (explicit DSWARM_*_BIN override), "known-good" (a
    blessed install location), "path" (a PATH scan hit), or "fallback" (nothing
    found — bare name). Drives the FE's "you're on an unpinned default path,
    consider setting DSWARM_<ENGINE>_BIN" guidance for local mode.
    """
    env = _ENV_OVERRIDE.get(name)
    if env and os.environ.get(env):
        return "env"
    for cand in _KNOWN_GOOD.get(name, []):
        p = os.path.expanduser(cand)
        if Path(p).exists() and not _looks_bad(p) and _runs_ok(p):
            return "known-good"
    bin_basename = name
    for p in _which_all(bin_basename):
        if not _looks_bad(p) and _runs_ok(p):
            return "path"
    return "fallback"


def _which_all(name: str) -> list[str]:
    """Every `name` found on PATH, in PATH order (shutil.which only returns one)."""
    out: list[str] = []
    seen: set[str] = set()
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        if not d:
            continue
        cand = os.path.join(d, name)
        if cand not in seen and os.path.isfile(cand) and os.access(cand, os.X_OK):
            seen.add(cand)
            out.append(cand)
    # also let shutil.which have a say (handles PATHEXT etc.) as a backstop
    w = shutil.which(name)
    if w and w not in seen:
        out.append(w)
    return out


@dataclass
class CliResult:
    """One CLI run's outcome, normalized across engines."""
    text: str                       # the agent's final response / transcript tail
    invocation_id: Optional[str] = None  # stable id for this CLI invocation aggregate
    session: Optional[str] = None   # session id, for a resume/conclude turn
    cost_usd: Optional[float] = None
    # token usage for this run, when the engine reports it. None == not reported.
    # claude exposes it via the result `usage` block; codex via turn.completed
    # `usage`. Fed to the cost ledger so the deck can show a token-usage column
    # alongside the $ figure (and so codex — which no longer reports a dollar
    # cost — still gets priced from its tokens). cursor reports neither.
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    num_turns: Optional[int] = None
    elapsed_s: float = 0.0
    timed_out: bool = False
    # OOM-killed: the worker's process was SIGKILL'd by the kernel out-of-memory
    # killer (a sibling run's container ballooned and starved the Docker VM — no
    # per-container --memory limit). This looks IDENTICAL to a wall-clock timeout by
    # exit code alone (the in-container `timeout` wrapper propagates 128+9=137 for
    # BOTH a real timeout AND a SIGKILL'd child), so we discriminate by the cgroup
    # oom_kill counter delta and surface it as its OWN reason — a worker that died
    # at 60s with an empty transcript is an OOM victim, NOT a 2400s timeout, and
    # mislabeling it as "timeout" sent diagnosis down the wrong path.
    oom_killed: bool = False
    cancelled: bool = False         # killed by a cancel_event (winner found / abort)
    steered: bool = False           # ended early by a steer_event — END THIS PASS but
    #   KEEP the session id (operator hint/redirect). The worker does NOT resume on
    #   steered (no resume loop under single-shot); the guidance flows to the next
    #   spawned worker. Used only to avoid downgrading _session_established on the cut
    #   pass. Distinct from `cancelled` (= die).
    raw_stderr: str = ""
    runtime_status: dict = field(default_factory=dict)


class ProbeContractError(RuntimeError):
    """The driver cannot prove that its runtime Probe is tool-disabled."""


@dataclass(frozen=True)
class CliProbeSpec:
    """Safe, single-turn invocation description for a runtime readiness Probe."""

    argv: tuple[str, ...]
    prompt: str
    model: str
    session_dir: str
    disabled_tools: tuple[str, ...]
    non_agentic: bool
    requires_closed_stdin: bool
    max_output_bytes: int = 64 * 1024


@dataclass(frozen=True)
class CliProbeResult:
    """Sanitized result of parsing a tool-disabled Probe transcript."""

    ok: bool
    classification: str
    code: str
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    diagnostics: str = ""
    completed_event_type: Optional[str] = None


@dataclass
class StreamStep:
    """One live step parsed from a streaming CLI line — so the deck can show the
    worker thinking/acting in real time instead of a dead pause until it returns.

    kind:
      "reasoning"    — the agent's prose/thought (text block)        → REASONING_DELTA
      "tool"         — a tool/command the agent invoked              → TOOL_CALL
      "tool_result"  — that tool's output                            → TERMINAL_OUTPUT
      "session"      — the engine assigned/echoed a session id
    """
    kind: str
    text: str = ""
    tool: str = ""        # tool name (kind == "tool")
    session: str = ""     # session id (kind == "session")
    # FULL, UNTRUNCATED tool output (kind == "tool_result"). `text` is truncated to
    # 600 chars for the live deck display, but a flag/fact provenance gate MUST see
    # what the command actually printed — a flag past char 600 of a command's output
    # (or in a nested `ssh host '...'` whose remote stdout is forwarded here) is real
    # but invisible in `text` (run-75379 false-negative: the genuine DC flag04 was
    # read on a pivoted host, its output never landed in the truncated chunk or the
    # summarized CliResult.text). Empty for non-tool_result steps; callers fall back
    # to `text` when `raw` is unset.
    raw: str = ""         # untruncated tool output (kind == "tool_result")


class CliDriver(abc.ABC):
    """A thin per-CLI shelled-executor adapter."""
    name: str

    # resolved once, then cached — the actual binary this driver invokes. We pin
    # to a runnable OFFICIAL install instead of bare `self.name` so a broken
    # third-party `claude` earlier on PATH can't silently take over (see
    # resolve_engine_bin). Override via DSWARM_CLAUDE_BIN / DSWARM_CODEX_BIN.
    _bin: Optional[str] = None

    # Engines whose CLI blocks waiting on stdin (pi on Windows: `--mode json`
    # idles until stdin EOF) must run with stdin closed — the runner passes
    # DEVNULL instead of inheriting the parent's pipe. True for pi only.
    close_stdin: bool = False

    @property
    def bin(self) -> str:
        override = os.environ.get(_ENV_OVERRIDE.get(self.name, ""), "").strip()
        if override:
            return override
        if self._bin is None:
            self._bin = resolve_engine_bin(self.name)
        return self._bin

    def new_session(self) -> Optional[str]:
        """A pre-seeded session id, or None if the engine assigns one itself."""
        return None

    # The optional KB MCP (if configured via DSWARM_KB_MCP_NAME) is registered at
    # user scope and inherited by every worker; to run a worker WITHOUT it we deny
    # its mcp tools by server prefix. Empty name → no prefix → nothing to deny.
    KB_TOOL_PREFIX = f"mcp__{KB_MCP_NAME}" if KB_MCP_NAME else ""

    @abc.abstractmethod
    def build_execute(
        self,
        prompt: str,
        session: Optional[str],
        *,
        web_access: bool = True,
        kb_access: bool = True,
        stream: bool = False,
    ) -> list[str]:
        """argv for a fresh focused run.

        web_access=False → strip the agent's internet tools (WebSearch/WebFetch)
        so a bench eval can't be contaminated by looking up a writeup.
        kb_access=False → deny the inherited optional KB MCP tools (default: the
        worker keeps the user-scope KB, if one is configured via
        DSWARM_KB_MCP_NAME, and can dispatch to it).
        stream=True → emit one JSON event PER STEP (assistant text / tool call /
        tool result) as the run proceeds, so the deck shows live progress instead
        of a dead pause. parse_stream_line() turns each line into a StreamStep;
        parse() still produces the final CliResult from the accumulated stdout.
        """

    def parse_stream_line(self, line: str) -> Optional["StreamStep"]:
        """Turn ONE line of streaming stdout into a live StreamStep (or None to
        ignore it). Default: nothing streams. Overridden by streaming engines.

        Single-step view (the FIRST step of a line). Kept for callers/tests that want
        one representative step; the streaming runner uses parse_stream_steps() to get
        ALL steps so a multi-block message doesn't lose later blocks (#18)."""
        return None

    def parse_stream_steps(self, line: str) -> list["StreamStep"]:
        """ALL live StreamSteps a single line carries. A single assistant message can
        hold several content blocks (text + tool_use + more text); #18: returning only
        the FIRST block dropped any FOUND_FLAG / VERIFIED_FACT in a later block from
        LIVE propagation (it only resurfaced via the final parse()). Default: wrap the
        single-step parse_stream_line (correct for engines that emit at most one step
        per line, e.g. codex). claude + cursor override this to yield every block."""
        step = self.parse_stream_line(line)
        return [step] if step is not None else []

    @abc.abstractmethod
    def build_resume(
        self,
        prompt: str,
        session: str,
        *,
        web_access: bool = True,
        kb_access: bool = True,
        stream: bool = False,
    ) -> list[str]:
        """argv to resume `session` with a follow-up (conclude/refine) turn."""

    @abc.abstractmethod
    def parse(self, stdout: str, stderr: str) -> CliResult:
        """Normalize the engine's stdout into a CliResult."""

    def probe_spec(self, *, model: str, session_dir: str) -> CliProbeSpec:
        """Return a provably tool-disabled, one-turn runtime Probe."""
        raise ProbeContractError("tool_disabled_unprovable")

    def parse_probe_result(
        self, stdout: str, stderr: str, returncode: int
    ) -> CliProbeResult:
        """Parse a Probe transcript without exposing raw model/provider text."""
        raise ProbeContractError("probe_parser_unavailable")

    # ── self-check (FE-healthcheck-page) ─────────────────────────────────────
    # The deep probe sends ONE tiny prompt and waits for the engine to answer —
    # this is what actually exercises auth/quota (a `--version` only proves the
    # binary unpacks). All three engines share the same shape via _hello_argv()
    # so the self-check is symmetric: claude no longer the only one that really
    # talks to its backend while codex/cursor merely checked a version string.
    HELLO_PROMPT = "Reply with exactly: OK"
    _HELLO_TIMEOUT = 60      # one cold turn can take ~18s; leave generous headroom
    _HELLO_RETRIES = 1       # retry once on a transient miss before calling it dead

    def _hello_argv(self) -> list[str]:
        """argv for a minimal one-turn 'say hello' probe. Engines that can't run a
        real turn cheaply return [] (→ fall back to the `--version` liveness check)."""
        return []

    def _hello_ok(self, r: "subprocess.CompletedProcess") -> bool:
        """Did the hello turn actually produce a model reply? Default: exit 0 and
        SOME non-empty stdout. Engines with a structured envelope tighten this."""
        return r.returncode == 0 and bool((r.stdout or "").strip())

    def healthcheck(self, *, env: "dict[str, str] | None" = None) -> bool:
        """Cheap-but-real liveness probe — can this CLI complete a turn right now
        (auth + quota ok)? Returns bool for back-compat; health_detail() carries
        the human-readable reason."""
        # Only forward env when set, so a health_detail override/stub that predates
        # the env parameter (no **kwargs) still works through the bool entrypoint.
        if env is None:
            return self.health_detail()[0]
        return self.health_detail(env=env)[0]

    def health_detail(self, *, env: "dict[str, str] | None" = None) -> "tuple[bool, str]":
        """(healthy, detail). Sends a one-turn hello and retries once on a
        transient failure (a single cold/jittery miss shouldn't report red). The
        detail names the failure mode — timeout / non-zero exit / empty reply /
        not-found — so the self-check page can tell connectivity from auth/quota.

        `env`, when given, is the COMPLETE environment for the probe subprocess
        (callers build {**os.environ, **credential_overlay}). Passing it explicitly
        — instead of the old global os.environ overlay — is what makes concurrent
        probes safe: two engines probing in parallel no longer clobber each other's
        CURSOR_API_KEY/etc. None preserves the legacy inherit-os.environ behavior."""
        argv = self._hello_argv()
        if not argv:  # engine has no cheap dry-run → fall back to version liveness
            try:
                r = subprocess.run([self.bin, "--version"], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace", timeout=20, env=env)
                if r.returncode == 0:
                    return True, ""
                return False, "binary not runnable (--version failed)"
            except FileNotFoundError:
                return False, "binary not found on PATH"
            except subprocess.TimeoutExpired:
                return False, "version probe timed out"
            except Exception as e:  # noqa: BLE001
                return False, str(e)[:160]

        last = "no reply"
        for attempt in range(self._HELLO_RETRIES + 1):
            try:
                r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=self._HELLO_TIMEOUT, env=env,
                                   stdin=subprocess.DEVNULL if getattr(self, "close_stdin", False) else None)
            except FileNotFoundError:
                return False, "binary not found on PATH"
            except subprocess.TimeoutExpired:
                last = f"hello probe timed out (>{self._HELLO_TIMEOUT}s)"
            except Exception as e:  # noqa: BLE001
                last = str(e)[:160]
            else:
                if self._hello_ok(r):
                    return True, ""
                # classify the miss so a retry/the operator knows what happened
                if r.returncode != 0:
                    failed_detail = ""
                    if '"type":"turn.failed"' in (r.stdout or ""):
                        for line in reversed((r.stdout or "").splitlines()):
                            try:
                                ev = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if ev.get("type") != "turn.failed":
                                continue
                            err = ev.get("error") or {}
                            failed_detail = str(
                                err.get("message") if isinstance(err, dict) else err
                            )
                            break
                    detail_src = failed_detail or r.stderr or r.stdout or ""
                    tail = detail_src.strip().splitlines()
                    last = (f"hello exited {r.returncode}"
                            + (f": {tail[-1][:300]}" if tail else ""))
                else:
                    last = "hello returned no model reply"
            if attempt < self._HELLO_RETRIES:
                time.sleep(1.0)  # brief backoff, then one more shot
        return False, last


_FLAG_LINE = re.compile(r"FOUND_FLAG=\s*(\S+)")


class PiDriver(CliDriver):
    """`pi --mode json` — pi's single-shot json-event-stream mode: prints every
    session event as a JSON line to stdout and EXITS (docs/json.md). On Windows
    the CLI idles until stdin EOF, so `close_stdin` is set and the runner passes
    DEVNULL.

    Sessions: each worker stores its sessions under a RELATIVE `--session-dir`
    `.pi-sessions` — argv-relative, so it resolves inside the worker's own cwd and
    parallel workers never share session files. The first event is
    `{"type":"session","id":...}` — parse() surfaces that id as the session, and
    build_resume reuses it via `--session <id>` (falling back to `-c/--continue`
    when the id is unknown).

    Provider/model: `--provider` comes from DSWARM_PI_PROVIDER (default: unset →
    pi's own resolution: settings.json defaultProvider or env keys like
    DEEPSEEK_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY, or `pi /login`
    subscriptions); ProfileDriver injects `--model` via _with_model.
    """

    name = "pi"
    # pi's --mode json waits for stdin EOF on Windows before executing; the
    # runners must hand it DEVNULL, not the parent's (open) pipe.
    close_stdin = True
    # pi's built-in tools are read/bash/edit/write/grep/find/ls — no web. The
    # WebSearch/WebFetch capability arrives as opt-in extensions, so denying by
    # name keeps an offline eval clean (same contract as claude's _WEB_TOOLS).
    _WEB_TOOLS = ["WebSearch", "WebFetch"]

    def _denied(self, *, web_access: bool, kb_access: bool) -> list[str]:
        # pi takes --exclude-tools as ONE comma-separated list argument
        # (`--exclude-tools <list>`), unlike claude's repeatable --disallowed-tools.
        deny: list[str] = []
        if not web_access:
            deny += self._WEB_TOOLS
        if not kb_access and self.KB_TOOL_PREFIX:
            deny.append(self.KB_TOOL_PREFIX)
        return ["--exclude-tools", ",".join(deny)] if deny else []

    def _provider(self) -> str:
        return os.environ.get("DSWARM_PI_PROVIDER", "").strip()

    def build_execute(
        self, prompt: str, session: Optional[str], *,
        web_access: bool = True, kb_access: bool = True, stream: bool = False,
    ) -> list[str]:
        # `--mode json` already emits one JSON event per step and EXITS after the
        # run; stream is accepted for interface parity (no extra flag needed).
        # The prompt is a positional arg per docs/json.md (`pi --mode json "..."`).
        argv = [self.bin, "--mode", "json", "--session-dir", ".pi-sessions"]
        prov = self._provider()
        if prov:
            argv += ["--provider", prov]
        if session:
            argv += ["--session", session]
        argv += self._denied(web_access=web_access, kb_access=kb_access)
        argv += [prompt]
        return argv

    def build_resume(
        self, prompt: str, session: str, *,
        web_access: bool = True, kb_access: bool = True, stream: bool = False,
    ) -> list[str]:
        # resume the SAME session when the id is known; fall back to
        # -c/--continue (the worker's .pi-sessions dir holds only its own
        # sessions, so "most recent" is the right one).
        argv = [self.bin, "--mode", "json", "--session-dir", ".pi-sessions"]
        prov = self._provider()
        if prov:
            argv += ["--provider", prov]
        argv += (["--session", session] if session else ["-c"])
        argv += self._denied(web_access=web_access, kb_access=kb_access)
        argv += [prompt]
        return argv

    @staticmethod
    def _message_text(message: Any) -> str:
        """Best-effort text of a pi AgentMessage: either a `text` field or the
        concatenation of its text content blocks."""
        if not isinstance(message, dict):
            return ""
        txt = message.get("text")
        if isinstance(txt, str) and txt.strip():
            return txt.strip()
        parts: list[str] = []
        for b in message.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "text":
                t = b.get("text")
                if isinstance(t, str) and t.strip():
                    parts.append(t.strip())
        return "\n".join(parts)

    @staticmethod
    def _tool_result_text(result: Any) -> str:
        """Best-effort full text of a tool result (for the provenance gate): the
        joined text blocks of `result.content`, or the raw dict."""
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list):
                parts = []
                for b in content:
                    if isinstance(b, dict) and isinstance(b.get("text"), str):
                        parts.append(b["text"])
                if parts:
                    return "\n".join(parts)
            return str(result)
        return str(result or "")

    @staticmethod
    def _usage_tokens(ev: Any) -> "tuple[Optional[int], Optional[int]]":
        """Best-effort (input, output) tokens from a pi event. pi nests usage
        inside the message (`message.usage` with input/output/cacheRead fields);
        tolerate a top-level `usage` with input_tokens/output_tokens too."""
        u = None
        if isinstance(ev, dict):
            if isinstance(ev.get("usage"), dict):
                u = ev["usage"]
            msg = ev.get("message")
            if u is None and isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
                u = msg["usage"]
        if not isinstance(u, dict):
            return None, None
        inp = int(u.get("input_tokens") or u.get("input") or 0) or None
        out = int(u.get("output_tokens") or u.get("output") or 0) or None
        return inp, out

    @staticmethod
    def _is_assistant(ev: Any) -> bool:
        msg = ev.get("message") if isinstance(ev, dict) else None
        if isinstance(msg, dict):
            return str(msg.get("role") or "") == "assistant"
        return False

    def parse_stream_steps(self, line: str) -> list[StreamStep]:
        # pi streams per-delta message_update events; emitting every text_delta as
        # its own reasoning step would flood the deck, so only COMPLETE blocks are
        # surfaced (message_end / tool_execution_* / turn_end) — same granularity
        # as the other drivers.
        line = line.strip()
        if not line or not line.startswith("{"):
            return []
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return []
        t = ev.get("type")
        if t == "session" and ev.get("id"):
            # first event of a json-mode run: the session id (usable for resume)
            return [StreamStep("session", session=str(ev["id"]))]
        if t == "tool_execution_start":
            args = ev.get("args") or {}
            arg = str(args.get("command") or args.get("query") or args.get("file_path") or "")[:300]
            return [StreamStep("tool", tool=str(ev.get("toolName") or "tool"), text=arg)]
        if t == "tool_execution_end":
            # text=truncated for the deck; raw=full for the provenance gate.
            full = self._tool_result_text(ev.get("result"))
            return [StreamStep("tool_result", text=full[:600], raw=full)]
        if t == "message":
            # Some pi json-mode versions only emit `message` events (no
            # message_end/turn_end); surface complete assistant messages too.
            if self._is_assistant(ev):
                text = self._message_text(ev.get("message"))
                if text:
                    return [StreamStep("reasoning", text=text)]
        if t == "message_end":
            # message_end fires for the USER message too — only surface assistant
            # text as a reasoning step.
            if self._is_assistant(ev):
                text = self._message_text(ev.get("message"))
                if text:
                    return [StreamStep("reasoning", text=text)]
        if t == "turn_end":
            if self._is_assistant(ev):
                text = self._message_text(ev.get("message"))
                if text:
                    return [StreamStep("reasoning", text=text)]
        return []

    def parse(self, stdout: str, stderr: str) -> CliResult:
        """Accumulate assistant text across message_end/turn_end/agent_end events
        (the final result of a `pi --mode json` run is the last assistant message),
        best-effort usage from any event carrying a `usage` block (top-level or
        nested in `message.usage`), and the session id from the leading
        `{"type":"session"}` event. The same assistant message arrives in
        message_end, turn_end AND agent_end.messages — dedupe by (role, text)."""
        parts: list[str] = []
        seen: set[tuple[str, str]] = set()
        in_tok = out_tok = 0
        session: Optional[str] = None
        for raw in stdout.splitlines():
            line = raw.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "session" and ev.get("id"):
                session = str(ev["id"])
            if t in ("message", "message_end", "turn_end", "agent_end"):
                # agent_end carries an array of messages; message_end/turn_end a
                # single message.
                msgs = ev.get("messages")
                if isinstance(msgs, list):
                    for m in msgs:
                        if isinstance(m, dict) and m.get("role") == "assistant":
                            txt = self._message_text(m)
                            key = ("assistant", txt)
                            if txt and key not in seen:
                                seen.add(key)
                                parts.append(txt)
                else:
                    if self._is_assistant(ev):
                        txt = self._message_text(ev.get("message"))
                        key = ("assistant", txt)
                        if txt and key not in seen:
                            seen.add(key)
                            parts.append(txt)
            inp, outp = self._usage_tokens(ev)
            if inp is not None:
                in_tok = inp
            if outp is not None:
                out_tok = outp
        text = "\n".join(parts).strip()
        if text:
            return CliResult(text=text[-8000:], session=session, cost_usd=None,
                             input_tokens=(in_tok or None), output_tokens=(out_tok or None),
                             raw_stderr=stderr[-2000:])
        return CliResult(text=stdout[-8000:], session=session, raw_stderr=stderr[-2000:])

    _PROBE_BUILTIN_TOOLS = ("read", "bash", "edit", "write", "grep", "find", "ls")
    _PROBE_MAX_OUTPUT_BYTES = 64 * 1024

    @staticmethod
    def _validate_probe_value(value: str, field_name: str, *, max_len: int = 256) -> str:
        value = str(value or "").strip()
        if not value:
            raise ValueError(f"{field_name} must not be empty")
        if len(value) > max_len or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError(f"{field_name} is invalid")
        return value

    def _probe_disabled_tools(self) -> tuple[str, ...]:
        tools = list(self._PROBE_BUILTIN_TOOLS) + list(self._WEB_TOOLS)
        if self.KB_TOOL_PREFIX:
            tools.append(self.KB_TOOL_PREFIX)
        configured = os.environ.get("DSWARM_MCP_TOOL_PREFIXES", "")
        tools.extend(part.strip() for part in configured.split(",") if part.strip())
        return tuple(dict.fromkeys(tools))

    def probe_spec(self, *, model: str, session_dir: str) -> CliProbeSpec:
        model = self._validate_probe_value(model, "model")
        session_dir = self._validate_probe_value(session_dir, "session_dir", max_len=1024)
        # The path is interpreted inside the Linux worker container, even when
        # the coordinator itself runs on Windows.
        if not session_dir.startswith("/"):
            raise ValueError("session_dir must be absolute")
        disabled_tools = self._probe_disabled_tools()
        # `pi` is intentionally a logical in-container command.  Do not use
        # self.bin: resolving it would inspect or execute the host installation.
        argv = (
            "pi",
            "--mode",
            "json",
            "--model",
            model,
            "--session-dir",
            session_dir,
            "--exclude-tools",
            ",".join(disabled_tools),
            self.HELLO_PROMPT,
        )
        return CliProbeSpec(
            argv=argv,
            prompt=self.HELLO_PROMPT,
            model=model,
            session_dir=session_dir,
            disabled_tools=disabled_tools,
            non_agentic=True,
            requires_closed_stdin=True,
            max_output_bytes=self._PROBE_MAX_OUTPUT_BYTES,
        )

    @staticmethod
    def _probe_event_text(event: Any) -> str:
        messages = event.get("messages") if isinstance(event, dict) else None
        candidates = messages if isinstance(messages, list) else [
            event.get("message") if isinstance(event, dict) else None
        ]
        parts: list[str] = []
        for message in candidates:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            text = PiDriver._message_text(message)
            if text:
                parts.append(text)
        return "\n".join(parts)

    @staticmethod
    def _probe_usage(event: Any) -> tuple[Optional[int], Optional[int], bool]:
        usage = None
        if isinstance(event, dict) and isinstance(event.get("usage"), dict):
            usage = event["usage"]
        elif isinstance(event, dict) and isinstance(event.get("message"), dict):
            message = event["message"]
            if isinstance(message.get("usage"), dict):
                usage = message["usage"]
        if usage is None:
            return None, None, True

        def token_value(*keys: str) -> tuple[Optional[int], bool]:
            present = next((usage[key] for key in keys if key in usage), None)
            if present is None:
                return None, True
            if isinstance(present, bool) or not isinstance(present, int) or present < 0:
                return None, False
            return present, True

        inp, inp_ok = token_value("input_tokens", "input")
        out, out_ok = token_value("output_tokens", "output")
        return inp, out, inp_ok and out_ok

    @staticmethod
    def _probe_failure_classification(text: str) -> tuple[str, str]:
        lower = text.lower()
        if any(token in lower for token in ("api key", "authentication", "unauthorized", "forbidden", "401", "403")):
            return "auth", "auth_failed"
        if any(token in lower for token in ("model", "configuration", "config", "unsupported", "invalid option")):
            return "model_config", "model_or_config_failed"
        if any(token in lower for token in ("timeout", "timed out", "deadline")):
            return "timeout", "timeout"
        if any(token in lower for token in ("connection", "connect", "transport", "reset", "network", "eof")):
            return "transport", "transport_error"
        return "non_zero_exit", "nonzero_exit"

    def parse_probe_result(
        self, stdout: str, stderr: str, returncode: int
    ) -> CliProbeResult:
        parseable = False
        invalid_usage = False
        input_tokens: Optional[int] = None
        output_tokens: Optional[int] = None
        reply_parts: list[str] = []
        terminal_type: Optional[str] = None
        failure_text = ""
        for raw in (stdout or "").splitlines():
            line = raw.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            parseable = True
            event_type = str(event.get("type") or "")
            if event_type in {"turn.failed", "agent_failed", "error"}:
                error = event.get("error")
                if isinstance(error, dict):
                    failure_text += " " + str(error.get("message") or "")
                else:
                    failure_text += " " + str(error or event.get("message") or "")
            inp, out, usage_ok = self._probe_usage(event)
            if not usage_ok:
                invalid_usage = True
            else:
                if inp is not None:
                    input_tokens = inp
                if out is not None:
                    output_tokens = out
            if event_type in {"message_end", "turn_end", "agent_end", "agent_settled"}:
                terminal_type = terminal_type or event_type
                text = self._probe_event_text(event)
                if text:
                    reply_parts.append(text)

        if invalid_usage:
            return CliProbeResult(False, "protocol", "invalid_usage", diagnostics="invalid usage")

        failure_source = failure_text + " " + (stderr or "")
        if returncode != 0 or failure_text:
            classification, code = self._probe_failure_classification(failure_source)
            return CliProbeResult(
                False, classification, code, input_tokens, output_tokens,
                diagnostics=code, completed_event_type=terminal_type,
            )

        if terminal_type is None:
            code = "incomplete_turn" if parseable else "empty_output"
            return CliProbeResult(False, "protocol", code, diagnostics=code)
        if not "".join(reply_parts).strip():
            return CliProbeResult(
                False, "empty_reply", "empty_reply", input_tokens, output_tokens,
                diagnostics="empty reply", completed_event_type=terminal_type,
            )
        return CliProbeResult(
            True, "success", "completed", input_tokens, output_tokens,
            completed_event_type=terminal_type,
        )

    def _hello_argv(self) -> list[str]:
        # a real one-turn json-mode probe — symmetric with the other engines.
        return [self.bin, "--mode", "json", "--session-dir", ".pi-sessions",
                self.HELLO_PROMPT]

    def _hello_ok(self, r: "subprocess.CompletedProcess") -> bool:
        # a completed model turn proves auth/quota and the backend round-trip.
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") in ("agent_end", "agent_settled", "turn_end", "message_end"):
                return True
        return False


DRIVERS: dict[str, CliDriver] = {
    "pi": PiDriver(),
}


def get_driver(name: str) -> CliDriver:
    try:
        return DRIVERS[name]
    except KeyError:
        raise ValueError(
            f"unknown engine {name!r}: expected one of {sorted(DRIVERS)} "
            f"(a profile id like 'pi-sub-container' should be resolved to its "
            f"base engine via driver_for/base_engine_for_profile first)"
        ) from None


def _insert_model_arg(argv: list[str], model: str) -> list[str]:
    model = (model or "").strip()
    if not model or "--model" in argv or "-m" in argv:
        return argv
    if "--" in argv:
        idx = argv.index("--")
        return [*argv[:idx], "--model", model, *argv[idx:]]
    if len(argv) <= 1:
        return [*argv, "--model", model]
    return [*argv[:-1], "--model", model, argv[-1]]


class ProfileDriver(CliDriver):
    """Profile-bound wrapper for local/subscription workers.

    A worker profile is the unit the operator configures. Health probes and argv
    construction must therefore carry the profile's selected model too; otherwise a
    quota-exhausted default model can mark the whole engine unhealthy.
    """

    def __init__(self, base: CliDriver, profile: dict[str, Any]) -> None:
        self.base = base
        self.profile = dict(profile)
        self.name = base.name
        self.HELLO_PROMPT = base.HELLO_PROMPT
        self._HELLO_TIMEOUT = getattr(base, "_HELLO_TIMEOUT", self._HELLO_TIMEOUT)
        self._HELLO_RETRIES = getattr(base, "_HELLO_RETRIES", self._HELLO_RETRIES)

    @property
    def bin(self) -> str:
        return self.base.bin

    def _model(self) -> str:
        return str(self.profile.get("model") or "").strip()

    def _with_model(self, argv: list[str]) -> list[str]:
        return _insert_model_arg(argv, self._model())

    def new_session(self) -> Optional[str]:
        return self.base.new_session()

    def build_execute(
        self, prompt: str, session: Optional[str], *,
        web_access: bool = True, kb_access: bool = True, stream: bool = False,
    ) -> list[str]:
        return self._with_model(self.base.build_execute(
            prompt, session, web_access=web_access, kb_access=kb_access, stream=stream))

    def build_resume(
        self, prompt: str, session: str, *,
        web_access: bool = True, kb_access: bool = True, stream: bool = False,
    ) -> list[str]:
        return self._with_model(self.base.build_resume(
            prompt, session, web_access=web_access, kb_access=kb_access, stream=stream))

    def parse(self, stdout: str, stderr: str) -> CliResult:
        return self.base.parse(stdout, stderr)

    def probe_spec(self, *, model: str, session_dir: str) -> CliProbeSpec:
        return self.base.probe_spec(model=self._model() or model, session_dir=session_dir)

    def parse_probe_result(
        self, stdout: str, stderr: str, returncode: int
    ) -> CliProbeResult:
        return self.base.parse_probe_result(stdout, stderr, returncode)

    def parse_stream_line(self, line: str) -> Optional["StreamStep"]:
        return self.base.parse_stream_line(line)

    def parse_stream_steps(self, line: str) -> list["StreamStep"]:
        return self.base.parse_stream_steps(line)

    def _hello_argv(self) -> list[str]:
        return self._with_model(self.base._hello_argv())  # noqa: SLF001

    def _hello_ok(self, r: "subprocess.CompletedProcess") -> bool:
        return self.base._hello_ok(r)  # noqa: SLF001


class EndpointDriver(CliDriver):
    """Profile-bound driver wrapper for custom API endpoints.

    The base driver still owns parsing and CLI-specific behavior; this wrapper
    only injects endpoint config and probes the configured endpoint directly.
    """

    def __init__(self, base: CliDriver, profile: dict[str, Any]) -> None:
        self.base = base
        self.profile = dict(profile)
        self.name = base.name

    @property
    def bin(self) -> str:
        return self.base.bin

    def new_session(self) -> Optional[str]:
        return self.base.new_session()

    def build_execute(
        self, prompt: str, session: Optional[str], *,
        web_access: bool = True, kb_access: bool = True, stream: bool = False,
    ) -> list[str]:
        return self.base.build_execute(
            prompt, session, web_access=web_access, kb_access=kb_access, stream=stream)

    def build_resume(
        self, prompt: str, session: str, *,
        web_access: bool = True, kb_access: bool = True, stream: bool = False,
    ) -> list[str]:
        return self.base.build_resume(
            prompt, session, web_access=web_access, kb_access=kb_access, stream=stream)

    def parse(self, stdout: str, stderr: str) -> CliResult:
        return self.base.parse(stdout, stderr)

    def probe_spec(self, *, model: str, session_dir: str) -> CliProbeSpec:
        selected_model = str(self.profile.get("model") or model).strip()
        return self.base.probe_spec(model=selected_model, session_dir=session_dir)

    def parse_probe_result(
        self, stdout: str, stderr: str, returncode: int
    ) -> CliProbeResult:
        return self.base.parse_probe_result(stdout, stderr, returncode)

    def parse_stream_line(self, line: str) -> Optional["StreamStep"]:
        return self.base.parse_stream_line(line)

    def parse_stream_steps(self, line: str) -> list["StreamStep"]:
        return self.base.parse_stream_steps(line)

    def _endpoint_probe_url(self) -> str:
        return str(self.profile.get("base_url") or "").rstrip("/")

    def _hello_argv(self) -> list[str]:
        # endpoint workers probe via curl in health_detail — no CLI hello probe.
        return []

    def _hello_ok(self, r: "subprocess.CompletedProcess") -> bool:
        return self.base._hello_ok(r)  # noqa: SLF001

    def _api_key(self, env: "dict[str, str] | None" = None) -> str:
        """Resolve the endpoint API key for the health probe, mirroring how the
        real worker authenticates (#5). The old version only handled `env:NAME`,
        so a FILE-backed Credential Account (api_key_ref empty, secret stored in an
        API_KEY file) made the probe omit the auth header → false-negative health
        even though the live worker authenticates fine via runtime_env_for_engine.
        Resolution order: explicit api_key_ref (env: or file:) → the *_API_KEY_FILE
        / *_API_KEY env the credential injection already populates for this worker.

        `env` (when given) is the credential environment the caller resolved for this
        probe — read it instead of the process-global os.environ so a parallel probe
        sees ITS OWN injected key, not whatever another thread last overlaid."""
        src = env if env is not None else os.environ
        ref = str(self.profile.get("api_key_ref") or "").strip()
        if ref.startswith("env:"):
            value = src.get(ref[4:], "").strip()
            if value:
                return value
        if ref.startswith("file:"):
            try:
                value = Path(ref[5:]).read_text(encoding="utf-8").strip()
                if value:
                    return value
            except OSError:
                pass
        # No explicit ref → fall back to the env the Credential Account injection
        # sets for this transport: <PROVIDER>_API_KEY_FILE (file-backed) or the
        # bare <PROVIDER>_API_KEY (env-backed). pi's endpoint workers read the
        # standard provider keys — OPENAI_API_KEY is what the injection populates.
        env_name = "OPENAI_API_KEY"
        file_env = src.get(f"{env_name}_FILE", "").strip()
        if file_env:
            try:
                return Path(file_env).read_text(encoding="utf-8").strip()
            except OSError:
                return ""
        return src.get(env_name, "").strip()

    def health_detail(self, *, env: "dict[str, str] | None" = None) -> "tuple[bool, str]":
        base_url = str(self.profile.get("base_url") or "").strip()
        if not base_url:
            return self.base.health_detail(env=env)
        result = probe_endpoint(
            self.profile,
            api_key=self._api_key(env),
            validate_model=True,
        )
        return bool(result.get("ok")), str(result.get("detail") or "endpoint probe failed")



def driver_for(profile_or_name: str | dict[str, Any]) -> CliDriver:
    if isinstance(profile_or_name, dict):
        base_name = base_engine_for_profile(profile_or_name)
        base = get_driver(base_name)
        if profile_uses_endpoint(profile_or_name):
            return EndpointDriver(base, profile_or_name)
        return ProfileDriver(base, profile_or_name)
    # A bare string may be a base engine, a transport, OR a profile id like
    # "codex-sub-container". base_engine_for_profile recovers the base from any of
    # them, so a profile id no longer hits DRIVERS[...] raw (which would KeyError —
    # the "local run crashes on the -sub-container profile" bug).
    return get_driver(base_engine_for_profile(str(profile_or_name)))


# Deep auth-level liveness for the engine bar (FE-quota-display). `--version`
# (`available`) only proves the binary runs — it can't catch an expired headless
# auth (e.g. cursor-agent -p → "Authentication required" even though
# `cursor-agent status` shows logged-in). health_detail() shells a real one-turn
# hello, so it's expensive: cache it on its OWN throttle (>= the deck's 60s poll)
# with last-good reuse, exactly like quota. Decorative + never blocks the bar.


import contextlib as _contextlib


@_contextlib.contextmanager
def _patched_env(values: "dict[str, str]"):
    """Temporarily overlay os.environ with `values`, restoring on exit."""
    old = {k: os.environ.get(k) for k in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _probe_health_with_creds(name: str, drv: "CliDriver",
                             account_root: "Optional[str]") -> "tuple[bool, str]":
    """Run a driver's health_detail() with the engine's DEFAULT-account credential
    env injected (when account_root is known) — so the global probe matches what a
    live worker sees (pi's provider key comes from the account store, not the
    host). account_root=None → bare probe (no account store available, e.g. a
    TUI/test context)."""
    if account_root is None:
        return drv.health_detail()
    try:
        from dswarm.solver.credential_accounts import runtime_env_for_engine
        env = runtime_env_for_engine(
            name, account_root=account_root, account_id=None, container=False).env
    except Exception:
        env = {}
    if not env:
        return drv.health_detail()
    with _patched_env(env):
        return drv.health_detail()



def engine_status(account_root: "Optional[str]" = None,
                  backend: str = "local",
                  profiles: "Optional[list[dict[str, Any]]]" = None) -> list[dict]:
    """Cheap per-engine status for the deck's always-on engine bar.

    This endpoint is polled by the browser, so it must not spend model tokens. It
    only checks that the configured engine binary can start (`--version`) and
    annotates the selected worker profile/model when available. Token-spending
    model probes live in `/api/engines/health`, the model-test button, and the
    dispatch-time health gate.
    """
    profile_rows = [p for p in (profiles or []) if isinstance(p, dict)]
    if profile_rows:
        selected: list[tuple[str, dict[str, Any] | None]] = []
        seen: set[str] = set()
        for p in profile_rows:
            name = base_engine_for_profile(p)
            if name in DRIVERS and name not in seen:
                selected.append((name, p))
                seen.add(name)
    else:
        selected = [(name, None) for name in DRIVERS]
    out: list[dict] = []
    for name, profile in selected:
        drv = driver_for(profile) if profile else DRIVERS[name]
        try:
            b = drv.bin
            ok = _runs_ok(b)
        except Exception:
            b, ok = name, False
        row = {
            "engine": name,
            "bin": b,
            "available": ok,
            # None means "not deep-probed by the always-on poll". The frontend only
            # treats explicit False as degraded; run-scoped failures and on-demand
            # checks still surface their concrete reasons.
            "healthy": None,
            "health_detail": "",
        }
        if profile:
            row.update({
                "profile_id": profile.get("id") or "",
                "profile_name": profile.get("name") or profile.get("id") or name,
                "model": str(profile.get("model") or ""),
                "backend": backend,
            })
        out.append(row)
    return out


def engine_health(backend: str = "local",
                  account_root: "Optional[str]" = None,
                  profiles: "Optional[list[dict[str, Any]]]" = None) -> list[dict]:
    """A DEEP per-engine self-check (FE-healthcheck-page). `backend` selects WHAT
    is checked, because local and container exercise different things:

    - "local"     → run each driver's real healthcheck ON THE HOST (claude does a
                    1-turn dry run that exercises the host's default login + auth).
                    Answers "is the host's default CLI healthy?".
    - "container" → `docker run --rm` the worker image and verify each engine's
                    CLI launches INSIDE the container (image present + binary on
                    the container PATH). Answers "can the worker image actually
                    start each engine?". Auth-in-container is account-specific and
                    is covered by the per-account connectivity test, not here.

    When `profiles` is provided for local mode, self-check those configured worker
    profiles instead of the bare engines: that makes the button exercise the same
    credential account and selected model a real worker will use. Returns {engine,
    bin, version, healthy, detail, backend}. On-demand only."""
    if (backend or "").strip() == "container":
        return _engine_health_container()
    profile_rows = [p for p in (profiles or []) if isinstance(p, dict)]
    if profile_rows:
        from dswarm.solver.credential_accounts import runtime_env_for_engine

        out: list[dict] = []
        for profile in profile_rows:
            name = base_engine_for_profile(profile)
            drv = driver_for(profile)
            b, version, healthy, detail = name, "", False, ""
            try:
                b = drv.bin
                r = subprocess.run([b, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
                raw = (r.stdout or r.stderr or "").strip()
                version = raw.splitlines()[0][:80] if raw else ""
                if r.returncode != 0:
                    detail = "binary not runnable (--version failed)"
                else:
                    account_id = str(profile.get("credential_account") or "").strip()
                    resolved_account_id = account_id if account_id else ""
                    env = runtime_env_for_engine(
                        name,
                        account_root=Path(account_root) if account_root else None,
                        account_id=resolved_account_id,
                        container=False,
                    ).env
                    old = {k: os.environ.get(k) for k in env}
                    try:
                        os.environ.update(env)
                        if profile_uses_endpoint(profile):
                            healthy, detail = drv.health_detail()
                        else:
                            argv = _insert_model_arg(
                                drv._hello_argv(),  # noqa: SLF001 - self-check mirrors driver probe.
                                str(profile.get("model") or ""))
                            if not argv:
                                healthy, detail = False, "driver has no hello probe"
                            else:
                                rr = subprocess.run(
                                    argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=getattr(drv, "_HELLO_TIMEOUT", 90))
                                healthy = bool(drv._hello_ok(rr))  # noqa: SLF001
                                if not healthy:
                                    tail = (rr.stderr or rr.stdout or "").strip().splitlines()
                                    detail = (f"hello exited {rr.returncode}"
                                              + (f": {tail[-1][:120]}" if tail else ""))
                    finally:
                        for k, v in old.items():
                            if v is None:
                                os.environ.pop(k, None)
                            else:
                                os.environ[k] = v
            except FileNotFoundError:
                detail = "binary not found on PATH"
            except subprocess.TimeoutExpired:
                detail = "probe timed out"
            except Exception as e:  # noqa: BLE001
                detail = str(e)[:160]
            out.append({"engine": name, "profile_id": profile.get("id") or "",
                        "profile_name": profile.get("name") or profile.get("id") or name,
                        "model": str(profile.get("model") or ""),
                        "bin": b, "version": version, "healthy": healthy,
                        "detail": detail, "backend": "local",
                        "bin_source": resolve_engine_bin_source(name),
                        "bin_env": _ENV_OVERRIDE.get(name, "")})
        return out
    out: list[dict] = []
    for name, drv in DRIVERS.items():
        b, version, healthy, detail = name, "", False, ""
        try:
            b = drv.bin
            r = subprocess.run([b, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
            raw = (r.stdout or r.stderr or "").strip()
            version = raw.splitlines()[0][:80] if raw else ""
            if r.returncode != 0:
                detail = "binary not runnable (--version failed)"
            else:
                # deep probe: a real one-turn hello (with one retry on a transient
                # miss). detail names the failure mode so red is actionable, not a
                # blanket "check login / quota". Inject the default-account creds so
                # cursor (CURSOR_API_KEY-only headless) isn't falsely reported down.
                healthy, detail = _probe_health_with_creds(name, drv, account_root)
        except FileNotFoundError:
            detail = "binary not found on PATH"
        except subprocess.TimeoutExpired:
            detail = "probe timed out"
        except Exception as e:  # noqa: BLE001 — surface the message to the operator
            detail = str(e)[:160]
        # bin_source tells the FE whether this path was explicitly pinned (env) or
        # auto-discovered (known-good / path) so it can warn that an unpinned local
        # default may resolve to the wrong version, and point at the env var to fix.
        out.append({"engine": name, "bin": b, "version": version,
                    "healthy": healthy, "detail": detail, "backend": "local",
                    "bin_source": resolve_engine_bin_source(name),
                    "bin_env": _ENV_OVERRIDE.get(name, "")})
    return out


# in-container worker binary per engine (mirrors container_exec._CONTAINER_BIN).
_CONTAINER_ENGINE_BIN = {
    "pi": "pi",
}


def _engine_health_container() -> list[dict]:
    """Container self-check: one `docker run --rm` per engine verifying the worker
    image has a launchable CLI. No account/bench mounts — this checks the image +
    binary plumbing only (auth is the per-account test's job)."""
    import shutil

    out: list[dict] = []
    docker = shutil.which("docker")
    # image presence is shared across engines — probe once.
    from dswarm.solver.container_exec import WORKER_IMAGE
    image_ok = False
    image_detail = ""
    if not docker:
        image_detail = "docker not found"
    else:
        try:
            r = subprocess.run([docker, "image", "inspect", WORKER_IMAGE],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20)
            image_ok = r.returncode == 0
            if not image_ok:
                image_detail = f"image missing: {WORKER_IMAGE}"
        except subprocess.TimeoutExpired:
            image_detail = "docker image inspect timed out"
        except Exception as e:  # noqa: BLE001
            image_detail = str(e)[:120]

    for name in DRIVERS:
        bin_in = _CONTAINER_ENGINE_BIN.get(name, name)
        healthy, version, detail = False, "", ""
        if not image_ok:
            detail = image_detail
        else:
            try:
                r = subprocess.run(
                    # the image ENTRYPOINT is the runtime supervisor (a daemon); a
                    # one-shot self-check must override it with a shell via
                    # --entrypoint, else `-lc <cmd>` becomes args to the supervisor.
                    [docker, "run", "--rm", "--network", "none",
                     "--entrypoint", "bash", WORKER_IMAGE,
                     "-lc", f"{bin_in} --version 2>&1 || echo DSWARM_CLI_FAIL"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
                raw = (r.stdout or "").strip()
                if "DSWARM_CLI_FAIL" in raw or r.returncode != 0:
                    detail = f"{name} CLI not launchable in container"
                else:
                    healthy = True
                    version = raw.splitlines()[0][:80] if raw else ""
            except subprocess.TimeoutExpired:
                detail = "container probe timed out"
            except Exception as e:  # noqa: BLE001
                detail = str(e)[:120]
        out.append({"engine": name, "bin": bin_in, "version": version,
                    "healthy": healthy, "detail": detail, "backend": "container"})
    return out


def _descendant_pids(root_pid: int) -> "list[int]":
    """Every descendant PID of root_pid (depth-first), via `ps -axo pid=,ppid=`.

    killpg only reaches the worker's ORIGINAL process group. A child that calls
    setsid() (a backgrounded daemon, `docker run -d`'s client, an agent helper
    that detaches) becomes its own group leader and survives killpg — it gets
    reparented to init and keeps running, holding CPU / ports / a concurrency
    slot (the "worker shows closed but its process is still alive" leak). We walk
    the live ppid table to catch those escapees too. Best-effort; [] on any error.
    """
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=10).stdout
    except Exception:
        return []
    children: "dict[int, list[int]]" = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    out_pids: "list[int]" = []
    stack = list(children.get(root_pid, []))
    seen: "set[int]" = set()
    while stack:
        pid = stack.pop()
        if pid in seen or pid == root_pid:
            continue
        seen.add(pid)
        out_pids.append(pid)
        stack.extend(children.get(pid, []))
    return out_pids


def _kill_proc_tree(proc: "subprocess.Popen", *, pgid: "Optional[int]" = None) -> None:
    """Kill a worker AND its full descendant tree, then REAP it.

    The CLI agent spawns helpers (curl, sh, python, docker); killing only the
    parent can leave a child holding the stdout pipe or running detached. Three
    layers, each best-effort:
      1. os.killpg(SIGKILL) on the worker's process group (start_new_session=True
         makes the worker a group leader, so this takes down everything that
         stayed in the group at once);
      2. enumerate every descendant PID via the live ppid table and SIGKILL each
         individually — this catches children that setsid()'d out of the group
         (the orphan/leak case killpg alone misses);
      3. proc.wait() to reap the parent so it doesn't linger as a <defunct>
         zombie occupying a process-table slot.
    """
    # 2 first: snapshot descendants BEFORE killpg, since killpg + reparent can
    # mutate the ppid table out from under us.
    descendants = _descendant_pids(proc.pid)
    try:
        target_pgid = pgid if pgid is not None else os.getpgid(proc.pid)
        os.killpg(target_pgid, signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    for pid in descendants:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    # reap the parent (avoid a defunct zombie). short timeout: it's been SIGKILL'd.
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def run_cli(driver: CliDriver, argv: list[str], *, cwd: str, timeout: int,
            env: Optional[dict] = None, container: "Optional[object]" = None) -> CliResult:
    """Run a CLI driver's argv as a subprocess and parse the result. `env`, if
    given, OVERLAYS os.environ (so the worker inherits PATH etc. plus our vars).

    `container`: if a ContainerHandle is given, the worker runs INSIDE that
    isolated Docker container (can't read the host bench tree) instead of bare on
    the host. None → host subprocess (default, unchanged)."""
    invocation_id = uuid.uuid4().hex
    if container is not None:
        from dswarm.solver.container_exec import run_cli_container
        result = run_cli_container(driver, argv, handle=container, cwd=cwd,
                                   timeout=timeout, env=env)
        result.invocation_id = invocation_id
        return result
    t0 = time.time()
    run_env = {**os.environ, **env} if env else None
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, env=run_env,
                              stdin=subprocess.DEVNULL if getattr(driver, "close_stdin", False) else None)
    except subprocess.TimeoutExpired as e:
        out = e.stdout if isinstance(e.stdout, str) else ""
        err = e.stderr if isinstance(e.stderr, str) else ""
        res = driver.parse(out or "", err or "")
        res.timed_out = True
        res.elapsed_s = time.time() - t0
        res.invocation_id = invocation_id
        return res
    res = driver.parse(proc.stdout or "", proc.stderr or "")
    res.elapsed_s = time.time() - t0
    res.invocation_id = invocation_id
    return res


def run_cli_streaming(
    driver: CliDriver, argv: list[str], *, cwd: str, timeout: int,
    on_step: "Callable[[StreamStep], None]", env: Optional[dict] = None,
    cancel_event: "Optional[threading.Event]" = None,
    on_proc: "Optional[Callable[[subprocess.Popen], None]]" = None,
    steer_event: "Optional[threading.Event]" = None,
    paused_event: "Optional[threading.Event]" = None,
    container: "Optional[object]" = None,
) -> CliResult:
    """Like run_cli, but reads stdout LINE BY LINE and fires on_step(StreamStep)
    for each parsed line as it arrives — so a caller can surface live progress.
    The full stdout is still accumulated and run through driver.parse() for the
    final CliResult (flag/cost/session), identical to the non-streaming path.
    `env`, if given, OVERLAYS os.environ for the subprocess.

    Runtime control (dispatcher control over a stateless worker subprocess):
      - `cancel_event`: when set, the subprocess is KILLED immediately (not just
        the asyncio task — that left the CLI agent running, see bug #2). A watcher
        thread kills it the instant the event fires, even if the model is mid-think
        and stdout is quiet (the per-line loop alone could wait minutes).
      - `on_proc`: invoked once with the live Popen so the caller can SIGSTOP /
        SIGCONT it for HITL pause/resume. The worker keeps the same PID, so a paused
        agent is genuinely frozen, not killed.
      - `paused_event`: set by the caller while the worker is SIGSTOP-frozen (HITL
        pause). The timeout is computed against wall-clock MINUS time spent paused, so
        a long operator pause can't trip the turn timeout and mislabel a deliberately
        frozen worker as `timed_out` (M7).
      - `steer_event`: like cancel, but means END THIS PASS without marking the worker
        dead — an operator hint/redirect/focus cuts the current pass so the swarm can
        respawn a worker that picks up the queued guidance. The subprocess is killed
        and res.steered=True; there is NO resume loop (single-shot), so the caller does
        not reconnect — steered only keeps the session id from being downgraded.
        cancel_event takes PRECEDENCE: a stop during a steer must still die.

    `container`: if a ContainerHandle is given, the worker runs INSIDE that
    isolated Docker container; all control (cancel/steer/pause) routes in via
    `docker exec kill`. None → host subprocess (default, unchanged).
    """
    invocation_id = uuid.uuid4().hex
    if container is not None:
        from dswarm.solver.container_exec import run_cli_streaming_container
        result = run_cli_streaming_container(
            driver, argv, handle=container, cwd=cwd, timeout=timeout,
            on_step=on_step, env=env, cancel_event=cancel_event,
            on_proc=on_proc, steer_event=steer_event, paused_event=paused_event)
        result.invocation_id = invocation_id
        return result
    import subprocess as _sp

    t0 = time.time()
    # M7: pause-aware timeout. `paused_accum` is the total wall-clock the worker spent
    # SIGSTOP-frozen by the operator; `pause_since` marks the start of the current
    # freeze (None when running). active_elapsed() subtracts paused time so a paused
    # worker can't be killed as `timed_out`. _pause_lock guards the two counters since
    # the watcher thread and the read loop both call active_elapsed().
    _pause_lock = threading.Lock()
    _pause_state = {"accum": 0.0, "since": None}  # mutated under _pause_lock

    def active_elapsed() -> float:
        """Wall-clock since t0 MINUS time spent paused. Folds the in-progress freeze
        in live so a worker paused RIGHT NOW doesn't keep accruing toward timeout."""
        now = time.time()
        if paused_event is not None and paused_event.is_set():
            with _pause_lock:
                if _pause_state["since"] is None:
                    _pause_state["since"] = now          # freeze just began
                paused = _pause_state["accum"] + (now - _pause_state["since"])
        else:
            with _pause_lock:
                if _pause_state["since"] is not None:    # freeze just ended → bank it
                    _pause_state["accum"] += now - _pause_state["since"]
                    _pause_state["since"] = None
                paused = _pause_state["accum"]
        return (now - t0) - paused

    run_env = {**os.environ, **env} if env else None
    # start_new_session=True puts the worker (and every descendant — the CLI agent
    # spawns curl/python/sh helpers) in its OWN process group. Killing just the
    # parent leaves a `sleep`/`curl` child holding the stdout pipe open, so the read
    # loop blocks until timeout (the deeper form of bug #2). We kill the whole GROUP.
    proc = _sp.Popen(argv, cwd=cwd, stdout=_sp.PIPE, stderr=_sp.PIPE,
                     text=True, encoding="utf-8", errors="replace", bufsize=1, env=run_env,
                     start_new_session=True,  # line-buffered + own process group
                     stdin=_sp.DEVNULL if getattr(driver, "close_stdin", False) else None)
    try:
        proc_pgid: "Optional[int]" = os.getpgid(proc.pid)
    except Exception:
        proc_pgid = None
    if on_proc is not None:
        try:
            on_proc(proc)
        except Exception:
            pass

    cancelled = False
    steered = False
    timed_out = False
    # Watcher thread: kill the subprocess the moment cancel OR steer fires, AND
    # enforce the wall-clock timeout. Without it, a control signal during a long
    # model "think" (no stdout) wouldn't be observed until the next line — which may
    # never come — and, more critically, a worker that emits ZERO stdout would block
    # the `for line in proc.stdout` read loop FOREVER (the in-loop timeout check at
    # the bottom never runs because the iterator never yields). The watcher is the
    # ONLY thing that can break a silent hang, so it ALWAYS runs — its startup is
    # deliberately NOT gated on cancel/steer being present (it used to be, which left
    # a bare `run_cli_streaming(..., timeout=N)` call with no timeout enforcement at
    # all). Killing the proc tree closes stdout, which unblocks the read loop.
    watcher_stop = threading.Event()

    def _watch() -> None:
        nonlocal cancelled, steered, timed_out
        while not watcher_stop.is_set():
            # cancel takes precedence over steer: a stop during a steer must die.
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _kill_proc_tree(proc, pgid=proc_pgid)
                return
            if steer_event is not None and steer_event.is_set():
                steered = True
                _kill_proc_tree(proc, pgid=proc_pgid)
                return
            if active_elapsed() > timeout:
                # Enforce the timeout HERE: the main read loop may be blocked on a
                # silent process and can't self-time-out. Kill the tree (unblocks the
                # read loop) and mark timed_out so the result reflects it. Uses
                # pause-aware elapsed so a frozen worker isn't killed for being paused.
                timed_out = True
                _kill_proc_tree(proc, pgid=proc_pgid)
                return
            watcher_stop.wait(0.1)

    watcher = threading.Thread(target=_watch, name="cli-control-watch", daemon=True)
    watcher.start()

    out_lines: list[str] = []
    err_lines: list[str] = []

    def _drain_stderr() -> None:
        try:
            assert proc.stderr is not None
            for err_line in proc.stderr:
                err_lines.append(err_line)
        except Exception:
            pass

    stderr_thread = threading.Thread(
        target=_drain_stderr, name="cli-stderr-drain", daemon=True)
    stderr_thread.start()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            out_lines.append(line)
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                _kill_proc_tree(proc, pgid=proc_pgid)
                break
            if steer_event is not None and steer_event.is_set():
                steered = True
                _kill_proc_tree(proc, pgid=proc_pgid)
                break
            if active_elapsed() > timeout:
                _kill_proc_tree(proc, pgid=proc_pgid)
                timed_out = True
                break
            try:
                steps = driver.parse_stream_steps(line)  # #18: ALL blocks, not just first
            except Exception:
                steps = []
            for step in steps:
                try:
                    on_step(step)
                except Exception:
                    pass  # a deck-emit failure must never kill the worker
        proc.wait(timeout=max(1, timeout - int(active_elapsed())))
    except _sp.TimeoutExpired:
        _kill_proc_tree(proc, pgid=proc_pgid)
        timed_out = True
    except Exception:
        _kill_proc_tree(proc, pgid=proc_pgid)
    finally:
        watcher_stop.set()
        if watcher is not None:
            watcher.join(timeout=1)
        # Some CLIs spawn sidecars that inherit stderr and outlive the parent. A
        # blocking proc.stderr.read() here keeps the worker task alive forever even
        # though the CLI parent is gone, so drain stderr in a thread and tear down
        # any leftover process-group holders if EOF does not arrive promptly.
        stderr_thread.join(timeout=1)
        if stderr_thread.is_alive():
            _kill_proc_tree(proc, pgid=proc_pgid)
            try:
                if proc.stderr:
                    proc.stderr.close()
            except Exception:
                pass
            stderr_thread.join(timeout=1)
    stderr = "".join(err_lines)
    res = driver.parse("".join(out_lines), stderr or "")
    res.timed_out = timed_out
    res.cancelled = cancelled
    res.steered = steered
    res.elapsed_s = time.time() - t0
    res.invocation_id = invocation_id
    return res
