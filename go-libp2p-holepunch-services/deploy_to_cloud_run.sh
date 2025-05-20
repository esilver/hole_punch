#!/usr/bin/env bash
# Quick-n-dirty helper that builds container images and deploys the three
# demo services to Cloud Run.  It assumes you already authenticated with
#   gcloud auth login && gcloud auth configure-docker
# and selected a project + region.
#
# Usage:
#   ./deploy_to_cloud_run.sh <gcp-region> <gcp-project>
#
# The script will:
#   1. Build & push container images for peerapi, rendezvous, and worker.
#   2. Deploy peerapi first and capture the resulting URL.
#   3. Deploy rendezvous with PEER_DISCOVERY_URL set to that URL.
#   4. Print the rendezvous Cloud Run URL and wait for you to copy a
#      multi-address from its logs.
#   5. Once you paste the multi-address, deploy a worker with the proper
#      env-vars.
#
# NOTE: Because the libp2p host generates a random peer-ID at runtime,
#       we cannot fully automate step 4; you need to copy the address
#       the service prints at start-up.
set -euo pipefail

REGION=${1:-}
PROJECT=${2:-}
if [[ -z "$REGION" || -z "$PROJECT" ]]; then
  echo "Usage: $0 <region> <gcp-project>" >&2
  exit 1
fi

IMAGE_ROOT="gcr.io/$PROJECT/go-libp2p-holepunch-demo"

build_push() {
  local dir=$1 name=$2
  tag="$IMAGE_ROOT/$name:latest"
  echo "Building $tag …"
  ( cd "$dir" && docker build -t "$tag" . )
  docker push "$tag"
}

# 1. images
build_push peerapi   peerapi
build_push rendezvous rendezvous
build_push worker    worker

echo "\nDeploying to Cloud Run in region $REGION …"

# 2. peerapi
PEERAPI_URL=$(gcloud run deploy peerapi \
  --image "$IMAGE_ROOT/peerapi:latest" \
  --region "$REGION" --platform managed --allow-unauthenticated \
  --format 'value(status.url)')

echo "peerapi deployed → $PEERAPI_URL"

# 3. rendezvous with env var
RENDEZVOUS_URL=$(gcloud run deploy rendezvous \
  --image "$IMAGE_ROOT/rendezvous:latest" \
  --region "$REGION" --platform managed --allow-unauthenticated \
  --set-env-vars "PEER_DISCOVERY_URL=$PEERAPI_URL" \
  --format 'value(status.url)')

echo "rendezvous deployed → $RENDEZVOUS_URL"

echo "\n============================================================"
echo "💡  FINAL STEP: we need the rendezvous libp2p multi-address."
cat <<EOF
1. In a new terminal run:

   gcloud logs tail rendezvous --region $REGION --project $PROJECT

   Wait until you see a line like:
     /dns4/..../tcp/443/wss/p2p/<peerID>

2. Copy that whole multi-address, then paste it here.
EOF
read -rp $'Paste rendezvous multi-address: ' RENDEZVOUS_MADDR

# 5. deploy worker(s)
gcloud run deploy worker \
  --image "$IMAGE_ROOT/worker:latest" \
  --region "$REGION" --platform managed --allow-unauthenticated \
  --set-env-vars "RENDEZVOUS_MULTIADDR=$RENDEZVOUS_MADDR,RENDEZVOUS_SERVICE_URL=$PEERAPI_URL"

echo "\nWorker deployed.  All services are up!" 