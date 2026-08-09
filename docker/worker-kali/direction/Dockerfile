ARG BASE_IMAGE=ctf-swarm-worker-kali:dev
FROM ${BASE_IMAGE}

ARG DIRECTION=web
ARG IMAGE_VERSION=dev
USER root

# The all-in-one base image already bakes every direction's skills and the
# D-Swarm base extensions (the repo-root `extensions/` package was consolidated
# into docker/worker-pi), so the direction layer is a thin naming/label layer.

LABEL org.opencontainers.image.title="ctf-swarm-pi-${DIRECTION}" \
      org.opencontainers.image.version="${IMAGE_VERSION}"

USER ctf
