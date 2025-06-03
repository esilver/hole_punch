#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
set -e

# Determine a working 'timeout' command (GNU Coreutils vs. macOS).
if command -v timeout >/dev/null 2>&1; then
  TIMEOUT_CMD="timeout"
elif command -v gtimeout >/dev/null 2>&1; then
  # Homebrew coreutils installs the binary as 'gtimeout'
  TIMEOUT_CMD="gtimeout"
  echo "Using 'gtimeout' as the timeout implementation."
else
  echo "Warning: 'timeout' command not found. Continuing without enforced time limits."
  # Define a shim so the rest of the script can keep using the same syntax:
  timeout() { local _duration=$1; shift; "${@}"; }
  TIMEOUT_CMD="timeout" # The stub function defined above
fi

# Helper to invoke commands with the resolved timeout (or none if stubbed)
run_with_timeout() {
  local duration=$1; shift
  if [ -n "${TIMEOUT_CMD}" ] && command -v ${TIMEOUT_CMD} >/dev/null 2>&1; then
    ${TIMEOUT_CMD} "${duration}" "${@}"
  else
    # Stubbed: ignore duration and execute directly
    "${@}"
  fi
}

# Load environment variables
if [ -f .envrc ]; then
    echo "Loading environment variables from .envrc..."
    set -a
    source .envrc
    set +a
else
    echo "Error: .envrc file not found. Please create it with the necessary environment variables." >&2
    exit 1
fi

set +e # Temporarily allow us to test for missing commands without exiting
if ! command -v gcloud >/dev/null 2>&1; then
  echo "Error: 'gcloud' CLI not found in PATH. Please install the Google Cloud SDK or ensure it's on PATH before running this script." >&2
  exit 1
fi
set -e # Re-enable immediate exit on failures

echo "--- Starting Rebuild and Redeployment Process for Go Services ---"

# === 1. Delete existing services AND build new images concurrently ===
echo "Step 1: Deleting services and building new images concurrently (with timeouts to avoid hanging)…"

# Define timeouts (override via env if needed)
DELETE_TIMEOUT_SEC=${DELETE_TIMEOUT_SEC:-180}
BUILD_TIMEOUT_SEC=${BUILD_TIMEOUT_SEC:-1800}

# --- Deletions (run in background with timeout) ---
echo "Deleting Rendezvous service (${RENDEZVOUS_SERVICE_NAME})…"
run_with_timeout ${DELETE_TIMEOUT_SEC}s gcloud run services delete "${RENDEZVOUS_SERVICE_NAME}" \
  --platform=managed --region="${REGION}" --project="${PROJECT_ID}" --quiet & rend_delete_pid=$!

echo "Deleting Worker service (${WORKER_SERVICE_NAME})…"
run_with_timeout ${DELETE_TIMEOUT_SEC}s gcloud run services delete "${WORKER_SERVICE_NAME}" \
  --platform=managed --region="${REGION}" --project="${PROJECT_ID}" --quiet & worker_delete_pid=$!

# --- Docker image builds (run in background with timeout) ---
BUILD_PLATFORM="linux/amd64"

echo "Building and pushing Rendezvous service image with Docker Buildx…"
run_with_timeout ${BUILD_TIMEOUT_SEC}s docker buildx build --platform "${BUILD_PLATFORM}" \
  -t "${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:${RENDEZVOUS_IMAGE_TAG}" \
  -f deployments/Dockerfile.rendezvous . --push & rend_build_pid=$!

echo "Building and pushing Worker service image with Docker Buildx…"
run_with_timeout ${BUILD_TIMEOUT_SEC}s docker buildx build --platform "${BUILD_PLATFORM}" \
  -t "${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG}" \
  -f deployments/Dockerfile.worker . --push & worker_build_pid=$!

# --- Wait for all four background jobs (deletes + builds) ---
echo "Waiting for deletions and builds to complete…"

