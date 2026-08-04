# Route-A pi worker image: the BTFly pi image + the muteki runtime-agent
# supervisor + blackboard skill + pi provider config. The ENTRYPOINT switches to
# the supervisor (reverse-connect rcp mode) so the muteki container backend can
# drive pi workers inside the per-category image; the pi CLI stays available as
# the worker engine.
#
# Built per category via build.sh (BASE_IMAGE=ctf-agent-pi-<cat>:0.1.0). The BTFly
# images already carry the category toolchain + the pi CTF knowledge base and
# category skills under /home/ctf/.pi/agent/skills/<cat>.
ARG BASE_IMAGE=ctf-agent-pi-base:0.1.0
FROM ${BASE_IMAGE}

# The BTFly base images end with `USER ctf`; BuildKit RUN steps INHERIT it, so
# every write below would run as uid 1000 and fail. Reset to root for the build.
USER root

ARG IMAGE_VERSION=dev

# ── supervisor (rcp control plane) ───────────────────────────────────────────
# The supervisor runs as root (PID1); the worker (pi) runs as the image user
# (ctf, uid 1000). No `kali` user exists in these images — the supervisor's
# kali lookup fails cleanly and workers run as root, which is fine in the
# disposable per-run container (BTFly's own design is a non-root ctf user, but
# root here only relaxes permissions inside the sandbox).
COPY ./runtime_agent /opt/muteki/runtime_agent
RUN chmod +x /opt/muteki/runtime_agent

# ── blackboard skill (worker <-> shared graph channel) ───────────────────────
COPY ./blackboard.SKILL.md /opt/muteki/muteki-blackboard/SKILL.md
COPY ./blackboard.py /opt/muteki/muteki-blackboard/blackboard.py
RUN chmod +x /opt/muteki/muteki-blackboard/blackboard.py && \
    ln -sf /opt/muteki/muteki-blackboard/blackboard.py /usr/local/bin/blackboard.py && \
    install -d -o ctf -g ctf /home/ctf/.pi/agent/skills/muteki-blackboard && \
    cp /opt/muteki/muteki-blackboard/SKILL.md /home/ctf/.pi/agent/skills/muteki-blackboard/SKILL.md && \
    cp /opt/muteki/muteki-blackboard/blackboard.py /home/ctf/.pi/agent/skills/muteki-blackboard/blackboard.py

# ── pi provider config (deepseek, keyless) ───────────────────────────────────
# The worker HOME is ISOLATED per worker (workspace/homes/<label>); the swarm
# links these into each isolated HOME (swarm._ensure_pi_config_links). The API
# key itself is NEVER baked — it arrives at runtime via the credential account
# projection (DEEPSEEK_API_KEY_FILE -> DEEPSEEK_API_KEY in runtime_agent).
COPY ./pi-config/settings.json /opt/muteki/pi-config/settings.json
COPY ./pi-config/models-store.json /opt/muteki/pi-config/models-store.json

# ── control plane + workspace layout (muteki conventions) ────────────────────
# CONTAINER_WORKSPACE (container_exec) is /home/kali/workspace; the host
# bind-mount creates it, but pre-create it so the mount target exists.
RUN mkdir -p /run/muteki/control /home/kali/workspace && \
    chown -R ctf:ctf /home/ctf/.pi /opt/muteki

LABEL org.opencontainers.image.title="ctf-swarm-pi" \
      org.opencontainers.image.description="BTFly pi worker + muteki supervisor (route A)" \
      org.opencontainers.image.version="${IMAGE_VERSION}"

# ENTRYPOINT: the supervisor; docker run appends --connect <host>:<port> --run-id <id>.
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/muteki/runtime_agent", "--workspace", "/home/kali/workspace"]
