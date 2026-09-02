# Route-A Kali worker BASE image: full Kali toolchain (maximal richness — every
# tool any direction may need) + the pi runtime (copied from the BTFly image for
# a deterministic version, no npm network at build) + dswarm runtime-agent
# supervisor + blackboard skill + pi provider config + ALL BTFly category
# skills/knowledge. Every direction image is a THIN layer on top of this
# (Dockerfile.direction), so the heavy toolchain lives in ONE image.
#
# The BTFly images contribute the pi CLI/runtime and the per-category pi skills
# under /home/ctf/.pi/agent/skills/<cat>.

ARG BASE_IMAGE=kalilinux/kali-rolling

# ── extraction stages: pi runtime + per-category skills from BTFly ──────────
FROM ctf-agent-pi-base:0.1.0 AS btfly-base
FROM ctf-agent-pi-web:0.1.0 AS btfly-web
FROM ctf-agent-pi-pwn:0.1.0 AS btfly-pwn
FROM ctf-agent-pi-reverse:0.1.0 AS btfly-reverse
FROM ctf-agent-pi-crypto:0.1.0 AS btfly-crypto
FROM ctf-agent-pi-forensics:0.1.0 AS btfly-forensics
FROM ctf-agent-pi-misc:0.1.0 AS btfly-misc

FROM ${BASE_IMAGE}
USER root

ARG IMAGE_VERSION=dev