# Helper to wait safely and report status without stopping script on failure
wait_and_report() {
  local pid=$1
  local description=$2
  if wait "$pid"; then
    echo "$description completed successfully."
  else
    echo "Warning: $description exited with an error or timed out (exit code $?). Continuing."
  fi
}

wait_and_report $rend_delete_pid "Rendezvous service deletion"
wait_and_report $worker_delete_pid "Worker service deletion"
wait_and_report $rend_build_pid "Rendezvous image build"
wait_and_report $worker_build_pid "Worker image build"

echo "All deletions and builds finished."

# Add a small delay to ensure resources are fully cleared before redeploying
sleep 10

# === 2. Deploy services concurrently ===
echo "Step 2: Deploying Rendezvous and Worker services concurrently…"

# Timeout for deployments
DEPLOY_TIMEOUT_SEC=${DEPLOY_TIMEOUT_SEC:-600}
# Maximum request timeout (for WebSocket connections)
REQUEST_TIMEOUT_SEC=${REQUEST_TIMEOUT_SEC:-3600}

# Rendezvous deploy
run_with_timeout ${DEPLOY_TIMEOUT_SEC}s gcloud run deploy "${RENDEZVOUS_SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --image "${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:${RENDEZVOUS_IMAGE_TAG}" \
  --platform=managed \
  --region "${REGION}" \
  --allow-unauthenticated \
  --timeout="${REQUEST_TIMEOUT_SEC}s" \
  --session-affinity \
  --set-env-vars="PING_INTERVAL_SEC=${PING_INTERVAL_SEC:-30},PING_TIMEOUT_SEC=${PING_TIMEOUT_SEC:-60}" & rend_deploy_pid=$!

# Worker deploy
run_with_timeout ${DEPLOY_TIMEOUT_SEC}s gcloud run deploy "${WORKER_SERVICE_NAME}" \
  --image "${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated \
  --timeout="${REQUEST_TIMEOUT_SEC}s" \
  --set-env-vars="RENDEZVOUS_SERVICE_URL=${RENDEZVOUS_URL},INTERNAL_UDP_PORT=${INTERNAL_UDP_PORT:-8081},STUN_HOST=${STUN_HOST:-stun.l.google.com},STUN_PORT=${STUN_PORT:-19302},BENCHMARK_GCS_URL=${BENCHMARK_GCS_URL},BENCHMARK_CHUNK_SIZE=${BENCHMARK_CHUNK_SIZE:-8192}" \
  --cpu=4 \
  --memory=8Gi \
  --no-cpu-throttling \
  --vpc-egress=all-traffic \
  --network=ip-worker-vpc \
  --subnet=ip-worker-subnet \
  --min-instances=2 \
  --max-instances=2 & worker_deploy_pid=$!

# Wait for deployments
echo "Waiting for service deployments to finish…"
wait_and_report $rend_deploy_pid "Rendezvous service deployment"
wait_and_report $worker_deploy_pid "Worker service deployment"

echo "Both services deployed (or attempted) concurrently."

# === 3. Get service URLs ===
echo ""
echo "Getting service URLs..."
RENDEZVOUS_URL=$(gcloud run services describe ${RENDEZVOUS_SERVICE_NAME} --platform=managed --region=${REGION} --format='value(status.url)' 2>/dev/null || echo "Failed to get URL")
WORKER_URL=$(gcloud run services describe ${WORKER_SERVICE_NAME} --platform=managed --region=${REGION} --format='value(status.url)' 2>/dev/null || echo "Failed to get URL")

echo ""
echo "=== Deployment Complete ==="
echo "Rendezvous Service: ${RENDEZVOUS_URL}"
echo "Rendezvous Admin: ${RENDEZVOUS_URL}/admin"
echo "Worker Service: ${WORKER_URL}"
echo ""
echo "Note: Workers will automatically connect to the rendezvous service."
echo "--- Rebuild and Redeployment Process Completed ---"