# Interactive Textual control plane for real Docker-first swarm runs.
# Worker tooling remains exclusively in the frozen Worker images.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DSWARM_IN_CONTAINER=1 \
    DSWARM_RUNTIME_MODE=docker \
    DSWARM_CONTROL_BIND=0.0.0.0 \
    DSWARM_CONTROL_HOST=tui-control

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates curl git docker-cli && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock* /app/
RUN uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev || pip install -e .
COPY . /app

ENTRYPOINT ["uv", "run", "python", "-m", "apps.tui"]
