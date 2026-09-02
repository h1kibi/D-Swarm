#!/usr/bin/env bash
# Rebuild the BTFly base + category pi images with the CALIBRATED pi version
# (PI_VERSION=0.83.0 — the version the PiDriver was calibrated against on the
# host; the previous images pinned 0.81.1 via the base Dockerfile default).
# Then run ./build.sh to rebuild the ctf-swarm-pi-* worker images on top.
#
# Failure-resilient: each image builds independently; failures are collected
# and reported at the end (exit 1 if any failed), so one flaky apt/pip step
# doesn't block the rest of the chain. Re-run to retry the failed ones.
#
# Usage: ./docker/worker-pi/build-base.sh   (PI_VERSION env overrides)
set -uo pipefail

PI_VERSION="${PI_VERSION:-0.84.1}"
# Debian apt mirrors for the BTFly stage images — TUNA 403s some hosts, so the
# mirror is overridable here instead of editing the vendored Dockerfiles.
APT_MIRROR="${APT_MIRROR:-https://mirrors.aliyun.com/debian}"
APT_SECURITY_MIRROR="${APT_SECURITY_MIRROR:-https://mirrors.aliyun.com/debian-security}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BTFLY="$(cd "$HERE/../../references/btfly" && pwd)"
cd "$BTFLY"

failed=()

build_one() {
  local name="$1" dockerfile="$2" extra=()
  if [ -n "${3:-}" ]; then extra=("--build-arg" "$3"); fi
  extra+=("--build-arg" "APT_MIRROR=$APT_MIRROR"
          "--build-arg" "APT_SECURITY_MIRROR=$APT_SECURITY_MIRROR")
  echo ">> [$name] ..."
  if ! docker build --platform linux/amd64 --load \
    "${extra[@]}" -t "$name" -f "$dockerfile" .; then
    echo "!! [$name] BUILD FAILED"
    failed+=("$name")
  fi
}

build_one ctf-agent-pi-base:0.1.0 images/base/Dockerfile "PI_VERSION=$PI_VERSION"
for cat in web crypto pwn reverse forensics misc; do
  build_one "ctf-agent-pi-$cat:0.1.0" "images/$cat/Dockerfile"
done

if [ ${#failed[@]} -gt 0 ]; then
  echo ">> FAILED: ${failed[*]} (re-run to retry)"
  exit 1
fi
echo ">> done. verify:"
docker run --rm --entrypoint sh ctf-agent-pi-base:0.1.0 -c 'pi --version'
