# Security Policy

## Reporting a Vulnerability

Please report security vulnerabilities **privately** via GitHub Security Advisories:

> **[Report a vulnerability](https://github.com/h1kibi/D-Swarm/security/advisories/new)**
> (repo → *Security* tab → *Report a vulnerability*)

Do **not** open a public issue for security problems. We will acknowledge your
report and work with you on a fix and coordinated disclosure.

---

## Runtime Trust Boundary

**D-Swarm is an offensive security automation tool.** It drives CLI coding agents to
execute commands, run security tooling, and reach target services in order to solve
CTF challenges. Understand the trust model before you run it.

### What D-Swarm does NOT promise

- **It does not isolate malicious challenges.** Strong sandboxing (malicious-code
  isolation, microVM/Firecracker, untrusted-input confinement) is an explicit
  **non-goal / accepted risk** of this project — not a missing feature. The worker
  executes content returned by the challenge target with privileges (the worker user
  has `NOPASSWD` sudo so it can install tools / change the system while solving), and
  the in-container control channel is explicitly **not** a security boundary against
  the worker.
- **The container worker backend is NOT a security sandbox.** Its purpose is a
  consistent Kali/CTF toolchain, a VPS / standalone-machine deployment form, and
  runtime-dependency / workspace / credential-mount isolation — **not** containment of
  untrusted code. Do not rely on it to protect the host from a malicious challenge.
- **Windows development hosts have weaker filesystem permission semantics.** The
  best-effort `chmod(0o600/0o700)` calls used for local credential and runtime files
  cannot provide POSIX-style owner-only isolation on Windows. Production worker
  isolation and credential projection are expected to run in the Docker/Linux
  runtime; treat native Windows runs as development-only and use a disposable host.

### How to run it safely

> ⚠️ **Run D-Swarm only in a dedicated, disposable environment** — a dedicated VPS, a
> throwaway VM, or a standalone machine with no sensitive data. **Do not run it on
> your primary workstation, a shared host, or a production environment.**

Treat the machine running D-Swarm as you would treat a machine you are pointing
offensive tooling *from*: assume any challenge could attempt to execute code through
the worker, and isolate accordingly.

### Web platform authorization & data visibility

The web platform (accounts, events, targets, runs, audit) has one explicit
authorization model:

- **Login**: account credentials (username/password) or the operator password
  (`DSWARM_WEB_PASSWORD`). Failures are throttled per client host (HTTP 429) and
  every attempt is written to the audit trail. Session tokens carry a `jti`;
  **logout denylists it**, and a **password change invalidates every previously
  issued token** (the caller receives a fresh one).
- **Gate**: with `DSWARM_WEB_PASSWORD` set, every `/api` route requires an
  authenticated principal (operator token, account JWT, or API key); anonymous
  requests are rejected. Non-loopback binds additionally require
  `DSWARM_JWT_SECRET` and `DSWARM_ENCRYPTION_KEY` at startup.
- **Roles**: admin accounts (and the operator token) own the settings surface —
  credential accounts, LLM providers/probes, worker image/settings, profiles.
  A member token gets 403 there. Runs, folders, targets, events, and the run
  rail are a **shared team board**: every authenticated member can see and work
  on them. This is a deliberate divergence from the archived design doc's
  per-member isolation — revisit with a real owner model if the team grows or
  per-member privacy becomes a requirement.
- **API keys**: per-account bearer credentials; they inherit the account's role
  and stop working when revoked or when the account is deactivated.

### Other things to be aware of

- **Flag verification trust assumption.** The provenance gate (`dswarm/solver/gate.py`)
  guards against the *model hallucinating* a flag — it does **not** guard against a
  *malicious challenge fabricating* a format-matching flag. For competitive use, verify
  accepted flags against the contest scoreboard, and prefer high-entropy flag formats
  (e.g. `flag{<sha256>}`) over short, brute-forceable ones.
- **Credentials.** D-Swarm reads engine credentials (OAuth tokens / API keys / the
  codex `auth.json`) from the macOS Keychain or environment and projects them into the
  worker environment. These are passed to the proprietary engine CLIs
  (`claude` / `codex` / `cursor-agent`), which transmit data to their
  respective providers (Anthropic / OpenAI / Cursor). Credential stores live under
  gitignored paths (`sessions/_secrets/`, `.env`) and are never committed; verify your
  own deployment keeps them out of version control.
- **Control plane.** In container mode the in-container supervisor dials back to a
  host-local control receiver (`127.0.0.1`, default port `9100`, per-run token). This
  is part of normal operation; be aware the control receiver listens on host loopback
  when deploying on a shared host.

## Supported Versions

This project is pre-1.0 and moves fast. Security fixes target the `main` branch.
