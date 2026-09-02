# eval_nyu — CTF benchmark regression harness (route A, P5)

Reproduces the June-2026 NYU CTF Bench eval as an in-repo, CLI-driven harness.
One engine per challenge (attribution); the harness owns the target and the
ground truth; the oracle verifies byte-for-byte.

## Quick start

```bash
# list a dataset without running anything
python -m eval_nyu --dataset eval_nyu/datasets/local-cdut.json --list

# run pi on 3 challenges (300s budget each), then render the report
python -m eval_nyu --dataset eval_nyu/datasets/local-cdut.json --engines pi \
    --limit 3 --budget 300 --out eval_nyu/results/pilot-1.jsonl --report

# compare against the historical baseline
python -m eval_nyu --report --out eval_nyu/results/pilot-1.jsonl \
    --baseline eval_nyu/results/baseline-2026-06-11.jsonl
```

## Full NYU-200 regression (on a machine with the dataset + baseline engines)

1. Put the NYU CTF Bench `test` split at `<root>` (test_dataset.json + per-
   challenge dirs with challenge.json).
2. Install baseline engines (claude / codex / cursor-agent on PATH) — the
   harness health-checks each engine and skips missing ones with a note.
3. Run (engines are raced only if you list several; each (challenge, engine)
   pair is one run, so a full 200×4 attribution grid = 800 runs — or use a
   single engine per category to cut cost):

```bash
python -m eval_nyu --dataset <nyu-root> --engines pi,claude,codex,cursor \
    --budget 1800 --out eval_nyu/results/nyu-200.jsonl --report
```

Resume is safe: already-measured (challenge, engine) pairs are skipped, so an
interrupted run can be continued with the same `--out`.

## Data formats

- **Local manifest** (JSON): `{"challenges": [ {challenge_id, name, category,
  description, flag_format, ground_truth: [...], target, deploy: {kind:
  "static"|"docker", dir, port, flag_env, host_port}, files: [...]} ]}`
- **NYU root** (directory): `test_dataset.json` + `challenge.json` per
  challenge (name/category/description/flag/files). Service challenges with a
  docker-compose.yml are deployed with the ground-truth flag injected.

## Target lifecycle

- `static` — the harness serves the flag on an HTTP page; the worker fetches
  it via `host.docker.internal:<port>` (works from worker containers).
- `docker` — `docker build` the challenge dir, `docker run -d -e GZCTF_FLAG=
  <ground-truth> -e FLAG=<ground-truth> -p <free>:<port>`, wait for HTTP 200,
  run the worker, then `docker rm -f` on teardown (also on failure).

## Metrics

Per (challenge, engine): solved (byte-for-byte vs ground truth), matched flag,
elapsed, tokens, cost (CostController snapshot), stop reason. The report
aggregates solve rate / median time / cost per engine and category, and — with
`--baseline` — a per-challenge pi-vs-historical-winner table.

## Caveats

- The queue is per-(challenge,engine); engine attribution assumes
  a solo ReasonSwarm run (the winner IS that engine).
- pi runs through the P3 container stack (ctf-swarm-pi-<cat> image + host
  model gateway + per-run task token). Requires the worker images built and
  Docker Desktop running.
- The baseline file is extracted from the June-2026 NYU report's *measured*
  rows; *projected* rows are estimates and are NOT ingested.
