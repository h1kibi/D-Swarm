#!/usr/bin/env bash
# Build the route-A pi worker images on top of the BTFly pi images (already
# present locally: ctf-agent-pi-{base,web,crypto,pwn,reverse,forensics,misc}:0.1.0).
# Each image = BTFly image + muteki runtime-agent (rcp supervisor) + blackboard
# skill + pi provider config, ENTRYPOINT switched to the supervisor so the muteki
# container backend can drive pi workers (one container per run, per category).
#
# Usage: ./docker/worker-pi/build.sh [repo] [version]
#   repo:    image repository prefix (default: ctf-swarm-pi)
#   version: version tag (default: 0.1.0)
set -euo pipefail

REPO="${1:-ctf-swarm-pi}"
VERSION="${2:-0.1.0}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

echo ">> [1/3] cross-compiling runtime-agent (linux/amd64, static)..."
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
  go build -C "$REPO_ROOT/cmd/runtime-agent" -trimpath -ldflags="-s -w" \
    -o "$HERE/runtime_agent" .
ls -la "$HERE/runtime_agent"

echo ">> [2/3] syncing blackboard skill + pi config into build context..."
cp "$REPO_ROOT/skills/muteki-blackboard/SKILL.md" "$HERE/blackboard.SKILL.md"
cp "$REPO_ROOT/skills/muteki-blackboard/blackboard.py" "$HERE/blackboard.py"
chmod +x "$HERE/blackboard.py"
test -f "$HERE/pi-config/settings.json" || \
  { echo "missing pi-config/settings.json"; exit 1; }
test -f "$HERE/pi-config/models-store.json" || \
  { echo "missing pi-config/models-store.json"; exit 1; }

build_one() {
  local cat="$1"  # base | web | crypto | pwn | reverse | forensics | misc
  local base_img="ctf-agent-pi-${cat}:0.1.0"
  local tag="${REPO}-${cat}:${VERSION}"
  echo ">> [3/3] docker build $tag (FROM $base_img) ..."
  docker build --platform linux/amd64 --load \
    --build-arg "BASE_IMAGE=${base_img}" \
    --build-arg "IMAGE_VERSION=${VERSION}" \
    -t "$tag" "$HERE"
  echo ">>   ok: $tag"
}

for cat in base web crypto pwn reverse forensics misc; do
  build_one "$cat"
done

echo ">> done. quick verify:"
echo "   docker run --rm --entrypoint sh ctf-swarm-pi-web:${VERSION} -c 'which pi; ls /opt/muteki'"
