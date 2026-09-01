#!/usr/bin/env bash
# Streams Particle events for a whole product into a log file,
# reconnecting automatically when the SSE connection drops.
#
# Usage:
#   PARTICLE_TOKEN=... ./particle_event_log.sh [logfile]
set -u

PRODUCT_ID="44896"
LOG_FILE="${1:-$HOME/particle_events.log}"
TOKEN="${PARTICLE_TOKEN:-}"
RETRY_DELAY=5

if [ -z "$TOKEN" ]; then
    echo "error: set PARTICLE_TOKEN environment variable" >&2
    exit 1
fi

URL="https://api.particle.io/v1/products/${PRODUCT_ID}/events"

while true; do
    echo "[$(date -Is)] --- connecting ---" >> "$LOG_FILE"
    # --keepalive-time 30: TCP keepalive probes detect a silently dead
    # connection within a few minutes; a healthy idle stream is never
    # dropped, unlike the --speed-limit/--speed-time approach.
    # grep strips the blank keepalive lines Particle sends every ~9s.
    curl -sN --no-buffer --keepalive-time 30 \
        -H "Authorization: Bearer ${TOKEN}" \
        "$URL" 2>&1 \
        | grep --line-buffered -v '^[[:space:]]*$' >> "$LOG_FILE"
    rc=${PIPESTATUS[0]}
    echo "[$(date -Is)] --- disconnected (curl exit ${rc}), retrying in ${RETRY_DELAY}s ---" >> "$LOG_FILE"
    sleep "$RETRY_DELAY"
done
