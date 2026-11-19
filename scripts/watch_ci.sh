#!/usr/bin/env bash
# Wait for the GitHub Actions run of a commit to finish and print each job's
# result (and any failed steps). Uses the public API, so no gh CLI or token.
#
# Usage:
#   scripts/watch_ci.sh            # current HEAD
#   scripts/watch_ci.sh <sha>
set -euo pipefail

REPO="${TURF_REPO:-iamandyn/turf}"
# Full SHA: the API matches head_sha exactly, so a short one never finds the run.
SHA="$(git -C "$(dirname "$0")/.." rev-parse "${1:-HEAD}")"
API="https://api.github.com/repos/$REPO/actions"

run_id=""
for _ in $(seq 1 90); do
    status="$(curl -sf "$API/runs?head_sha=$SHA" | python3 -c '
import json, sys
# Only our workflow: Dependabot and others can run on the same commit.
runs = [r for r in json.load(sys.stdin).get("workflow_runs", []) if r["name"] == "CI/CD"]
r = runs[0] if runs else {"status": "queued", "conclusion": None, "id": "-"}
print(r["status"], r["conclusion"], r["id"])
')"
    echo "$(date +%T) $status"
    run_id="${status##* }"
    [[ "$status" == completed* ]] && break
    sleep 20
done

curl -sf "$API/runs/$run_id/jobs" | python3 -c '
import json, sys
for job in json.load(sys.stdin)["jobs"]:
    print(job["name"] + ":", job["conclusion"])
    for step in job["steps"]:
        if step["conclusion"] not in ("success", "skipped"):
            print("    step failed:", step["name"], "(" + str(step["conclusion"]) + ")")
'
echo "https://github.com/$REPO/actions/runs/$run_id"
[[ "$status" == "completed success"* ]]
