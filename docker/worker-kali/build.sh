#!/usr/bin/env bash
# Build the single all-in-one Kali + pi + ctf-skills worker image.
#
# Usage: ./docker/worker-kali/build.sh [repo] [version]
#   repo:    image repository prefix (default: ghcr.io/h1kibi/dswarm-worker-pi)
#   version: version tag (default: 0.3.0-rc.1)
set -euo pipefail

REPO="${1:-ghcr.io/h1kibi/dswarm-worker-pi}"
VERSION="${2:-0.3.0-rc.1}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

echo ">> [1/4] cross-compiling runtime-agent (linux/amd64, static)..."
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
  go build -C "$REPO_ROOT/cmd/runtime-agent" -trimpath -ldflags="-s -w" \
    -o "$HERE/runtime_agent" .
ls -la "$HERE/runtime_agent"

echo ">> [2/3] building single Kali pi image..."
docker build --platform linux/amd64 --load \
  -f "$HERE/base/Dockerfile" \
  --build-arg "IMAGE_VERSION=${VERSION}" \
  -t "${REPO}:${VERSION}" \
  "$REPO_ROOT"

echo ">> [3/3] done. quick verify:"
echo "   docker run --rm --entrypoint sh ${REPO}:${VERSION} -c 'which pi; pi --version'"
