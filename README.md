# Build Instructions

This repository contains two services that can be containerized and deployed to Google Cloud Run. Below are example commands for building each image using either Docker Buildx or Google Cloud Build (`gcloud builds submit`).

## Environment Variables
Set the following variables before running the build commands:

```bash
# Common
export PROJECT_ID="iceberg-eli"      # GCP project
export REGION="us-central1"         # Artifact Registry region
export RENDEZVOUS_URL="https://rendezvous-service-982092720909.us-central1.run.app" # URL of the deployed Rendezvous service
export BENCHMARK_GCS_URL="https://storage.googleapis.com/holepunching/yellow_tripdata_2025-02.parquet" # GCS file URL for the worker benchmark

# Rendezvous service
export AR_RENDEZVOUS_REPO_NAME="rendezvous-repo"
export RENDEZVOUS_SERVICE_NAME="rendezvous-service"
export RENDEZVOUS_IMAGE_TAG="v_ip_update"

# Worker service
export AR_WORKER_REPO_NAME="ip-worker-repo"
export WORKER_SERVICE_NAME="ip-worker-service"
export WORKER_IMAGE_TAG="v_ip_update"
export BENCHMARK_CHUNK_SIZE="8192" # Default chunk size for benchmark
```

## Build Rendezvous Service

Using **Docker Buildx**:

```bash
docker buildx build --platform linux/amd64 \
  -t ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:${RENDEZVOUS_IMAGE_TAG} \
  -f rendezvous_service_code/Dockerfile.rendezvous rendezvous_service_code --push
```

Using **gcloud** (Cloud Build):

```bash
gcloud builds submit rendezvous_service_code --region=$REGION \
  --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:${RENDEZVOUS_IMAGE_TAG} \
  --dockerfile=rendezvous_service_code/Dockerfile.rendezvous
```

## Build Worker Service

Using **Docker Buildx**:

```bash
docker buildx build --platform linux/amd64 \
  -t ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG} \
  -f Dockerfile.worker . --push
```

Using **gcloud** (Cloud Build):

```bash
gcloud builds submit --region=$REGION \
  --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG} \
  --dockerfile=Dockerfile.worker
```

## Deploy Rendezvous Service

```bash
gcloud run deploy ${RENDEZVOUS_SERVICE_NAME} \
  --project=${PROJECT_ID} \
  --image ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:v_ip_update \
  --platform=managed \
  --region ${REGION} \
  --allow-unauthenticated \
  --session-affinity
```

## Deploy Worker Service

```bash
gcloud run deploy ${WORKER_SERVICE_NAME} \
  --image ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG} \
  --project ${PROJECT_ID} \
  --region ${REGION} \
  --platform managed \
  --allow-unauthenticated \
  --set-env-vars="RENDEZVOUS_SERVICE_URL=${RENDEZVOUS_URL},STUN_HOST=stun.l.google.com,STUN_PORT=19302,INTERNAL_UDP_PORT=8081,PING_INTERVAL_SEC=25,PING_TIMEOUT_SEC=25,BENCHMARK_GCS_URL=${BENCHMARK_GCS_URL},BENCHMARK_CHUNK_SIZE=${BENCHMARK_CHUNK_SIZE}" \
  --vpc-egress=all-traffic \
  --network=ip-worker-vpc \
  --subnet=ip-worker-subnet \
  --min-instances=2 \
  --max-instances=2
```

## Delete Services

To delete the deployed services, use the following commands:

**Delete Worker Service:**
```bash
gcloud run services delete ${WORKER_SERVICE_NAME} --platform=managed --region=${REGION} --project=${PROJECT_ID}
```

**Delete Rendezvous Service:**
```bash
gcloud run services delete ${RENDEZVOUS_SERVICE_NAME} --platform=managed --region=${REGION} --project=${PROJECT_ID}
```
