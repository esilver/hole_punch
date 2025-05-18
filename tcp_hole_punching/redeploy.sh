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
    source .envrc # Initial load
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
# EXTERNAL_ECHO_IMAGE_NAME: Docker image name for the external echo service
# EXTERNAL_ECHO_SERVICE_NAME: Cloud Run service name for the external echo service
# EXTERNAL_ECHO_CONTAINER_PORT: Port the external echo service container listens on
# EXTERNAL_ECHO_REGION: Google Cloud region for the external echo service (NEW - must be different from $REGION)
# ECHO_VPC_CONNECTOR_NAME: Name of the VPC Access Connector for the echo service (OPTIONAL, if used)

# --- Check for essential variables ---
if [ -z "$PROJECT_ID" ] || [ -z "$REGION" ] || [ -z "$AR_REPO" ] || \
   [ -z "$RENDEZVOUS_IMAGE_NAME" ] || [ -z "$WORKER_IMAGE_NAME" ] || \
   [ -z "$RENDEZVOUS_CONTAINER_PORT" ] || [ -z "$DOCKER_BUILD_TIMEOUT_SEC" ] || \
   [ -z "$GCLOUD_DEPLOY_TIMEOUT_SEC" ] || [ -z "$RENDEZVOUS_IMAGE_TAG" ] || \
   [ -z "$WORKER_IMAGE_TAG" ] || [ -z "$WORKER_A_SERVICE_NAME" ] || \
   [ -z "$WORKER_B_SERVICE_NAME" ] || [ -z "$WORKER_A_INTERNAL_TCP_PORT" ] || \
   [ -z "$WORKER_B_INTERNAL_TCP_PORT" ] || [ -z "$WORKER_A_CLOUDRUN_HEALTH_PORT" ] || \
   [ -z "$WORKER_B_CLOUDRUN_HEALTH_PORT" ] || [ -z "$RENDEZVOUS_SERVICE_URL" ] || \
   [ -z "$EXTERNAL_ECHO_IMAGE_NAME" ] || [ -z "$EXTERNAL_ECHO_SERVICE_NAME" ] || [ -z "$EXTERNAL_ECHO_CONTAINER_PORT" ] || [ -z "$EXTERNAL_ECHO_REGION" ] || [ -z "$EXTERNAL_ECHO_SERVER_PORT" ]; then
    echo "Error: One or more required environment variables are not set."
    echo "Please ensure all of the following are defined in .envrc:"
    echo "PROJECT_ID, REGION, AR_REPO, RENDEZVOUS_IMAGE_NAME, WORKER_IMAGE_NAME, RENDEZVOUS_CONTAINER_PORT, DOCKER_BUILD_TIMEOUT_SEC, GCLOUD_DEPLOY_TIMEOUT_SEC, RENDEZVOUS_IMAGE_TAG, WORKER_IMAGE_TAG, WORKER_A_SERVICE_NAME, WORKER_B_SERVICE_NAME, WORKER_A_INTERNAL_TCP_PORT, WORKER_B_INTERNAL_TCP_PORT, WORKER_A_CLOUDRUN_HEALTH_PORT, WORKER_B_CLOUDRUN_HEALTH_PORT, RENDEZVOUS_SERVICE_URL, EXTERNAL_ECHO_IMAGE_NAME, EXTERNAL_ECHO_SERVICE_NAME, EXTERNAL_ECHO_CONTAINER_PORT, EXTERNAL_ECHO_REGION, EXTERNAL_ECHO_SERVER_PORT"
    echo "Consider creating and configuring a .envrc file and running 'direnv allow'."
    exit 1
fi

if [ "$REGION" == "$EXTERNAL_ECHO_REGION" ]; then
    echo "Error: EXTERNAL_ECHO_REGION must be different from REGION for effective NAT traversal discovery." >&2
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

CLOUDRUN_RENDEZVOUS_SERVICE_NAME="${RENDEZVOUS_IMAGE_NAME}"
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

echo "Deleting External Echo Service: ${CLOUDRUN_EXTERNAL_ECHO_SERVICE_NAME} in region ${EXTERNAL_ECHO_REGION}..."
gcloud run services delete "${CLOUDRUN_EXTERNAL_ECHO_SERVICE_NAME}" \
    --platform managed \
    --region "${EXTERNAL_ECHO_REGION}" \
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

# --- Provision External Echo Service on a Compute Engine VM ---
echo "\n[+] Creating / Updating External Echo VM to run the echo container..."

# Required env vars for the VM approach
if [ -z "$EXTERNAL_ECHO_VM_NAME" ] || [ -z "$EXTERNAL_ECHO_VM_ZONE" ] || [ -z "$EXTERNAL_ECHO_VM_EXTERNAL_PORT" ]; then
  echo "Error: For VM-based echo service you must set EXTERNAL_ECHO_VM_NAME, EXTERNAL_ECHO_VM_ZONE, and EXTERNAL_ECHO_VM_EXTERNAL_PORT in .envrc" >&2
  exit 1
