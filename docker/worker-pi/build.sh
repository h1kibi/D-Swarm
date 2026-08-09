#!/usr/bin/env bash
# Build the route-A pi worker images on a FULL Kali toolchain base.
#   base (Dockerfile):    kalilinux/kali-rolling + all kali-tools-* + the pi
#                         runtime (copied from BTFly) + supervisor + blackboard
#                         skill + pi provider config + ALL BTFly category skills.
#   <dir> (Dockerfile.direction): a THIN layer on base adding only the
#                         direction's prompt/skills/extensions/provider defaults.
# The ENTRYPOINT is the supervisor so the dswarm container backend can drive pi
# workers (one container per run, per direction).
#
# Stage prerequisite: the base Dockerfile extracts the pi runtime and the
# BTFly category skills from `ctf-agent-pi-*:0.1.0` stage images (built from
# references/btfly/images). Those stages are NOT kept after a build; if they
# are missing this script exits with a clear message instead of a cryptic
# `FROM` error - just run ./docker/worker-pi/build-base.sh first.
#
# Usage: ./docker/worker-pi/build.sh [repo] [version]
#   repo:    image repository prefix (default: ctf-swarm-pi)
#   version: version tag (default: 0.3.0-rc.1)
set -euo pipefail

REPO="${1:-ctf-swarm-pi}"
VERSION="${2:-0.3.0-rc.1}"
KALI_BASE="${DSWARM_KALI_BASE_IMAGE:-kalilinux/kali-rolling}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

echo ">> [1/4] cross-compiling runtime-agent (linux/amd64, static)..."
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
  go build -C "$REPO_ROOT/cmd/runtime-agent" -trimpath -ldflags="-s -w" \
    -o "$HERE/runtime_agent" .
ls -la "$HERE/runtime_agent"

echo ">> [2/4] syncing blackboard skill + pi config into build context..."
cp "$REPO_ROOT/skills/dswarm-blackboard/SKILL.md" "$HERE/blackboard.SKILL.md"
cp "$REPO_ROOT/skills/dswarm-blackboard/blackboard.py" "$HERE/blackboard.py"
chmod +x "$HERE/blackboard.py"
test -f "$HERE/pi-config/settings.json" || \
  { echo "missing pi-config/settings.json"; exit 1; }
test -f "$HERE/pi-config/models-store.json" || \
  { echo "missing pi-config/models-store.json"; exit 1; }
test -f "$HERE/pi-config/models.json" || \
  { echo "missing pi-config/models.json"; exit 1; }

echo ">> [2/4] validating pi extensions (layout, imports, models, tsc)..."
python3 "$HERE/scripts/check_pi_extensions.py"

STAGE_IMAGES=(
  "ctf-agent-pi-base:0.1.0"
  "ctf-agent-pi-web:0.1.0"
  "ctf-agent-pi-pwn:0.1.0"
  "ctf-agent-pi-reverse:0.1.0"
  "ctf-agent-pi-crypto:0.1.0"
  "ctf-agent-pi-forensics:0.1.0"
  "ctf-agent-pi-misc:0.1.0"
)
missing=()
for img in "${STAGE_IMAGES[@]}"; do
  docker image inspect "$img" >/dev/null 2>&1 || missing+=("$img")
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "!! missing BTFly stage image(s): ${missing[*]}" >&2
  echo "!! the base Dockerfile extracts pi + category skills from these stages." >&2
  echo "!! run ./docker/worker-pi/build-base.sh first, then re-run this script." >&2
  exit 1
fi

echo ">> [3/4] docker build ${REPO}-base:${VERSION} (FROM $KALI_BASE, full toolchain)..."
docker build --platform linux/amd64 --load \
  --build-arg "BASE_IMAGE=${KALI_BASE}" \
  --build-arg "IMAGE_VERSION=${VERSION}" \
  -t "${REPO}-base:${VERSION}" "$HERE"
echo ">>   ok: ${REPO}-base:${VERSION}"

build_one() {
  local dir="$1"  # web | pwn | rev | crypto | misc | forensics | aisec
  local tag="${REPO}-${dir}:${VERSION}"
  echo ">> [4/4] docker build $tag (FROM ${REPO}-base:${VERSION} + direction layer)..."
  docker build --platform linux/amd64 --load \
    -f "$HERE/Dockerfile.direction" \
    --build-arg "BASE_IMAGE=${REPO}-base:${VERSION}" \
    --build-arg "IMAGE_VERSION=${VERSION}" \
    --build-arg "DIRECTION=${dir}" \
    -t "$tag" "$HERE"
  echo ">>   ok: $tag"
}

for dir in web pwn rev crypto misc forensics aisec; do
  build_one "$dir"
done

# The web UI/default profile launches DEFAULT_WORKER_IMAGE.
# Keep a generic compatibility tag pointing at the full Kali base, otherwise a
# freshly built worker set only has per-direction tags and dispatch preflight
# fails with "image missing or unavailable: <DEFAULT_WORKER_IMAGE>".
docker tag "${REPO}-base:${VERSION}" "${REPO}:${VERSION}"

echo ">> done. quick verify:"
echo "   docker run --rm --entrypoint sh ${REPO}:${VERSION} -c 'which pi; ls /opt/dswarm'"
