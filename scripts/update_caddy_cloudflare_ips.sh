#!/usr/bin/env bash
# Refresh the Cloudflare ranges Caddy trusts for client IPs, on the server.
# Fetches Cloudflare's published lists, rewrites the `trusted_proxies static ...`
# line in /etc/caddy/Caddyfile, validates, and reloads. Restores the backup if
# validation fails. Safe to re-run (e.g. monthly).
#
# Usage:
#   TURF_HOST=ovh scripts/update_caddy_cloudflare_ips.sh
set -euo pipefail

: "${TURF_HOST:?set TURF_HOST, e.g. ovh}"

ssh "$TURF_HOST" bash -s <<'REMOTE'
set -euo pipefail
CADDYFILE=/etc/caddy/Caddyfile

ranges="$(curl -sf https://www.cloudflare.com/ips-v4; echo; curl -sf https://www.cloudflare.com/ips-v6)"
ranges="$(tr -s '[:space:]' ' ' <<<"$ranges" | sed 's/^ //; s/ $//')"
count="$(wc -w <<<"$ranges")"
# Sanity check: Cloudflare publishes ~15 IPv4 and ~7 IPv6 ranges.
if (( count < 10 )) || ! grep -Eq '^([0-9a-f.:/]+ )+[0-9a-f.:/]+$' <<<"$ranges"; then
    echo "unexpected Cloudflare range list ($count entries); not touching Caddy" >&2
    exit 1
fi

grep -q 'trusted_proxies static' "$CADDYFILE" || {
    echo "no 'trusted_proxies static' line in $CADDYFILE; add the global block from deploy/Caddyfile.snippet first" >&2
    exit 1
}

backup="$CADDYFILE.bak-$(date -u +%Y%m%dT%H%M%SZ)"
sudo cp -p "$CADDYFILE" "$backup"
sudo sed -i -E "s#(trusted_proxies static).*#\1 $ranges#" "$CADDYFILE"
if sudo caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
    sudo systemctl reload caddy
    echo "trusted $count Cloudflare ranges; backup at $backup"
else
    sudo cp -p "$backup" "$CADDYFILE"
    echo "validation failed; restored $backup" >&2
    exit 1
fi
REMOTE
