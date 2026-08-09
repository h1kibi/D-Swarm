# Direction config layers (route A)

Each direction profile (`pi-web`, `pi-pwn`, `pi-rev`, `pi-crypto`, `pi-misc`,
`pi-forensics`, `pi-aisec`) has its own thin config layer baked into its worker
image. The base image stays the same full-toolchain Kali/BTFly image for every
direction (maximum-richness environment); only what the agent *sees* differs.

Layout per direction:

- `prompt.md` — the direction tool & environment briefing injected into the
  worker prompt (`DSWARM_DIRECTION_PROMPT`). Keep it short; it only names the
  tools/environments relevant to this direction so the prompt stays small even
  though the image carries everything.
- `skills/` — direction skill extensions, one subdirectory per skill (each with
  its own `SKILL.md`). The swarm symlinks them into the isolated worker HOME
  under `~/.pi/agent/skills/`. The `dswarm-blackboard` skill is always present.
- `extensions/` — direction pi provider/CLI extensions, merged into
  `/opt/dswarm/pi-config/extensions` at image build time.
- `models.json` (optional) — per-direction provider defaults (baseUrl/API key
  placeholders). When present it overlays `/opt/dswarm/pi-config/models.json`.

`base` is the non-direction fallback used for the generic
`ctf-swarm-pi:0.3.0-rc.1` compat tag; its prompt is intentionally minimal.