# ── full Kali toolchain (maximal richness) ───────────────────────────────────
# kali-linux-headless = the standard headless toolset; the kali-tools-*
# metapackages add the web/exploitation/RE/crypto-stego/forensics/OSINT/password
# /sniffing/vuln/fuzzing/post-exploitation breadth. python3-venv/pip are needed
# by worker scripts; tini is the supervisor's PID-1 wrapper.
# The slim Kali base ships NO ca-certificates, so install them FIRST from the
# image's own (http) source before switching to the https kali.download mirror
# — the worker also needs a CA bundle for HTTPS targets.
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      ca-certificates && \
    rm -rf /var/lib/apt/lists/* && \
    rm -f /etc/apt/sources.list.d/* && \
    printf 'deb https://kali.download/kali kali-rolling main contrib non-free non-free-firmware\n' \
      > /etc/apt/sources.list && \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get \
      -o Acquire::Retries=5 -o Acquire::http::Timeout=60 -o Acquire::https::Timeout=60 \
      install -y --no-install-recommends \
      tini \
      kali-linux-headless \
      kali-tools-web kali-tools-exploitation kali-tools-reverse-engineering \
      kali-tools-crypto-stego kali-tools-forensics kali-tools-information-gathering \
      kali-tools-passwords kali-tools-sniffing-spoofing kali-tools-vulnerability \
      kali-tools-fuzzing kali-tools-windows-resources kali-tools-post-exploitation \
      python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/* && \
    useradd -m -u 1000 -s /bin/bash ctf

# ── CTF python/ruby extras not pulled by the kali-tools-* metapackages ───────
# A separate layer so the big install above stays cacheable. pip/gem installs
# are best-effort (network to PyPI/RubyGems can be flaky); a failure is logged,
# never allowed to break the image.
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get \
      -o Acquire::Retries=5 -o Acquire::http::Timeout=60 -o Acquire::https::Timeout=60 \
      install -y --no-install-recommends \
      python3-pwntools python3-z3 python3-pycryptodome python3-gmpy2 python3-scipy \
      ghidra ropper checksec \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv --system-site-packages /opt/venv \
    && (/opt/venv/bin/pip install --no-cache-dir \
           angr volatility3 \
         || echo "[dswarm] WARN: pip extras (angr/volatility3) failed - install at runtime") \
    && (gem install --no-document zsteg one_gadget \
         || echo "[dswarm] WARN: gem extras (zsteg/one_gadget) failed - install at runtime")

# ── dynamic-analysis + QoL supplements (image audit 2026-09-01) ──────────────
# The kali-tools-* metapackages don't pull the pwn dynamic-analysis kit
# (strace/ltrace/qemu-user/patchelf/seccomp-tools) nor ripgrep/jq/poppler; and
# the ghidra package ships analyzeHeadless OUTSIDE PATH. Small layer so a
# rebuild stays cheap; keep it after the big install above for cache locality.
RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get \
      -o Acquire::Retries=5 -o Acquire::http::Timeout=60 -o Acquire::https::Timeout=60 \
      install -y --no-install-recommends \
      strace ltrace patchelf qemu-user ripgrep jq poppler-utils sudo \
    && rm -rf /var/lib/apt/lists/* && \
    (gem install --no-document seccomp-tools \
       || echo "[dswarm] WARN: gem seccomp-tools failed - install at runtime") && \
    GHIDRA_HEADLESS="$(find /usr/share/ghidra /usr/lib/ghidra /opt -maxdepth 5 \
      -name analyzeHeadless 2>/dev/null | head -1)" && \
    if [ -n "$GHIDRA_HEADLESS" ]; then \
      ln -sf "$GHIDRA_HEADLESS" /usr/local/bin/analyzeHeadless; \
    else echo "[dswarm] WARN: analyzeHeadless not found - ghidra headless unlinked"; fi

# ── runtime self-install channel (parity with docker/worker-kali) ────────────
# Workers install missing tools on demand; the models type `apt-get` without
# sudo, so the auto-sudo wrappers transparently retry through NOPASSWD sudo
# when the caller is not root. Without this the ctf user cannot install
# anything at runtime (the tool-on-demand design breaks).
RUN echo "ctf ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ctf && chmod 440 /etc/sudoers.d/ctf && \
    printf '#!/bin/sh\nname="$(basename "$0")"\nreal="/usr/bin/$name"\n[ "$(id -u)" = "0" ] && exec "$real"\nexec sudo -n "$real"\n' \
      > /usr/local/sbin/dswarm-auto-sudo && chmod +x /usr/local/sbin/dswarm-auto-sudo && \
    ln -sf /usr/local/sbin/dswarm-auto-sudo /usr/local/bin/apt-get && \
    ln -sf /usr/local/sbin/dswarm-auto-sudo /usr/local/bin/apt && \
    ln -sf /usr/local/sbin/dswarm-auto-sudo /usr/local/bin/dpkg

# ----------------------------------------------------------------------
# Vendored external skills: base skills for every worker (MIT).
# ljagiello/ctf-skills: solve-challenge (triage/dispatcher) and ctf-writeup
# (final writeup) are direction-agnostic, so they are baked into the default
# HOME next to the BTFly category skills and linked into every isolated
# worker HOME at runtime. Per-direction skills (ctf-* and the yaklang
# host-pentest playbooks) live in the direction layer (Dockerfile.direction).
# Provenance: docker/worker-pi/THIRD_PARTY_SKILLS.md
COPY ./base-skills/solve-challenge /home/ctf/.pi/agent/skills/solve-challenge
COPY ./base-skills/ctf-writeup /home/ctf/.pi/agent/skills/ctf-writeup

# ----------------------------------------------------------------------
# Vendored skill tool bootstrap: build-time pip gap + on-demand install.
# ljagiello's install_ctf_tools.sh is distributed at a fixed image path
# (the vendored skills reference it) so workers can install missing tools
# at runtime. At build time pip_gap_fill.py pre-installs only the
# PIP_PACKAGES entries that are not importable in /opt/venv; best-effort,
# so a flaky PyPI never breaks the build (Kali kali-tools-* covers apt).
COPY ./scripts/install_ctf_tools.sh /opt/dswarm/ctf-tools/install_ctf_tools.sh
COPY ./scripts/pip_gap_fill.py /opt/dswarm/ctf-tools/pip_gap_fill.py
RUN chmod +x /opt/dswarm/ctf-tools/install_ctf_tools.sh && \
    (/opt/venv/bin/python /opt/dswarm/ctf-tools/pip_gap_fill.py \
      || echo "[dswarm] WARN: pip gap fill failed - install at runtime")


# ── pi runtime (deterministic version from BTFly; offline) ───────────────────
COPY --from=btfly-base /usr/local/bin/node /usr/local/bin/node
COPY --from=btfly-base /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \
    ln -sf ../lib/node_modules/@earendil-works/pi-coding-agent/dist/cli.js /usr/local/bin/pi && \
    chmod +x /usr/local/bin/node /usr/local/lib/node_modules/@earendil-works/pi-coding-agent/dist/cli.js

# ── pi agent home seeded from BTFly (base config + ALL category skills) ──────
COPY --from=btfly-base /home/ctf/.pi /home/ctf/.pi
COPY --from=btfly-web /home/ctf/.pi/agent/skills/web /home/ctf/.pi/agent/skills/web
COPY --from=btfly-pwn /home/ctf/.pi/agent/skills/pwn /home/ctf/.pi/agent/skills/pwn
COPY --from=btfly-reverse /home/ctf/.pi/agent/skills/reverse /home/ctf/.pi/agent/skills/reverse
COPY --from=btfly-crypto /home/ctf/.pi/agent/skills/crypto /home/ctf/.pi/agent/skills/crypto
COPY --from=btfly-forensics /home/ctf/.pi/agent/skills/forensics /home/ctf/.pi/agent/skills/forensics
COPY --from=btfly-misc /home/ctf/.pi/agent/skills/misc /home/ctf/.pi/agent/skills/misc

# ── supervisor (rcp control plane) ───────────────────────────────────────────
COPY ./runtime_agent /opt/dswarm/runtime_agent
RUN chmod +x /opt/dswarm/runtime_agent

# ── blackboard skill (worker <-> shared graph channel) ───────────────────────
COPY ./blackboard.SKILL.md /opt/dswarm/dswarm-blackboard/SKILL.md
COPY ./blackboard.py /opt/dswarm/dswarm-blackboard/blackboard.py
RUN chmod +x /opt/dswarm/dswarm-blackboard/blackboard.py && \
    ln -sf /opt/dswarm/dswarm-blackboard/blackboard.py /usr/local/bin/blackboard.py && \
    install -d -o ctf -g ctf /home/ctf/.pi/agent/skills/dswarm-blackboard && \
    cp /opt/dswarm/dswarm-blackboard/SKILL.md /home/ctf/.pi/agent/skills/dswarm-blackboard/SKILL.md && \
    cp /opt/dswarm/dswarm-blackboard/blackboard.py /home/ctf/.pi/agent/skills/dswarm-blackboard/blackboard.py

# ── pi provider config (deepseek + host model gateway; key never baked) ──────
# The worker HOME is ISOLATED per worker (workspace/homes/<label>); the swarm
# links these into each isolated HOME. The API key itself arrives at runtime via
# the credential account projection, never baked here.
COPY ./pi-config/settings.json /opt/dswarm/pi-config/settings.json
COPY ./pi-config/models-store.json /opt/dswarm/pi-config/models-store.json
COPY ./pi-config/models.json /opt/dswarm/pi-config/models.json
COPY ./pi-config/extensions/ /opt/dswarm/pi-config/extensions/
RUN cp /opt/dswarm/pi-config/settings.json /home/ctf/.pi/agent/settings.json && \
    cp /opt/dswarm/pi-config/models-store.json /home/ctf/.pi/agent/models-store.json && \
    cp /opt/dswarm/pi-config/models.json /home/ctf/.pi/agent/models.json && \
    mkdir -p /home/ctf/.pi/agent/extensions && \
    find /opt/dswarm/pi-config/extensions -maxdepth 1 -type f -name '*.ts' \
      -exec cp {} /home/ctf/.pi/agent/extensions/ \;

# ── control plane + workspace layout (dswarm conventions) ────────────────────
# CONTAINER_WORKSPACE (container_exec) is /home/kali/workspace; the host
# bind-mount creates it, but pre-create it so the mount target exists.
RUN mkdir -p /run/dswarm/control /home/kali/workspace && \
    chown -R ctf:ctf /home/ctf/.pi /opt/dswarm

LABEL org.opencontainers.image.title="ctf-swarm-pi-base" \
      org.opencontainers.image.description="Kali full-toolchain pi worker base + dswarm supervisor (route A)" \
      org.opencontainers.image.version="${IMAGE_VERSION}"

# ENTRYPOINT: the supervisor; docker run appends --connect <host>:<port> --run-id <id>.
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/dswarm/runtime_agent", "--workspace", "/home/kali/workspace"]
