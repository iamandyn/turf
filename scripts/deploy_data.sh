#!/usr/bin/env bash
# Upload a release's serving database to the server and switch to it.
# Code deploys go through CI; data changes monthly and is too big for CI, so it
# ships separately with this script.
#
# Usage:
#   TURF_HOST=ovh scripts/deploy_data.sh 2026-11-18.0
#   TURF_HOST=ovh scripts/deploy_data.sh --rollback 2026-10-21.0   # switch only, no upload
set -euo pipefail

: "${TURF_HOST:?set TURF_HOST, e.g. ovh}"
ROLLBACK=false
if [[ "${1:-}" == "--rollback" ]]; then ROLLBACK=true; shift; fi
RELEASE="${1:?usage: deploy_data.sh [--rollback] <release>}"
[[ "$RELEASE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+$ ]] || { echo "bad release: $RELEASE" >&2; exit 2; }
REMOTE="/opt/turf/data/$RELEASE"

cd "$(dirname "$0")/.."
if ! $ROLLBACK; then
    LOCAL_DB="data/$RELEASE/turf.duckdb"
    [[ -f "$LOCAL_DB" ]] || { echo "Missing $LOCAL_DB; run the pipeline first." >&2; exit 1; }
    echo "==> Uploading $LOCAL_DB"
    ssh "$TURF_HOST" "mkdir -p $REMOTE"
    # Upload under a temp name, then rename, so a half-copied file is never served.
    rsync -a --partial --info=progress2 "$LOCAL_DB" "$TURF_HOST:$REMOTE/turf.duckdb.uploading"
    ssh "$TURF_HOST" "mv $REMOTE/turf.duckdb.uploading $REMOTE/turf.duckdb"
fi

echo "==> Switching data/current to $RELEASE and restarting"
ssh "$TURF_HOST" "test -f $REMOTE/turf.duckdb && ln -sfn $RELEASE /opt/turf/data/current \
    && docker compose --project-name turf --file /opt/turf/docker-compose.yml \
       --env-file /opt/turf/state/.env up -d --force-recreate"

for _ in $(seq 1 60); do
    if meta="$(ssh "$TURF_HOST" "curl -sf --max-time 2 http://127.0.0.1:8090/api/meta")" \
        && grep -q "\"release\":\"$RELEASE\"" <<<"$meta"; then
        echo "$meta"
        echo "==> Serving $RELEASE"
        exit 0
    fi
    sleep 1
done
echo "Turf did not come back serving $RELEASE; check 'docker logs turf'." >&2
exit 1