fi

# Delete any existing instance with the same name (quietly)
gcloud compute instances delete "$EXTERNAL_ECHO_VM_NAME" \
    --zone "$EXTERNAL_ECHO_VM_ZONE" --quiet --project "$PROJECT_ID" || true

# Ensure firewall rule exists to allow inbound traffic on the external port
FIREWALL_RULE_NAME="allow-echo-${EXTERNAL_ECHO_VM_EXTERNAL_PORT}"
if ! gcloud compute firewall-rules describe "$FIREWALL_RULE_NAME" --project "$PROJECT_ID" >/dev/null 2>&1; then
  echo "Creating firewall rule $FIREWALL_RULE_NAME for tcp:${EXTERNAL_ECHO_VM_EXTERNAL_PORT}..."
  gcloud compute firewall-rules create "$FIREWALL_RULE_NAME" \
      --allow "tcp:${EXTERNAL_ECHO_VM_EXTERNAL_PORT}" \
      --direction INGRESS \
      --target-tags "echo-vm" \
      --project "$PROJECT_ID" --quiet
fi

# Define the startup script that will run on the VM
STARTUP_SCRIPT=$(cat <<'EOS'
#!/bin/bash
set -euxo pipefail

# Injected from metadata substitution below so that we don't rely on direnv variables inside the VM.
REGION="__REGION__"
EXTERNAL_ECHO_REMOTE_IMAGE_URL="__ECHO_IMAGE__"
EXTERNAL_ECHO_VM_EXTERNAL_PORT="__EXTERNAL_PORT__"
EXTERNAL_ECHO_CONTAINER_PORT="__CONTAINER_PORT__"

# Docker is pre-installed on Container-Optimized OS.

# Wait up to 60 s for the metadata server to answer
for i in $(seq 1 12); do
  TOKEN=$(curl -s -H "Metadata-Flavor: Google" \
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" | \
    sed -n 's/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"\n]*\)".*/\1/p')

  if [ -n "$TOKEN" ]; then
    echo "Startup-script: Successfully obtained Artifact Registry token."
    break
  fi
  echo "Startup-script: Metadata server not ready or token not found yet – sleeping 5s (attempt $i/12)"
  sleep 5
done

if [ -z "$TOKEN" ]; then
  echo "Startup-script: Error: could not obtain Artifact Registry token after 60 s" >&2
  # Additional debug: Try curl without awk to see what we get
  echo "Startup-script: Debug: curl output without awk:"
  curl -s -H "Metadata-Flavor: Google" \
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
  exit 1 # This exit is within the VM's startup script
fi

echo "Startup-script: Logging in to Artifact Registry..."
# Use writable directory for Docker credentials since /root is read-only on COS
export DOCKER_CONFIG=/var/lib/docker-config
mkdir -p "$DOCKER_CONFIG"
echo "$TOKEN" | docker login -u oauth2accesstoken --password-stdin https://${REGION}-docker.pkg.dev

echo "Startup-script: Pulling and running the echo container..."
docker run -d --restart unless-stopped -p ${EXTERNAL_ECHO_VM_EXTERNAL_PORT}:${EXTERNAL_ECHO_CONTAINER_PORT} ${EXTERNAL_ECHO_REMOTE_IMAGE_URL}
EOS
)

