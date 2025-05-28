#!/bin/bash
set -e

# Configuration
PROJECT_ID="iceberg-eli"
SERVICE_NAME="trino-p2p-service"
REGION="us-central1"
IMAGE_NAME="trino-p2p-worker"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}Building and deploying Trino P2P workers...${NC}"

# Build the Trino Docker image for amd64/linux platform and push
echo -e "${YELLOW}Building Docker image for amd64/linux and pushing to GCR...${NC}"
docker buildx build --platform linux/amd64 -f Dockerfile.trino -t gcr.io/${PROJECT_ID}/${IMAGE_NAME}:latest --push .

# Deploy coordinator (first worker)
echo -e "${YELLOW}Deploying Trino coordinator...${NC}"
COORDINATOR_ID=$(uuidgen | tr '[:upper:]' '[:lower:]')
gcloud run deploy ${SERVICE_NAME}-coordinator \
    --image gcr.io/${PROJECT_ID}/${IMAGE_NAME}:latest \
    --platform managed \
    --region ${REGION} \
    --allow-unauthenticated \
    --memory 4Gi \
    --cpu 4 \
    --max-instances 1 \
    --set-env-vars "RENDEZVOUS_SERVICE_URL=https://rendezvous-service-982092720909.us-central1.run.app" \
    --set-env-vars "WORKER_ID=${COORDINATOR_ID}" \
    --set-env-vars "TRINO_COORDINATOR_ID=${COORDINATOR_ID}" \
    --set-env-vars "TRINO_MODE=true" \
    --set-env-vars "IS_COORDINATOR=true" \
    --set-env-vars "TRINO_LOCAL_PORT=8081" \
    --set-env-vars "TRINO_PROXY_PORT=18080" \
    --set-env-vars "INTERNAL_UDP_PORT=8081" \
    --set-env-vars "STUN_HOST=stun.l.google.com" \
    --set-env-vars "STUN_PORT=19302" \
    --set-env-vars "PING_INTERVAL_SEC=25" \
    --set-env-vars "PING_TIMEOUT_SEC=25" \
    --set-env-vars "STUN_RECHECK_INTERVAL_SEC=60" \
    --vpc-egress=all-traffic \
    --network=ip-worker-vpc \
    --subnet=ip-worker-subnet \
    --project ${PROJECT_ID}

# Deploy worker node
echo -e "${YELLOW}Deploying Trino worker...${NC}"
WORKER_ID=$(uuidgen | tr '[:upper:]' '[:lower:]')
gcloud run deploy ${SERVICE_NAME}-worker \
    --image gcr.io/${PROJECT_ID}/${IMAGE_NAME}:latest \
    --platform managed \
    --region ${REGION} \
    --allow-unauthenticated \
    --memory 4Gi \
    --cpu 4 \
    --max-instances 1 \
    --set-env-vars "RENDEZVOUS_SERVICE_URL=https://rendezvous-service-982092720909.us-central1.run.app" \
    --set-env-vars "WORKER_ID=${WORKER_ID}" \
    --set-env-vars "TRINO_COORDINATOR_ID=${COORDINATOR_ID}" \
    --set-env-vars "TRINO_MODE=true" \
    --set-env-vars "TRINO_LOCAL_PORT=8081" \
    --set-env-vars "TRINO_PROXY_PORT=18080" \
    --set-env-vars "INTERNAL_UDP_PORT=8081" \
    --set-env-vars "STUN_HOST=stun.l.google.com" \
    --set-env-vars "STUN_PORT=19302" \
    --set-env-vars "PING_INTERVAL_SEC=25" \
    --set-env-vars "PING_TIMEOUT_SEC=25" \
    --set-env-vars "STUN_RECHECK_INTERVAL_SEC=60" \
    --vpc-egress=all-traffic \
    --network=ip-worker-vpc \
    --subnet=ip-worker-subnet \
    --project ${PROJECT_ID}

echo -e "${GREEN}Deployment complete!${NC}"
echo -e "${GREEN}Coordinator ID: ${COORDINATOR_ID}${NC}"
echo -e "${GREEN}Worker ID: ${WORKER_ID}${NC}"

# Get service URLs
COORDINATOR_URL=$(gcloud run services describe ${SERVICE_NAME}-coordinator --region ${REGION} --format 'value(status.url)' --project ${PROJECT_ID})
WORKER_URL=$(gcloud run services describe ${SERVICE_NAME}-worker --region ${REGION} --format 'value(status.url)' --project ${PROJECT_ID})

echo -e "${GREEN}Service URLs:${NC}"
echo -e "Coordinator: ${COORDINATOR_URL}"
echo -e "Worker: ${WORKER_URL}"

echo -e "${YELLOW}Waiting for services to start...${NC}"
sleep 10

# Test the services
echo -e "${YELLOW}Testing Trino discovery...${NC}"
curl -s ${COORDINATOR_URL}/v1/info | jq .
curl -s ${COORDINATOR_URL}/v1/announcement | jq .