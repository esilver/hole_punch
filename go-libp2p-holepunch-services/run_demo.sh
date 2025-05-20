#!/usr/bin/env bash
# Run a local demo: starts the rendezvous in the background, waits until it prints
# a multi-address, then launches a worker pointed at that address.
# When you press Ctrl-C the script will terminate both processes.
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "$0")"; pwd)"
cd "$ROOT_DIR"

RV_LOG=$(mktemp)

cleanup() {
  if [[ -n "${RV_PID:-}" ]]; then
     kill "$RV_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT

echo "Starting rendezvous..."
(cd rendezvous && go run .) > "$RV_LOG" 2>&1 &
RV_PID=$!

# Wait for the address line containing /p2p/
ADDR=""
for i in {1..50}; do
  if grep -m1 '/p2p/' "$RV_LOG" > /dev/null; then
     ADDR=$(grep -m1 '/p2p/' "$RV_LOG" | awk '{print $1}')
     break
  fi
  sleep 0.2
done
if [[ -z "$ADDR" ]]; then
  echo "Failed to detect rendezvous address; log:\n$(cat "$RV_LOG")" >&2
  exit 1
fi

echo "Rendezvous address detected: $ADDR"

echo "Launching worker..."
(cd worker && go run . -rendezvous "$ADDR") 