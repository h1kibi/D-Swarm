FROM node:24-bookworm-slim AS node-runtime

FROM kalilinux/kali-rolling

ARG PI_VERSION=0.83.0
ARG IMAGE_VERSION=dev
ARG DEBIAN_FRONTEND=noninteractive

COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \
    npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}" && \
    npm cache clean --force

RUN apt-get update -q && \
    apt-get install -y -q --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      file \
      git \
      golang-go \
      jq \
      less \
      python3 \
      python3-pip \
      procps \
      ripgrep \
      tini \
      unzip \
      xz-utils && \
    groupadd --gid 1000 ctf && \
    useradd --uid 1000 --gid ctf --create-home --shell /bin/bash ctf && \
    mkdir -p /run/dswarm/control /home/kali/workspace /opt/dswarm/pi-config && \
    chown -R ctf:ctf /home/ctf /home/kali/workspace /opt/dswarm

RUN echo "ctf ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

RUN cat > /usr/local/sbin/dswarm-auto-sudo <<'EOF' && \
    chmod +x /usr/local/sbin/dswarm-auto-sudo && \
    ln -sf /usr/local/sbin/dswarm-auto-sudo /usr/local/bin/apt-get && \
    ln -sf /usr/local/sbin/dswarm-auto-sudo /usr/local/bin/apt && \
    ln -sf /usr/local/sbin/dswarm-auto-sudo /usr/local/bin/dpkg
#!/bin/sh
name="$(basename "$0")"
real="/usr/bin/$name"
if [ "$(id -u)" = "0" ]; then
  exec "$real" "$@"
fi
exec sudo -n "$real" "$@"
EOF

COPY docker/worker-kali/runtime_agent /opt/dswarm/runtime_agent
COPY docker/worker-kali/pi-config/ /opt/dswarm/pi-config/
# Skills/extensions now come from the worker-pi layout (the repo-root
# `extensions/` package was consolidated into docker/worker-pi).
COPY skills/dswarm-blackboard/ /home/ctf/.pi/agent/skills/dswarm-blackboard/
COPY docker/worker-pi/base-skills/ /home/ctf/.pi/agent/skills/
COPY docker/worker-pi/directions/web/skills/ /home/ctf/.pi/agent/skills/
COPY docker/worker-pi/directions/pwn/skills/ /home/ctf/.pi/agent/skills/
COPY docker/worker-pi/directions/rev/skills/ /home/ctf/.pi/agent/skills/
COPY docker/worker-pi/directions/crypto/skills/ /home/ctf/.pi/agent/skills/
COPY docker/worker-pi/directions/forensics/skills/ /home/ctf/.pi/agent/skills/
COPY docker/worker-pi/directions/misc/skills/ /home/ctf/.pi/agent/skills/
COPY docker/worker-pi/directions/aisec/skills/ /home/ctf/.pi/agent/skills/
COPY docker/worker-pi/pi-config/extensions/*.ts /home/ctf/.pi/agent/extensions/

# Offline knowledge base and PoC repos: usable even when the target/network is
# restricted, and queried with rg instead of web search.
RUN mkdir -p /home/ctf/knowledges /home/ctf/pocs /home/ctf/.local/nuclei-templates && \
    cd /home/ctf/knowledges && \
      git clone --depth=1 https://github.com/swisskyrepo/PayloadsAllTheThings.git && \
      git clone --depth=1 https://github.com/swisskyrepo/InternalAllTheThings.git && \
      git clone --depth=1 https://github.com/HackTricks-wiki/hacktricks.git && \
      git clone --depth=1 https://github.com/HackTricks-wiki/hacktricks-cloud.git && \
    cd /home/ctf/pocs && \
      git clone --depth=1 https://github.com/vulhub/vulhub.git && \
      git clone --depth=1 https://github.com/Threekiii/Awesome-POC.git && \
    git clone --depth=1 https://github.com/projectdiscovery/nuclei-templates.git \
      /home/ctf/.local/nuclei-templates && \
    find /home/ctf/knowledges /home/ctf/pocs /home/ctf/.local/nuclei-templates \
      -name .git -type d -prune -exec rm -rf {} + && \
    chown -R ctf:ctf /home/ctf/knowledges /home/ctf/pocs /home/ctf/.local

COPY docker/worker-kali/AGENTS.md /opt/dswarm/AGENTS.md
RUN mkdir -p /home/kali/workspace && \
    cp /opt/dswarm/AGENTS.md /home/ctf/.pi/agent/AGENTS.md && \
    cp /opt/dswarm/AGENTS.md /home/kali/workspace/AGENTS.md && \
    cp /opt/dswarm/AGENTS.md /home/kali/workspace/CLAUDE.md && \
    chown -R ctf:ctf /home/ctf/.pi /home/kali/workspace

RUN chmod +x /opt/dswarm/runtime_agent && \
    ln -sf /home/ctf/.pi/agent/skills/dswarm-blackboard/blackboard.py /usr/local/bin/blackboard.py && \
    mkdir -p /home/ctf/.pi/agent/extensions && \
    cp -r /opt/dswarm/pi-config/settings.json /home/ctf/.pi/agent/settings.json && \
    cp -r /opt/dswarm/pi-config/models-store.json /home/ctf/.pi/agent/models-store.json && \
    cp -r /opt/dswarm/pi-config/models.json /home/ctf/.pi/agent/models.json && \
    mkdir -p /home/ctf/.pi/agent/extensions && \
    cp -r /opt/dswarm/pi-config/extensions/* /home/ctf/.pi/agent/extensions/ && \
    chown -R ctf:ctf /home/ctf/.pi

ENV HOME=/home/ctf \
    XDG_CACHE_HOME=/home/ctf/.cache \
    XDG_CONFIG_HOME=/home/ctf/.config \
    XDG_DATA_HOME=/home/ctf/.local/share \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

LABEL org.opencontainers.image.title="dswarm-worker-pi" \
      org.opencontainers.image.version="${IMAGE_VERSION}"

WORKDIR /home/ctf/workspace
USER ctf
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/dswarm/runtime_agent", "--workspace", "/home/kali/workspace"]
