#!/bin/bash
set -e # Exit immediately if a command exits with a non-zero status.

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
  timeout_shim() { local _duration=$1; shift; "${@}"; }
  TIMEOUT_CMD="timeout_shim" # The stub function defined above
fi

# Helper to invoke commands with the resolved timeout (or none if stubbed)
run_with_timeout() {
  local duration=$1; shift
  if [ "${TIMEOUT_CMD}" = "timeout_shim" ]; then
    # Stubbed: ignore duration and execute directly via the shim
    "${TIMEOUT_CMD}" "${duration}" "${@}"
  elif command -v "${TIMEOUT_CMD}" >/dev/null 2>&1; then
    "${TIMEOUT_CMD}" "${duration}" "${@}"
  else
    # Fallback if TIMEOUT_CMD was somehow set but not found, and not the shim
    echo "Warning: TIMEOUT_CMD '${TIMEOUT_CMD}' not found, running command without timeout."
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

# Check for gcloud
set +e # Temporarily allow us to test for missing commands without exiting
if ! command -v gcloud >/dev/null 2>&1; then
  echo "Error: 'gcloud' CLI not found in PATH. Please install the Google Cloud SDK or ensure it's on PATH before running this script." >&2
  exit 1
fi
set -e # Re-enable immediate exit on failures

# Redeployment script for TCP Hole Punching services
# This script assumes you have:-
#   - gcloud SDK installed and configured (logged in, project set).
#   - Docker installed and running.
#   - direnv installed and `direnv allow` has been run in this directory,
#     OR all required environment variables are already exported.
#   - Necessary GCP APIs enabled (Cloud Run, Artifact Registry).
#   - An Artifact Registry Docker repository created.

# --- Configuration (sourced from .envrc or environment) ---
# PROJECT_ID: Your Google Cloud Project ID
# REGION: Google Cloud region for services (e.g., us-central1)
# AR_REPO: Your Artifact Registry repository name
# RENDEZVOUS_IMAGE_NAME: Docker image name for the rendezvous service
# WORKER_IMAGE_NAME: Docker image name for the worker service
# RENDEZVOUS_CONTAINER_PORT: Port the rendezvous service container listens on (default 8080)
# DOCKER_BUILD_TIMEOUT_SEC: Timeout for docker build commands
# GCLOUD_DEPLOY_TIMEOUT_SEC: Timeout for gcloud deploy commands
# RENDEZVOUS_IMAGE_TAG: Tag for the rendezvous service image (e.g., latest)
# WORKER_IMAGE_TAG: Tag for the worker service image (e.g., latest)
# WORKER_A_SERVICE_NAME: Cloud Run service name for worker A
# WORKER_B_SERVICE_NAME: Cloud Run service name for worker B
# WORKER_A_INTERNAL_TCP_PORT: Internal P2P port for worker A container
# WORKER_B_INTERNAL_TCP_PORT: Internal P2P port for worker B container
# WORKER_A_CLOUDRUN_HEALTH_PORT: Health check port for worker A on Cloud Run ($PORT inside container)
# WORKER_B_CLOUDRUN_HEALTH_PORT: Health check port for worker B on Cloud Run ($PORT inside container)
# EXTERNAL_ECHO_IMAGE_NAME: Docker image name for the external echo service (NEW)
# EXTERNAL_ECHO_SERVICE_NAME: Cloud Run service name for the external echo service (NEW)
# EXTERNAL_ECHO_CONTAINER_PORT: Port the external echo service container listens on (NEW, e.g. 8080)

# --- Check for essential variables ---
if [ -z "$PROJECT_ID" ] || [ -z "$REGION" ] || [ -z "$AR_REPO" ] || \
   [ -z "$RENDEZVOUS_IMAGE_NAME" ] || [ -z "$WORKER_IMAGE_NAME" ] || \
   [ -z "$RENDEZVOUS_CONTAINER_PORT" ] || [ -z "$DOCKER_BUILD_TIMEOUT_SEC" ] || \
   [ -z "$GCLOUD_DEPLOY_TIMEOUT_SEC" ] || [ -z "$RENDEZVOUS_IMAGE_TAG" ] || \
   [ -z "$WORKER_IMAGE_TAG" ] || [ -z "$WORKER_A_SERVICE_NAME" ] || \
   [ -z "$WORKER_B_SERVICE_NAME" ] || [ -z "$WORKER_A_INTERNAL_TCP_PORT" ] || \
   [ -z "$WORKER_B_INTERNAL_TCP_PORT" ] || [ -z "$WORKER_A_CLOUDRUN_HEALTH_PORT" ] || \
   [ -z "$WORKER_B_CLOUDRUN_HEALTH_PORT" ] || [ -z "$RENDEZVOUS_SERVICE_URL" ] || \
   [ -z "$EXTERNAL_ECHO_IMAGE_NAME" ] || [ -z "$EXTERNAL_ECHO_SERVICE_NAME" ] || [ -z "$EXTERNAL_ECHO_CONTAINER_PORT" ]; then
    echo "Error: One or more required environment variables are not set."
    echo "Please ensure all of the following are defined in .envrc:"
    echo "PROJECT_ID, REGION, AR_REPO, RENDEZVOUS_IMAGE_NAME, WORKER_IMAGE_NAME, RENDEZVOUS_CONTAINER_PORT, DOCKER_BUILD_TIMEOUT_SEC, GCLOUD_DEPLOY_TIMEOUT_SEC, RENDEZVOUS_IMAGE_TAG, WORKER_IMAGE_TAG, WORKER_A_SERVICE_NAME, WORKER_B_SERVICE_NAME, WORKER_A_INTERNAL_TCP_PORT, WORKER_B_INTERNAL_TCP_PORT, WORKER_A_CLOUDRUN_HEALTH_PORT, WORKER_B_CLOUDRUN_HEALTH_PORT, RENDEZVOUS_SERVICE_URL, EXTERNAL_ECHO_IMAGE_NAME, EXTERNAL_ECHO_SERVICE_NAME, EXTERNAL_ECHO_CONTAINER_PORT"
    echo "Consider creating and configuring a .envrc file and running 'direnv allow'."
    exit 1
fi

# --- Derived Variables ---
RENDEZVOUS_SERVICE_DIR="./rendezvous_service_code"
WORKER_SERVICE_DIR="./holepunch"
EXTERNAL_ECHO_SERVICE_DIR="./external_echo_service_code"

# Remote image URL uses the tag from .envrc
RENDEZVOUS_REMOTE_IMAGE_URL="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${RENDEZVOUS_IMAGE_NAME}:${RENDEZVOUS_IMAGE_TAG}"
WORKER_REMOTE_IMAGE_URL="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${WORKER_IMAGE_NAME}:${WORKER_IMAGE_TAG}"
EXTERNAL_ECHO_REMOTE_IMAGE_URL="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${EXTERNAL_ECHO_IMAGE_NAME}:latest"

CLOUDRUN_RENDEZVOUS_SERVICE_NAME="${RENDEZVOUS_IMAGE_NAME}" # Convention: use image name as service name
CLOUDRUN_EXTERNAL_ECHO_SERVICE_NAME="${EXTERNAL_ECHO_SERVICE_NAME}"
BUILD_PLATFORM="linux/amd64" # Ensure Cloud Run compatibility

# --- Gracefully attempt to delete existing services (ignore errors if they don't exist) ---
echo "\\n[*] Attempting to delete existing Cloud Run services (if they exist)..."
set +e # Don't exit if delete fails (e.g., service not found)

echo "Deleting Rendezvous Service: ${CLOUDRUN_RENDEZVOUS_SERVICE_NAME}..."
gcloud run services delete "${CLOUDRUN_RENDEZVOUS_SERVICE_NAME}" \
    --platform managed \
    --region "${REGION}" \
    --project "${PROJECT_ID}" \
    --quiet

echo "Deleting Worker A: ${WORKER_A_SERVICE_NAME}..."
gcloud run services delete "${WORKER_A_SERVICE_NAME}" \
    --platform managed \
    --region "${REGION}" \
    --project "${PROJECT_ID}" \
    --quiet

echo "Deleting Worker B: ${WORKER_B_SERVICE_NAME}..."
gcloud run services delete "${WORKER_B_SERVICE_NAME}" \
    --platform managed \
    --region "${REGION}" \
    --project "${PROJECT_ID}" \
    --quiet

echo "Deleting External Echo Service: ${CLOUDRUN_EXTERNAL_ECHO_SERVICE_NAME}..."
gcloud run services delete "${CLOUDRUN_EXTERNAL_ECHO_SERVICE_NAME}" \
    --platform managed \
    --region "${REGION}" \
    --project "${PROJECT_ID}" \
    --quiet

set -e # Re-enable immediate exit on errors for subsequent commands
echo "[*] Finished attempting to delete existing services."

# --- Build Docker Images ---
echo "
[+] Building and Pushing Rendezvous Service Docker image for ${BUILD_PLATFORM}..."
run_with_timeout "${DOCKER_BUILD_TIMEOUT_SEC}s" docker buildx build --platform "${BUILD_PLATFORM}" \
  -t "${RENDEZVOUS_REMOTE_IMAGE_URL}" \
  -f "${RENDEZVOUS_SERVICE_DIR}/Dockerfile" "${RENDEZVOUS_SERVICE_DIR}" --push
echo "Rendezvous Service image '${RENDEZVOUS_REMOTE_IMAGE_URL}' built and pushed successfully."

echo "
[+] Building and Pushing Worker Service Docker image for ${BUILD_PLATFORM}..."
run_with_timeout "${DOCKER_BUILD_TIMEOUT_SEC}s" docker buildx build --platform "${BUILD_PLATFORM}" \
  -t "${WORKER_REMOTE_IMAGE_URL}" \
  -f "${WORKER_SERVICE_DIR}/Dockerfile" "${WORKER_SERVICE_DIR}" --push
echo "Worker Service image '${WORKER_REMOTE_IMAGE_URL}' built and pushed successfully."

echo "
[+] Building and Pushing External Echo Service Docker image for ${BUILD_PLATFORM}..."
run_with_timeout "${DOCKER_BUILD_TIMEOUT_SEC}s" docker buildx build --platform "${BUILD_PLATFORM}" \
  -t "${EXTERNAL_ECHO_REMOTE_IMAGE_URL}" \
  -f "${EXTERNAL_ECHO_SERVICE_DIR}/Dockerfile" "${EXTERNAL_ECHO_SERVICE_DIR}" --push
echo "External Echo Service image '${EXTERNAL_ECHO_REMOTE_IMAGE_URL}' built and pushed successfully."

# --- Deploy Rendezvous Service to Google Cloud Run ---
echo "
[+] Deploying Rendezvous Service (${CLOUDRUN_RENDEZVOUS_SERVICE_NAME}) to Google Cloud Run..."
run_with_timeout "${GCLOUD_DEPLOY_TIMEOUT_SEC}s" gcloud run deploy "${CLOUDRUN_RENDEZVOUS_SERVICE_NAME}" \
    --image "${RENDEZVOUS_REMOTE_IMAGE_URL}" \
    --platform managed \
    --region "${REGION}" \
    --port "${RENDEZVOUS_CONTAINER_PORT}" \
    --allow-unauthenticated \
    --project "${PROJECT_ID}" \
    --session-affinity \
    --quiet

# Ensure RENDEZVOUS_SERVICE_URL is up-to-date after deployment for workers to use
# The .envrc already has the deployed URL from the previous successful run, or a placeholder.
# For robustness in a fully automated script, one might fetch it here:
# export RENDEZVOUS_SERVICE_URL=$(gcloud run services describe "${CLOUDRUN_RENDEZVOUS_SERVICE_NAME}" --platform managed --region "${REGION}" --project "${PROJECT_ID}" --format='value(status.url)')
# echo "Fetched RENDEZVOUS_SERVICE_URL: ${RENDEZVOUS_SERVICE_URL}"
# if [ -z "${RENDEZVOUS_SERVICE_URL}" ]; then
#     echo "Error: Failed to fetch RENDEZVOUS_SERVICE_URL. Exiting." >&2
#     exit 1
# fi

# --- Deploy External Echo Service to Google Cloud Run ---
echo "
[+] Deploying External Echo Service (${CLOUDRUN_EXTERNAL_ECHO_SERVICE_NAME}) to Google Cloud Run..."
run_with_timeout "${GCLOUD_DEPLOY_TIMEOUT_SEC}s" gcloud run deploy "${CLOUDRUN_EXTERNAL_ECHO_SERVICE_NAME}" \
    --image "${EXTERNAL_ECHO_REMOTE_IMAGE_URL}" \
    --platform managed \
    --region "${REGION}" \
    --port "${EXTERNAL_ECHO_CONTAINER_PORT}" \
    --allow-unauthenticated \
    --project "${PROJECT_ID}" \
    --quiet

# Fetch the URL for the deployed echo service
export EXTERNAL_ECHO_SERVICE_URL=$(gcloud run services describe "${CLOUDRUN_EXTERNAL_ECHO_SERVICE_NAME}" --platform managed --region "${REGION}" --project "${PROJECT_ID}" --format='value(status.url)')
echo "Fetched EXTERNAL_ECHO_SERVICE_URL: ${EXTERNAL_ECHO_SERVICE_URL}"
if [ -z "${EXTERNAL_ECHO_SERVICE_URL}" ]; then
    echo "Error: Failed to fetch EXTERNAL_ECHO_SERVICE_URL for the newly deployed echo service. Exiting." >&2
    exit 1
fi

# --- Deploy Worker A to Google Cloud Run ---
echo "
[+] Deploying Worker A (${WORKER_A_SERVICE_NAME}) to Google Cloud Run..."
run_with_timeout "${GCLOUD_DEPLOY_TIMEOUT_SEC}s" gcloud run deploy "${WORKER_A_SERVICE_NAME}" \
    --image "${WORKER_REMOTE_IMAGE_URL}" \
    --platform managed \
    --region "${REGION}" \
    --port "${WORKER_A_CLOUDRUN_HEALTH_PORT}" \
    --set-env-vars "RENDEZVOUS_SERVICE_URL=${RENDEZVOUS_SERVICE_URL},INTERNAL_TCP_PORT=${WORKER_A_INTERNAL_TCP_PORT},EXTERNAL_ECHO_SERVER_HOST=${EXTERNAL_ECHO_SERVICE_URL#https://},EXTERNAL_ECHO_SERVER_PORT=${EXTERNAL_ECHO_CONTAINER_PORT}" \
    --allow-unauthenticated \
    --project "${PROJECT_ID}" \
    --quiet

# --- Deploy Worker B to Google Cloud Run ---
echo "
[+] Deploying Worker B (${WORKER_B_SERVICE_NAME}) to Google Cloud Run..."
run_with_timeout "${GCLOUD_DEPLOY_TIMEOUT_SEC}s" gcloud run deploy "${WORKER_B_SERVICE_NAME}" \
    --image "${WORKER_REMOTE_IMAGE_URL}" \
    --platform managed \
    --region "${REGION}" \
    --port "${WORKER_B_CLOUDRUN_HEALTH_PORT}" \
    --set-env-vars "RENDEZVOUS_SERVICE_URL=${RENDEZVOUS_SERVICE_URL},INTERNAL_TCP_PORT=${WORKER_B_INTERNAL_TCP_PORT},EXTERNAL_ECHO_SERVER_HOST=${EXTERNAL_ECHO_SERVICE_URL#https://},EXTERNAL_ECHO_SERVER_PORT=${EXTERNAL_ECHO_CONTAINER_PORT}" \
    --allow-unauthenticated \
    --project "${PROJECT_ID}" \
    --quiet

# (No need to fetch DEPLOYED_URL for rendezvous again as the user is instructed to update .envrc)
# The worker URLs are not critical for this script's output.

echo "
----------------------------------------------------------------------"
echo "Rendezvous Service (${CLOUDRUN_RENDEZVOUS_SERVICE_NAME}) deployment/update initiated."
echo "External Echo Service (${CLOUDRUN_EXTERNAL_ECHO_SERVICE_NAME}) deployment/update initiated."
echo "Worker A (${WORKER_A_SERVICE_NAME}) deployment/update initiated."
echo "Worker B (${WORKER_B_SERVICE_NAME}) deployment/update initiated."
echo "Check GCP Console for deployment status and service URLs."
echo "----------------------------------------------------------------------"

echo "
[INFO] Remember to update RENDEZVOUS_SERVICE_URL in your .envrc or environment"
echo "       if the Rendezvous service URL changed from what is in .envrc."

echo "
[+] Redeployment script finished." 