#!/usr/bin/env bash
# Deploy Turf to a server running Docker, behind its existing Caddy.
#
# Usage:
#   TURF_HOST=user@server scripts/deploy.sh                 # code only
#   TURF_HOST=user@server TURF_RELEASE=2026-11-18.0 scripts/deploy.sh --data
#
# Layout on the server (~/turf):
#   src/                 the committed code at HEAD (git archive, no local edits)
#   data/<release>/      turf.duckdb for each uploaded release
#   data/current ->      symlink to the release being served (switching it is the rollback)
set -euo pipefail

: "${TURF_HOST:?set TURF_HOST, e.g. user@server}"
REMOTE_DIR="${TURF_REMOTE_DIR:-turf}"
WITH_DATA=false
[[ "${1:-}" == "--data" ]] && WITH_DATA=true

cd "$(dirname "$0")/.."

if [[ -n "$(git status --porcelain -- src pyproject.toml uv.lock Dockerfile deploy)" ]]; then
    echo "Uncommitted changes in deployable files; commit first so the server runs a known commit." >&2
    exit 1
fi
COMMIT="$(git rev-parse --short HEAD)"

echo "==> Uploading code at $COMMIT"
ssh "$TURF_HOST" "mkdir -p $REMOTE_DIR/src $REMOTE_DIR/data"
git archive --format=tar HEAD | ssh "$TURF_HOST" "rm -rf $REMOTE_DIR/src.new && mkdir $REMOTE_DIR/src.new && tar -x -C $REMOTE_DIR/src.new && rm -rf $REMOTE_DIR/src && mv $REMOTE_DIR/src.new $REMOTE_DIR/src && echo $COMMIT > $REMOTE_DIR/src/COMMIT"

if $WITH_DATA; then
    : "${TURF_RELEASE:?set TURF_RELEASE, e.g. 2026-11-18.0}"
    LOCAL_DB="data/$TURF_RELEASE/turf.duckdb"
    [[ -f "$LOCAL_DB" ]] || { echo "Missing $LOCAL_DB; run the pipeline first." >&2; exit 1; }
    echo "==> Uploading $LOCAL_DB"
    ssh "$TURF_HOST" "mkdir -p $REMOTE_DIR/data/$TURF_RELEASE"
    rsync -a --partial --info=progress2 "$LOCAL_DB" "$TURF_HOST:$REMOTE_DIR/data/$TURF_RELEASE/turf.duckdb"
    ssh "$TURF_HOST" "ln -sfn $TURF_RELEASE $REMOTE_DIR/data/current"
fi

echo "==> Building and starting the container"
# New data needs a recreated container: the mount resolves data/current once, at start.
RECREATE=""
$WITH_DATA && RECREATE="--force-recreate"
ssh "$TURF_HOST" "cd $REMOTE_DIR/src/deploy && ln -sfn ../../data data && docker compose up -d --build $RECREATE"

echo "==> Waiting for health check"
for _ in $(seq 1 30); do
    if ssh "$TURF_HOST" "curl -sf http://127.0.0.1:8090/healthz" >/dev/null; then
        ssh "$TURF_HOST" "curl -s http://127.0.0.1:8090/api/meta"; echo
        echo "==> Deployed $COMMIT"
        exit 0
    fi
    sleep 2
done
echo "Health check failed; recent logs:" >&2
ssh "$TURF_HOST" "docker logs --tail 50 turf" >&2
exit 1