# Replace placeholders with real values (safe because no other occurrences expected)
STARTUP_SCRIPT=${STARTUP_SCRIPT//__REGION__/${REGION}}
STARTUP_SCRIPT=${STARTUP_SCRIPT//__ECHO_IMAGE__/${EXTERNAL_ECHO_REMOTE_IMAGE_URL}}
STARTUP_SCRIPT=${STARTUP_SCRIPT//__EXTERNAL_PORT__/${EXTERNAL_ECHO_VM_EXTERNAL_PORT}}
STARTUP_SCRIPT=${STARTUP_SCRIPT//__CONTAINER_PORT__/${EXTERNAL_ECHO_CONTAINER_PORT}}

# Create the VM instance with the startup script
gcloud compute instances create "$EXTERNAL_ECHO_VM_NAME" \
    --zone "$EXTERNAL_ECHO_VM_ZONE" \
    --machine-type "e2-micro" \
    --image-family "cos-stable" \
    --image-project "cos-cloud" \
    --tags "echo-vm" \
    --scopes "https://www.googleapis.com/auth/cloud-platform" \
    --metadata=startup-script="${STARTUP_SCRIPT}" \
    --project "$PROJECT_ID" --quiet

# Wait up to 60 s for the metadata server to answer
# The above token logic and docker run are now INSIDE the STARTUP_SCRIPT for the VM.
# The main redeploy.sh script will now wait for the VM to be ready using the /dev/tcp check.

# Fetch external IP
export EXTERNAL_ECHO_SERVER_HOST=$(gcloud compute instances describe "$EXTERNAL_ECHO_VM_NAME" --zone "$EXTERNAL_ECHO_VM_ZONE" --project "$PROJECT_ID" --format='value(networkInterfaces[0].accessConfigs[0].natIP)')

echo "[INFO] Updated EXTERNAL_ECHO_SERVER_HOST to: $EXTERNAL_ECHO_SERVER_HOST"

if [ -z "$EXTERNAL_ECHO_SERVER_HOST" ]; then
  echo "Error: Could not retrieve external IP for VM $EXTERNAL_ECHO_VM_NAME" >&2
  exit 1
fi

echo "External Echo VM IP: $EXTERNAL_ECHO_SERVER_HOST"

# ---- Wait for External Echo Service to become ready ----
echo "[+] Waiting for external echo service to accept TCP connections on ${EXTERNAL_ECHO_VM_EXTERNAL_PORT}..."
MAX_WAIT_SEC=180
ELAPSED=0
SLEEP_INTERVAL=5
while ! (exec 3<>/dev/tcp/${EXTERNAL_ECHO_SERVER_HOST}/${EXTERNAL_ECHO_VM_EXTERNAL_PORT}) 2>/dev/null; do
  if [ $ELAPSED -ge $MAX_WAIT_SEC ]; then
    echo "Error: External echo service did not become ready within ${MAX_WAIT_SEC}s." >&2
    exit 1
  fi
  echo "[INFO] Echo service not ready yet. Sleeping ${SLEEP_INTERVAL}s..."
  sleep $SLEEP_INTERVAL
  ELAPSED=$((ELAPSED + SLEEP_INTERVAL))
done
echo "[INFO] External echo service is ready. Proceeding with worker deployment."

# --- Deploy Worker A to Google Cloud Run (in $REGION) ---
echo "
[+] Deploying Worker A (${WORKER_A_SERVICE_NAME}) to Google Cloud Run in region ${REGION}..."
run_with_timeout "${GCLOUD_DEPLOY_TIMEOUT_SEC}s" gcloud run deploy "${WORKER_A_SERVICE_NAME}" \
    --image "${WORKER_REMOTE_IMAGE_URL}" \
    --platform managed \
    --region "${REGION}" \
    --port "${WORKER_A_CLOUDRUN_HEALTH_PORT}" \
    --set-env-vars "RENDEZVOUS_SERVICE_URL=${RENDEZVOUS_SERVICE_URL},INTERNAL_TCP_PORT=${WORKER_A_INTERNAL_TCP_PORT},EXTERNAL_ECHO_SERVER_HOST=${EXTERNAL_ECHO_SERVER_HOST},EXTERNAL_ECHO_SERVER_PORT=${EXTERNAL_ECHO_VM_EXTERNAL_PORT}" \
    --allow-unauthenticated \
    --project "${PROJECT_ID}" \
    --quiet

# --- Deploy Worker B to Google Cloud Run (in $REGION) ---
echo "
[+] Deploying Worker B (${WORKER_B_SERVICE_NAME}) to Google Cloud Run in region ${REGION}..."
run_with_timeout "${GCLOUD_DEPLOY_TIMEOUT_SEC}s" gcloud run deploy "${WORKER_B_SERVICE_NAME}" \
    --image "${WORKER_REMOTE_IMAGE_URL}" \
    --platform managed \
    --region "${REGION}" \
    --port "${WORKER_B_CLOUDRUN_HEALTH_PORT}" \
    --set-env-vars "RENDEZVOUS_SERVICE_URL=${RENDEZVOUS_SERVICE_URL},INTERNAL_TCP_PORT=${WORKER_B_INTERNAL_TCP_PORT},EXTERNAL_ECHO_SERVER_HOST=${EXTERNAL_ECHO_SERVER_HOST},EXTERNAL_ECHO_SERVER_PORT=${EXTERNAL_ECHO_VM_EXTERNAL_PORT}" \
    --allow-unauthenticated \
    --project "${PROJECT_ID}" \
    --quiet

# (No need to fetch DEPLOYED_URL for rendezvous again as the user is instructed to update .envrc)
# The worker URLs are not critical for this script's output.

echo "
----------------------------------------------------------------------"
echo "Rendezvous Service (${CLOUDRUN_RENDEZVOUS_SERVICE_NAME}) deployed in ${REGION}."
echo "External Echo Service (${CLOUDRUN_EXTERNAL_ECHO_SERVICE_NAME}) deployed in ${EXTERNAL_ECHO_REGION}. URL: ${EXTERNAL_ECHO_SERVER_HOST}:${EXTERNAL_ECHO_CONTAINER_PORT}"
echo "Worker A (${WORKER_A_SERVICE_NAME}) deployed in ${REGION}."
echo "Worker B (${WORKER_B_SERVICE_NAME}) deployed in ${REGION}."
echo "Check GCP Console for deployment status."
echo "----------------------------------------------------------------------"

echo "
[INFO] Remember to update RENDEZVOUS_SERVICE_URL in your .envrc or environment"
echo "       if the Rendezvous service URL changed from what is in .envrc."

echo "
[+] Redeployment script finished." 