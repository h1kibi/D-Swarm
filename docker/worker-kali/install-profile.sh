#!/usr/bin/env bash
set -euo pipefail

PROFILE="${1:?usage: install-profile.sh <tool-profile.json>}"

apt_list="$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1])).get("apt", [])))' "$PROFILE")"
pip_list="$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1])).get("pip", [])))' "$PROFILE")"
go_list="$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1])).get("go", [])))' "$PROFILE")"
clone_list="$(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1])).get("clone", [])))' "$PROFILE")"
gem_list="$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1])).get("gem", [])))' "$PROFILE")"
postinstall_list="$(python3 -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1])).get("postinstall", [])))' "$PROFILE")"

if [ -n "$apt_list" ]; then
  apt-get update -q
  # shellcheck disable=SC2086
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends $apt_list
fi

if [ -n "$pip_list" ]; then
  # Kali's Python is externally managed; the disposable worker image installs
  # into the system environment on purpose.
  # shellcheck disable=SC2086
  python3 -m pip install --no-cache-dir --break-system-packages $pip_list
fi

if [ -n "$go_list" ]; then
  export PATH="$PATH:$(go env GOPATH)/bin"
  for spec in $go_list; do
    go install "$spec"
  done
fi

if [ -n "$clone_list" ]; then
  mkdir -p /opt/ctf-tools
  while IFS= read -r url; do
    [ -z "$url" ] && continue
    name="$(basename "$url" .git)"
    if [ ! -d "/opt/ctf-tools/$name" ]; then
      git clone --depth 1 "$url" "/opt/ctf-tools/$name"
    fi
  done <<< "$clone_list"
fi

if [ -n "$gem_list" ]; then
  # shellcheck disable=SC2086
  gem install --no-document $gem_list
fi

if [ -n "$postinstall_list" ]; then
  while IFS= read -r cmd; do
    [ -z "$cmd" ] && continue
    bash -c "$cmd"
  done <<< "$postinstall_list"
fi
