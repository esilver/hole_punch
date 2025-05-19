# P2P UDP/QUIC Communication with Rendezvous Service

This repository contains a proof-of-concept system for establishing peer-to-peer (P2P) UDP communication between services, potentially running in environments like Google Cloud Run behind NATs. It utilizes a central **Rendezvous Service** for peer discovery and signaling, and **Worker Services** that establish direct UDP connections and can optionally upgrade to QUIC for reliable, secure data exchange.

## Features

The system demonstrates:
- NAT traversal using STUN
- UDP hole punching
- WebSocket-based signaling for P2P negotiation
- Direct P2P UDP data exchange
- (Optional) QUIC tunneling over P2P UDP
- A web-based UI on each worker for interactivity and monitoring

## Project Components (Agents)

This system is primarily composed of two types of services:

### 1. Rendezvous Service (`rendezvous_service_code/`)
- Acts as a central discovery server
- Workers connect to it via WebSockets to register their presence and discovered public UDP endpoints
- Pairs available workers and instructs them to attempt direct P2P connections
- Built with Python, FastAPI, and Uvicorn

### 2. Worker Service (Root Directory - `main.py`, `Dockerfile`, etc.)
- Represents an individual peer in the P2P network
- Performs STUN discovery to find its public UDP IP/port
- Connects to the Rendezvous Service to register and get peer information
- Initiates UDP hole punching to establish direct connections with peers
- Supports basic P2P UDP chat and data benchmarks
- Can establish a QUIC tunnel over the UDP P2P link for secure, reliable stream-based communication
- Serves a local web UI (`index.html`) for interaction and monitoring, using a local WebSocket server for browser-backend communication
- Built with Python, `asyncio`, `websockets`, `aioquic`, `pystun3`, and other libraries

For a more detailed breakdown of the agent roles and interactions, please see `agents.md`.

## Directory Structure

```
.
├── rendezvous_service_code/     # Code for the Rendezvous Service
│   ├── Dockerfile.rendezvous    # Dockerfile specific to Rendezvous service
│   ├── main.py                  # FastAPI application for Rendezvous
│   └── requirements.txt         # Python dependencies for Rendezvous
├── tests/                       # Unit tests
│   ├── test_network_utils.py
│   ├── test_quic_tunnel.py
│   └── test_ui_server.py
├── .envrc                       # Environment variables (local, use with direnv)
├── .gitignore
├── cert.pem                     # Self-signed certificate for QUIC (server can generate if missing)
├── key.pem                      # Private key for QUIC (server can generate if missing)
├── Dockerfile                   # Main Dockerfile for the Worker Service (updated from Dockerfile.worker)
├── Dockerfile.worker            # Worker service Dockerfile
├── index.html                   # Web UI for the Worker Service
├── main.py                      # Main application for the Worker Service
├── network_utils.py             # STUN, packet utilities
├── p2p_protocol.py              # P2P UDP protocol handler
├── quic_tunnel.py               # QUIC tunnel implementation
├── README.md                    # This file
├── rebuild_and_redeploy_services.sh # Script to rebuild and deploy both services
├── redeploy_services.sh         # Script to build and deploy services (older version)
├── rendezvous_client.py         # Client logic for Worker to connect to Rendezvous
├── requirements.txt             # Python dependencies for the Worker Service
└── ui_server.py                 # WebSocket and HTTP server for the Worker's UI
```

## Prerequisites

1. **Google Cloud SDK (`gcloud`):** Installed and authenticated. Project and region configured.
2. **Docker:** For building container images. Docker Buildx is recommended for multi-platform builds (especially for `linux/amd64` if building on non-amd64 hardware).
3. **Python 3.9+:** For local development and understanding the code.
4. **Enabled Google Cloud APIs:**
   - Cloud Build API (`cloudbuild.googleapis.com`)
   - Cloud Run API (`run.googleapis.com`)
   - Compute Engine API (`compute.googleapis.com`)
   - Artifact Registry API (`artifactregistry.googleapis.com`)
   - IAM API (`iam.googleapis.com`)
5. **Google Cloud Project & Networking:**
   - A GCP project
   - A VPC network, subnet, Cloud Router, and Cloud NAT gateway (configured for full cone NAT / EIM if possible) to allow Cloud Run services to have a stable outbound public IP for UDP communication. This is typically set up for the Worker services. (Refer to `plan.md` for an example setup of these resources).

## Environment Variables

Set the following environment variables before running the build commands. These are typically set in the Cloud Run service deployment or can be sourced locally from a `.envrc` file (e.g., when using `direnv`).

### Example Configuration

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

### Complete Variable Reference

