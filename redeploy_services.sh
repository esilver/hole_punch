#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
set -e

# Load environment variables
if [ -f .envrc ]; then
    echo "Loading environment variables from .envrc..."
    # Use set -a to export all variables defined in .envrc
    set -a
    source .envrc
    set +a
else
    echo "Error: .envrc file not found. Please create it with the necessary environment variables." >&2
    exit 1
fi

echo "--- Starting Redeployment Process (with Docker Buildx) ---"

# --- 1. Delete existing services concurrently ---
echo "Step 1: Deleting existing services concurrently..."
gcloud run services delete ${RENDEZVOUS_SERVICE_NAME} --platform=managed --region=${REGION} --project=${PROJECT_ID} --quiet & rend_delete_pid=$!
gcloud run services delete ${WORKER_SERVICE_NAME} --platform=managed --region=${REGION} --project=${PROJECT_ID} --quiet & worker_delete_pid=$!

# Wait for deletions to complete
wait $rend_delete_pid
echo "Rendezvous service (${RENDEZVOUS_SERVICE_NAME}) deletion process completed."
wait $worker_delete_pid
echo "Worker service (${WORKER_SERVICE_NAME}) deletion process completed."
echo "All services deleted."

sleep 5 # Short delay after deletions

# --- 2. Build and push images using Docker Buildx (sequentially for clarity, can be parallelized) ---
echo "Step 2: Building and pushing images with Docker Buildx..."

echo "Building and pushing Rendezvous service image..."
docker buildx build --platform linux/amd64 \
  -t ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:${RENDEZVOUS_IMAGE_TAG} \
  -f rendezvous_service_code/Dockerfile.rendezvous rendezvous_service_code --push
echo "Rendezvous service image build and push completed."

echo "Building and pushing Worker service image..."
docker buildx build --platform linux/amd64 \
  -t ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG} \
  -f Dockerfile.worker . --push
echo "Worker service image build and push completed."

# --- 3. Deploy Rendezvous Service ---
echo "Step 3: Deploying Rendezvous service (${RENDEZVOUS_SERVICE_NAME})..."
gcloud run deploy ${RENDEZVOUS_SERVICE_NAME} \
  --project=${PROJECT_ID} \
  --image ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:${RENDEZVOUS_IMAGE_TAG} \
  --platform=managed \
  --region ${REGION} \
  --allow-unauthenticated \
  --session-affinity
echo "Rendezvous service (${RENDEZVOUS_SERVICE_NAME}) deployed successfully."

# --- 4. Deploy Worker Service ---
# Update RENDEZVOUS_URL if it's dynamically generated or to ensure it's the latest one from the new deployment
# For simplicity, this script assumes RENDEZVOUS_URL from .envrc is still the target.
# If RENDEZVOUS_SERVICE_NAME always resolves to the correct URL, this is fine.
# Otherwise, you might need to fetch the new URL:
# export RENDEZVOUS_URL=$(gcloud run services describe ${RENDEZVOUS_SERVICE_NAME} --platform=managed --region=${REGION} --project=${PROJECT_ID} --format='value(status.url)')
# echo "Updated RENDEZVOUS_URL to: ${RENDEZVOUS_URL}"

echo "Step 4: Deploying Worker service (${WORKER_SERVICE_NAME})..."
gcloud run deploy ${WORKER_SERVICE_NAME} \
  --image ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG} \
  --project ${PROJECT_ID} \
  --region ${REGION} \
  --platform managed \
  --allow-unauthenticated \
  --set-env-vars="RENDEZVOUS_SERVICE_URL=${RENDEZVOUS_URL},STUN_HOST=${DEFAULT_STUN_HOST:-stun.l.google.com},STUN_PORT=${DEFAULT_STUN_PORT:-19302},INTERNAL_UDP_PORT=${INTERNAL_UDP_PORT:-8081},PING_INTERVAL_SEC=${PING_INTERVAL_SEC:-25},PING_TIMEOUT_SEC=${PING_TIMEOUT_SEC:-25},BENCHMARK_GCS_URL=${BENCHMARK_GCS_URL},BENCHMARK_CHUNK_SIZE=${BENCHMARK_CHUNK_SIZE}" \
  --vpc-egress=all-traffic \
  --network=ip-worker-vpc \
  --subnet=ip-worker-subnet \
  --min-instances=2 \
  --max-instances=2
echo "Worker service (${WORKER_SERVICE_NAME}) deployed successfully."

echo "--- Redeployment Process Completed ---" 