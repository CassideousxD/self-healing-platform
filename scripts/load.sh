#!/usr/bin/env bash
set -euo pipefail

API_URL="${API_URL:-http://localhost:8000}"
FRONTEND_URL="${FRONTEND_URL:-http://localhost:8081}"

echo "Starting steady load generator against API at $API_URL..."
echo "Authenticating via frontend proxy ($FRONTEND_URL/auth/login)..."

LOGIN_RESP=$(curl -sf -X POST "$FRONTEND_URL/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"demo","password":"demo123"}' 2>/dev/null || true)

TOKEN=$(echo "$LOGIN_RESP" | sed -E 's/.*"token":"([^"]+)".*/\1/')

if [[ -z "$TOKEN" || "$TOKEN" == *'"error"'* ]]; then
  echo "Failed to obtain auth token. Response: $LOGIN_RESP"
  echo "Retrying via direct auth fallback if port-forwarded or available..."
  exit 1
fi

echo "Authenticated successfully."
echo "Generating ~5 req/s (POST /jobs and GET /stats). Press Ctrl+C to stop."

DURATION="${1:-0}"
START_TIME=$(date +%s)

COUNT=0
stop() {
  echo ""
  echo "Load generation stopped cleanly. Total requests sent: $COUNT"
  exit 0
}

trap stop SIGINT SIGTERM

while true; do
  if [[ "$DURATION" -gt 0 ]]; then
    NOW=$(date +%s)
    if (( NOW - START_TIME >= DURATION )); then
      stop
    fi
  fi
  # Request 1: POST /jobs
  R1=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API_URL/jobs" \
    -H "Authorization: $TOKEN" || echo "err")
  COUNT=$((COUNT + 1))
  sleep 0.2

  # Request 2: GET /stats
  R2=$(curl -s -o /dev/null -w "%{http_code}" "$API_URL/stats" || echo "err")
  COUNT=$((COUNT + 1))
  echo "[req $COUNT] POST /jobs -> $R1 | GET /stats -> $R2"
  sleep 0.2
done
