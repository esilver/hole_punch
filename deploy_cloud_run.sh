#!/usr/bin/env bash
# Build & deploy a go-libp2p service (rendezvous or worker) to Cloud Run using Cloud Build.
# Usage:
#   ./deploy_cloud_run.sh <service> <project_id> <region> [tag] [--deploy-only] [<additional gcloud args>]
# Example:
#   ./deploy_cloud_run.sh rendezvous my-gcp-project us-central1 v1
set -euo pipefail

# -----------------------------------------------------------------------------
# Usage
#   ./deploy_cloud_run.sh <service> <project_id> <region> [tag] [--deploy-only] [<additional gcloud args>]
#
# Example (build & deploy):
#   ./deploy_cloud_run.sh rendezvous my-gcp-project us-central1 v1
#
# Example (deploy-only, skip build):
#   ./deploy_cloud_run.sh worker my-gcp-project us-central1 v1 --deploy-only --set-env-vars "RENDEZVOUS_SERVICE_URL=https://..."
# -----------------------------------------------------------------------------

if [[ $# -lt 3 ]]; then
  echo "usage: $0 <rendezvous|worker> <PROJECT_ID> <REGION> [TAG] [--deploy-only] [<extra gcloud run deploy args>]" >&2
  exit 1
fi

# Required positional args
SERVICE=$1; shift
PROJECT=$1; shift
REGION=$1; shift

# Optional tag (defaults to latest) – only treat next arg as TAG if it does NOT start with '--'
if [[ $# -gt 0 && $1 != --* ]]; then
  TAG=$1
  shift
else
  TAG="latest"
fi

# Flags / extra args processing
DEPLOY_ONLY=false
EXTRA_ARGS=()
while (( "$#" )); do
  case "$1" in
    --deploy-only)
      DEPLOY_ONLY=true
      shift
      ;;
    *)
      EXTRA_ARGS+=("$1")
      shift
      ;;
  esac
done

ROOT_DIR="$(cd "$(dirname "$0")"; pwd)"
SERVICE_DIR="$ROOT_DIR/go-libp2p-holepunch-services/${SERVICE}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${SERVICE}-repo/${SERVICE}:${TAG}"

if [[ ! -d "$SERVICE_DIR" ]]; then
  echo "Unknown service dir $SERVICE_DIR" >&2; exit 1
fi

# -----------------------------------------------------------------------------
# Build & push image (unless --deploy-only was supplied)
# -----------------------------------------------------------------------------
if [[ "$DEPLOY_ONLY" == false ]]; then
  echo "Building & pushing image $IMAGE with docker buildx ..."

  # ensure repo exists
  gcloud artifacts repositories describe "${SERVICE}-repo" \
       --project "$PROJECT" --location "$REGION" >/dev/null 2>&1 || \
    gcloud artifacts repositories create "${SERVICE}-repo" \
       --repository-format=docker --location "$REGION" --description="repo for $SERVICE"

  # configure docker to push to Artifact Registry
  gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

  # build & push (linux/amd64)
  docker buildx build --no-cache --pull --platform linux/amd64 -t "$IMAGE" --push "$SERVICE_DIR"
else
  echo "Skipping image build & push (deploy-only). Using existing image $IMAGE"
fi

# -----------------------------------------------------------------------------
# Deploy to Cloud Run
# -----------------------------------------------------------------------------

echo "Deploying to Cloud Run ($SERVICE)..."
gcloud run deploy "$SERVICE" \
  --project="$PROJECT" \
  --region="$REGION" \
  --image="$IMAGE" \
  --platform=managed \
  --allow-unauthenticated \
  --session-affinity \
  --max-instances=2 \
  "${EXTRA_ARGS[@]}"

echo "Done. Get URL with: gcloud run services describe $SERVICE --platform managed --region $REGION --format='value(status.url)'" 