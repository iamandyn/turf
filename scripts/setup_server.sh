#!/usr/bin/env bash
# One-time (and re-runnable) server setup for Turf, run from a dev machine with
# an admin SSH login that has sudo. App releases go through CI; this is for the
# pieces CI is deliberately not allowed to change: the deploy user, its forced
# command, and the compose file.
#
# Usage:
#   TURF_HOST=ovh scripts/setup_server.sh path/to/ci_deploy_key.pub
#
# Creates:
#   user turfdeploy        in the docker group, key-only, forced command
#   /opt/turf/             root-owned: docker-compose.yml, bin/server-deploy
#   /opt/turf/state/       turfdeploy-owned: .env with the current image tag
#   /opt/turf/data/        admin-owned, group turfdeploy: releases + current symlink
set -euo pipefail

: "${TURF_HOST:?set TURF_HOST, e.g. ovh}"
PUBKEY_FILE="${1:?usage: setup_server.sh <ci-deploy-key.pub>}"
PUBKEY="$(cat "$PUBKEY_FILE")"
[[ "$PUBKEY" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+(\ .*)?$ ]] || { echo "expected an ssh-ed25519 public key" >&2; exit 2; }

cd "$(dirname "$0")/.."

ssh "$TURF_HOST" "mkdir -p /tmp/turf-setup"
scp -q deploy/docker-compose.yml scripts/server_deploy.sh "$TURF_HOST:/tmp/turf-setup/"

ssh "$TURF_HOST" PUBKEY="'$PUBKEY'" bash -s <<'REMOTE'
set -euo pipefail
admin="$(id -un)"

if ! id turfdeploy >/dev/null 2>&1; then
    sudo useradd --system --create-home --shell /bin/bash turfdeploy
fi
sudo passwd -l turfdeploy >/dev/null
sudo usermod -aG docker turfdeploy

sudo install -d -o root -g root -m 0755 /opt/turf /opt/turf/bin
sudo install -d -o turfdeploy -g turfdeploy -m 0755 /opt/turf/state
sudo install -d -o "$admin" -g turfdeploy -m 2775 /opt/turf/data
sudo install -o root -g root -m 0644 /tmp/turf-setup/docker-compose.yml /opt/turf/docker-compose.yml
sudo install -o root -g root -m 0755 /tmp/turf-setup/server_deploy.sh /opt/turf/bin/server-deploy
rm -rf /tmp/turf-setup

# The key can only run the deploy script: no shell, PTY, or forwarding.
sudo install -d -o turfdeploy -g turfdeploy -m 0700 ~turfdeploy/.ssh
printf 'restrict,command="/opt/turf/bin/server-deploy" %s\n' "$PUBKEY" \
    | sudo tee ~turfdeploy/.ssh/authorized_keys >/dev/null
sudo chown turfdeploy:turfdeploy ~turfdeploy/.ssh/authorized_keys
sudo chmod 0600 ~turfdeploy/.ssh/authorized_keys

echo "setup done"
REMOTE