- `PROJECT_ID`: Your Google Cloud Project ID
- `REGION`: The GCP region for deployments (e.g., `us-central1`)
- `RENDEZVOUS_SERVICE_URL`: The public URL of the deployed Rendezvous Service (used by Workers)
- `AR_RENDEZVOUS_REPO_NAME`: Artifact Registry repository name for the Rendezvous service
- `RENDEZVOUS_SERVICE_NAME`: Cloud Run service name for Rendezvous
- `RENDEZVOUS_IMAGE_TAG`: Image tag for the Rendezvous service
- `AR_WORKER_REPO_NAME`: Artifact Registry repository name for the Worker service
- `WORKER_SERVICE_NAME`: Cloud Run service name for the Worker
- `WORKER_IMAGE_TAG`: Image tag for the Worker service
- `INTERNAL_UDP_PORT`: Internal UDP port the Worker listens on (e.g., `8081`)
- `STUN_HOST`: STUN server hostname
- `STUN_PORT`: STUN server port
- `BENCHMARK_GCS_URL`: URL of a file in GCS for UDP benchmark tests
- `BENCHMARK_CHUNK_SIZE`: Chunk size for benchmark data transfer
- `STUN_RECHECK_INTERVAL_SEC`: How often workers re-check their STUN mapping

## Build Instructions

This repository contains two services that can be containerized and deployed to Google Cloud Run. Below are example commands for building each image using either Docker Buildx or Google Cloud Build.

### Build Rendezvous Service

#### Using Docker Buildx:

```bash
docker buildx build --platform linux/amd64 \
  -t ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:${RENDEZVOUS_IMAGE_TAG} \
  -f rendezvous_service_code/Dockerfile.rendezvous rendezvous_service_code --push
```

#### Using Google Cloud Build:

```bash
gcloud builds submit rendezvous_service_code --region=$REGION \
  --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:${RENDEZVOUS_IMAGE_TAG} \
  --dockerfile=rendezvous_service_code/Dockerfile.rendezvous
```

### Build Worker Service

#### Using Docker Buildx:

```bash
docker buildx build --platform linux/amd64 \
  -t ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG} \
  -f Dockerfile.worker . --push
```

#### Using Google Cloud Build:

```bash
gcloud builds submit --region=$REGION \
  --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG} \
  --dockerfile=Dockerfile.worker
```

## Deployment Instructions

### 1. Deploy Rendezvous Service

```bash
gcloud run deploy ${RENDEZVOUS_SERVICE_NAME} \
  --project=${PROJECT_ID} \
  --image ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:${RENDEZVOUS_IMAGE_TAG} \
  --platform=managed \
  --region=${REGION} \
  --allow-unauthenticated \
  --session-affinity
```

> **Note:** Note the URL of the deployed Rendezvous Service. You will need it for `RENDEZVOUS_SERVICE_URL` when deploying the Worker Service.

### 2. Deploy Worker Service

```bash
gcloud run deploy ${WORKER_SERVICE_NAME} \
  --image ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG} \
  --project=${PROJECT_ID} \
  --region=${REGION} \
  --platform=managed \
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

### Delete Worker Service:

```bash
gcloud run services delete ${WORKER_SERVICE_NAME} --platform=managed --region=${REGION} --project=${PROJECT_ID}
```

### Delete Rendezvous Service:

```bash
gcloud run services delete ${RENDEZVOUS_SERVICE_NAME} --platform=managed --region=${REGION} --project=${PROJECT_ID}
```

## Automated Redeployment

The `rebuild_and_redeploy_services.sh` script provides a way to automate the deletion, rebuilding, and redeployment of both services. Ensure your environment variables are correctly set before running it.

## Usage & Verification

1. Deploy both the Rendezvous and Worker services (Rendezvous first)
2. Open the public URL of two different Worker instances in your browser
3. The Web UI on each worker should load
4. Check the Rendezvous service logs (`/debug/list_workers` endpoint) to see if workers are registered and have STUN UDP endpoints
5. Check worker logs for successful STUN discovery, connection to Rendezvous, and P2P pairing offers
6. The Worker UI should indicate P2P connection status
7. Test sending chat messages via UDP and QUIC between the worker UIs
8. Use the benchmark tool in the UI to test UDP throughput

## Known Files & Plans

- `plan.md`: Initial PoC plan for Cloud Run with Cloud NAT
- `plann_step2.md`: Plan for Rendezvous service and worker registration
- `plann_step3a.md`: Plan for Worker UDP endpoint discovery and STUN
- `plann_step3b`: Plan for Peer-to-Peer UDP connection attempt (hole punching)
- `plann_step4a`: Plan for Worker Web UI, local WebSocket, and P2P data sending

*(Further plans related to QUIC specifics and advanced features may exist or be developed)*

## Debugging QUIC Issues

If you encounter QUIC connection problems ("Idle timeout" being a common symptom):

### Check Worker Logs
The most crucial information is in the individual worker logs (`main.py`, `quic_tunnel.py`, `p2p_protocol.py`). Look for:
- QUIC handshake events (`HandshakeCompleted`)
- QUIC PINGs being sent and `PingAcknowledged` events being received
- Any errors during `feed_datagram` or in the `_timer_management_loop`
- Reliability of `udp_sender_for_quic` (are packets actually being sent out over UDP?)

### Correlate Logs
Match logs from both peer workers involved in a failed session.

### NAT Behavior
Ensure Cloud NAT is configured correctly (EIM is helpful). Persistent UDP packet loss after initial hole punching often points to NAT issues or overly aggressive UDP timeout on the NAT.

### Certificate Issues
For the server-side QUIC worker, ensure `cert.pem` and `key.pem` are valid and accessible, or that self-signed generation is succeeding.