#!/usr/bin/env bash
# Streams Particle events for one product device into a log file,
# reconnecting automatically when the SSE connection drops.
#
# Usage:
#   PARTICLE_TOKEN=... ./particle_event_log.sh [logfile]
set -u

PRODUCT_ID="44896"
DEVICE_ID="e00fce68532d1a8e8491bd55"
LOG_FILE="${1:-$HOME/particle_events.log}"
TOKEN="${PARTICLE_TOKEN:-}"
RETRY_DELAY=5

if [ -z "$TOKEN" ]; then
    echo "error: set PARTICLE_TOKEN environment variable" >&2
    exit 1
fi

URL="https://api.particle.io/v1/products/${PRODUCT_ID}/devices/${DEVICE_ID}/events"

while true; do
    echo "[$(date -Is)] --- connecting ---" >> "$LOG_FILE"
    # --speed-time 90: if nothing arrives for 90s (not even keepalives),
    # treat the connection as dead and let the loop reconnect.
    curl -sN --no-buffer \
        --speed-limit 1 --speed-time 90 \
        -H "Authorization: Bearer ${TOKEN}" \
        "$URL" >> "$LOG_FILE" 2>&1
    rc=$?
    echo "[$(date -Is)] --- disconnected (curl exit ${rc}), retrying in ${RETRY_DELAY}s ---" >> "$LOG_FILE"
    sleep "$RETRY_DELAY"
done
