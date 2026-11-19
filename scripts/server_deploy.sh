#!/usr/bin/env bash
# Runs ON THE SERVER as the forced command for the CI deploy key
# (installed to /opt/turf/bin/server-deploy by scripts/setup_server.sh).
#
# The CI key can do exactly one thing: `deploy <40-char commit SHA>`. Anything
# else is refused. Registry credentials arrive on stdin (line 1: user, line 2:
# token), are used for one pull, and never touch disk outside a temp dir.
#
# Deploy = pull ghcr.io/iamandyn/turf:<sha>, switch the container to it, and
# wait until /api/meta reports that commit. If it doesn't, roll back to the
# previous image and exit non-zero so the CI job fails.
set -euo pipefail

IMAGE_REPO="ghcr.io/iamandyn/turf"
ROOT="/opt/turf"
STATE="$ROOT/state"
COMPOSE=(docker compose --project-name turf --file "$ROOT/docker-compose.yml" --env-file "$STATE/.env")

cmd="${SSH_ORIGINAL_COMMAND:-${1:-}} ${2:-}"
cmd="${cmd% }"
if [[ ! "$cmd" =~ ^deploy\ ([0-9a-f]{40})$ ]]; then
    echo "refused: expected 'deploy <40-char sha>'" >&2
    exit 2
fi
sha="${BASH_REMATCH[1]}"
image="$IMAGE_REPO:$sha"

export DOCKER_CONFIG
DOCKER_CONFIG="$(mktemp -d)"
trap 'rm -rf "$DOCKER_CONFIG"' EXIT
if read -r -t 5 registry_user && read -r -t 5 registry_token && [[ -n "$registry_token" ]]; then
    printf '%s' "$registry_token" | docker login ghcr.io -u "$registry_user" --password-stdin >/dev/null
fi

echo "==> Pulling $image"
docker pull --quiet "$image" >/dev/null

previous=""
[[ -f "$STATE/.env" ]] && previous="$(sed -n 's/^TURF_IMAGE=//p' "$STATE/.env")"

switch_to() {
    printf 'TURF_IMAGE=%s\n' "$1" > "$STATE/.env.tmp"
    mv "$STATE/.env.tmp" "$STATE/.env"
    "${COMPOSE[@]}" up -d --remove-orphans
}

serves_commit() {
    for _ in $(seq 1 60); do
        if curl -sf --max-time 2 http://127.0.0.1:8090/api/meta | grep -q "\"commit\":\"$1\""; then
            return 0
        fi
        sleep 1
    done
    return 1
}

echo "==> Switching from ${previous:-<none>} to $image"
switch_to "$image"
if serves_commit "$sha"; then
    echo "==> Deployed $sha"
    # Keep the last few images for manual rollback; drop older ones.
    docker image ls "$IMAGE_REPO" --format '{{.Tag}}' | tail -n +6 \
        | xargs -r -I{} docker image rm "$IMAGE_REPO:{}" >/dev/null 2>&1 || true
    exit 0
fi

echo "!!> $sha did not come up healthy; recent logs:" >&2
docker logs --tail 40 turf >&2 || true
if [[ -n "$previous" ]]; then
    echo "!!> Rolling back to $previous" >&2
    switch_to "$previous"
fi
exit 1
