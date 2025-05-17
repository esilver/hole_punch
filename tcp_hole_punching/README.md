# TCP Hole Punching (2025-Ready Experiment)

This project demonstrates TCP hole punching between two peers, orchestrated by a rendezvous service. It's designed with a "2025-ready" approach, emphasizing structured logging, type hinting, and current Python best practices.

## Directory Structure

```
.tcp_hole_punching/
├── holepunch/                    # Worker client code
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
├── rendezvous_service_code/      # Rendezvous server code
│   ├── Dockerfile
│   ├── main.py
│   └── requirements.txt
└── README.md
```

## Services

### 1. Rendezvous Service (`rendezvous_service_code`)

A FastAPI application that coordinates two worker clients to find each other.

#### Environment Variables:

*   `PORT`: (Optional) The port on which the FastAPI application will run. Defaults to `8080`.

#### Build and Run (Docker):

```bash
# Navigate to the rendezvous service directory
cd tcp_hole_punching/rendezvous_service_code

# Build the Docker image
docker build -t rendezvous-service .

# Run the Docker container
# (Replace 8080 with your desired host port if PORT env var is different)
docker run -p 8080:8080 --rm --name rendezvous rendezvous-service
```

#### Local Development (without Docker):

```bash
cd tcp_hole_punching/rendezvous_service_code
python -m venv venv
source venv/bin/activate  # On Windows use `venv\Scripts\activate`
pip install -r requirements.txt

# Run the service (PORT can be set as an env var)
# PORT=8081 python main.py
python main.py
```

### 2. Worker Client (`holepunch`)

A Python application that connects to the rendezvous service, reports its public IP and a designated internal TCP port, and then attempts to establish a P2P TCP connection with a peer.

#### Environment Variables:

*   `INTERNAL_TCP_PORT`: (Optional) The internal TCP port the worker will listen on and attempt to use for NAT traversal. Defaults to `8082`.
*   `PORT`: (Optional) The port for the worker's own health check HTTP server (useful for Cloud Run). Defaults to `8080`.
*   `RENDEZVOUS_SERVICE_URL`: **(Required)** The full URL of the rendezvous service (e.g., `http://localhost:8080` or `http://your-rendezvous-service-domain.com`).
*   `WS_OPEN_TIMEOUT`: (Optional) Timeout in seconds for establishing the WebSocket connection to the rendezvous server. Defaults to `15.0`.
*   `WS_RECV_TIMEOUT`: (Optional) Timeout in seconds for receiving messages from the rendezvous server. Defaults to `120.0`.
*   `LISTENER_READY_TIMEOUT`: (Optional) Timeout in seconds to wait for the local TCP listener to be ready. Defaults to `15.0`.
*   `P2P_MSG_TIMEOUT`: (Optional) Timeout in seconds for P2P message exchange after connection. Defaults to `10.0`.

#### Build and Run (Docker):

**Note:** For Docker, `RENDEZVOUS_SERVICE_URL` typically needs to be accessible from within the Docker network. 
If running the rendezvous service locally in Docker:
*   On Docker Desktop (Mac/Windows), you might use `http://host.docker.internal:PORT` (replace `PORT` with the host port mapped to the rendezvous container, e.g., `http://host.docker.internal:8080`).
*   If both the rendezvous and worker containers are on the same custom Docker bridge network, you can often use the rendezvous container's name as the hostname (e.g., `http://rendezvous-service:8080` if the rendezvous container is named `rendezvous-service` and listens on port `8080` internally).
*   Alternatively, you can use the host's IP address if the rendezvous container's port is mapped to the host.

```bash
# Navigate to the worker client directory
cd tcp_hole_punching/holepunch

# Build the Docker image
docker build -t holepunch-worker .

# Run the Docker container (TWO instances are needed for a P2P test)
# Replace YOUR_RENDEZVOUS_URL and ensure different host ports if running on the same machine

# Terminal 1 (Worker A):
docker run -e RENDEZVOUS_SERVICE_URL="YOUR_RENDEZVOUS_URL" \
           -e INTERNAL_TCP_PORT=8082 \
           -e PORT=8080 \
           --rm --name worker-a holepunch-worker

# Terminal 2 (Worker B):
docker run -e RENDEZVOUS_SERVICE_URL="YOUR_RENDEZVOUS_URL" \
           -e INTERNAL_TCP_PORT=8083 \
           -e PORT=8081 \
           --rm --name worker-b holepunch-worker
```

**Important for Docker P2P:**
Achieving successful TCP hole punching with Docker can be complex due to Docker's own networking layers. For direct P2P between containers on the same host, they usually need to be on the same custom Docker network, and you might still face challenges. For P2P across different hosts, NAT traversal for the Docker host machines themselves becomes the primary concern, with Docker port mappings adding another layer.

Using `--network="host"` might simplify local P2P tests but has security implications and platform differences.

#### Local Development (without Docker):

```bash
cd tcp_hole_punching/holepunch
python -m venv venv
source venv/bin/activate # On Windows use `venv\Scripts\activate`
pip install -r requirements.txt

# Run the worker (TWO instances are needed for a P2P test)
# Ensure RENDEZVOUS_SERVICE_URL is set correctly.
# Use different INTERNAL_TCP_PORT and PORT for each instance if running on the same machine.

# Terminal 1 (Worker A):
export RENDEZVOUS_SERVICE_URL="http://localhost:8080" # Or your rendezvous server URL
export INTERNAL_TCP_PORT=8082
export PORT=8080 # Health check port for this worker
python main.py

# Terminal 2 (Worker B):
export RENDEZVOUS_SERVICE_URL="http://localhost:8080" # Or your rendezvous server URL
export INTERNAL_TCP_PORT=8083
export PORT=8081 # Health check port for this worker
python main.py
```

## Google Cloud Prerequisites (for Cloud Run Deployment)

If you plan to deploy these services to Google Cloud Run, you'll need to set up your environment. Here are some common `gcloud` commands for prerequisites:

### 1. Install and Initialize Google Cloud SDK

If you haven't already, [install the Google Cloud SDK](https://cloud.google.com/sdk/docs/install) and initialize it:

```bash
# Follow instructions from the link above to install

# Initialize the SDK
gcloud init
```

### 2. Log In and Set Project

```bash
# Log in to your Google Account
gcloud auth login

# List your projects
gcloud projects list

# Set your default project (replace YOUR_PROJECT_ID)
export PROJECT_ID="YOUR_PROJECT_ID"
gcloud config set project $PROJECT_ID
```

### 3. Enable Necessary APIs

For Cloud Run and Artifact Registry (if you plan to store your Docker images there):

```bash
# Enable Cloud Run API
gcloud services enable run.googleapis.com --project=$PROJECT_ID

# Enable Artifact Registry API (for storing Docker images)
gcloud services enable artifactregistry.googleapis.com --project=$PROJECT_ID

# (Optional) Enable Cloud Build API if you plan to use Cloud Build
gcloud services enable cloudbuild.googleapis.com --project=$PROJECT_ID
```

### 4. Configure Docker Authentication for Artifact Registry

If you intend to push your Docker images to Google Artifact Registry:

```bash
# Configure Docker to use gcloud as a credential helper for Artifact Registry
# Replace YOUR_REGION with the region for your Artifact Registry (e.g., us-central1)
gcloud auth configure-docker YOUR_REGION-docker.pkg.dev
```

### 5. (Optional) Create an Artifact Registry Docker Repository

If you don't have one already, create a repository to store your Docker images:

```bash
# Replace YOUR_REPOSITORY_NAME and YOUR_REGION
export AR_REPO="YOUR_REPOSITORY_NAME"
export REGION="YOUR_REGION" # e.g., us-central1

gcloud artifacts repositories create $AR_REPO \
    --repository-format=docker \
    --location=$REGION \
    --description="Docker repository for TCP hole punching project"

# You can then tag and push images like so:
# docker tag rendezvous-service $REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPO/rendezvous-service:latest
# docker push $REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPO/rendezvous-service:latest
```

With these prerequisites in place, you can proceed to build your Docker images and deploy them to Cloud Run. You would typically use `gcloud run deploy` for deployment.

## Deploying to Google Cloud Run

Once your Docker images are built and (optionally) pushed to Artifact Registry, you can deploy them to Cloud Run. The `redeploy.sh` script in the project root automates this process for the rendezvous service and, for research purposes, two worker instances.

**Automated Redeployment via `redeploy.sh`:**
The `redeploy.sh` script handles:
1.  Building Docker images for both the rendezvous service and the worker client (for `linux/amd64`).
2.  Pushing these images to your configured Google Artifact Registry.
3.  Deploying/updating the `rendezvous-service` to Cloud Run.
4.  Deploying/updating two worker instances (`holepunch-worker-a` and `holepunch-worker-b`) to Cloud Run. This setup allows for testing direct P2P communication viability between serverless instances.

Before running `./redeploy.sh`:
1.  Ensure your `tcp_hole_punching/.envrc` file is correctly configured with your GCP project details, region, Artifact Registry repository, service names, desired image tags, ports, and the correct `RENDEZVOUS_SERVICE_URL` (updated after the first successful rendezvous deployment).
2.  Make sure you have `direnv` installed and have run `direnv allow`, or have manually exported the variables from `.envrc`.
3.  Ensure Docker is running, `gcloud` is authenticated, and you have executable permissions for the script (`chmod +x redeploy.sh`).

### 1. Deployed Services (via `redeploy.sh`)

#### a. Rendezvous Service (`rendezvous-service`)
*   **Purpose**: Coordinates pairing between worker instances.
*   **Deployment**: Deployed by `redeploy.sh` using the image from Artifact Registry.
    *   `gcloud run deploy "${CLOUDRUN_RENDEZVOUS_SERVICE_NAME}" ...`
*   **Key Config from `.envrc`**: `PROJECT_ID`, `REGION`, `AR_REPO`, `RENDEZVOUS_IMAGE_NAME`, `RENDEZVOUS_IMAGE_TAG`, `RENDEZVOUS_CONTAINER_PORT`.
*   **Accessibility**: Publicly accessible via HTTPS, uses session affinity.

#### b. Worker Instance A (`holepunch-worker-a`)
*   **Purpose**: A peer instance for P2P testing, running on Cloud Run.
*   **Deployment**: Deployed by `redeploy.sh` using the worker image from Artifact Registry.
    *   `gcloud run deploy "${WORKER_A_SERVICE_NAME}" ...`
*   **Key Config from `.envrc`**: `WORKER_A_SERVICE_NAME`, `WORKER_IMAGE_TAG`, `WORKER_A_CLOUDRUN_HEALTH_PORT` (for its health check via `$PORT`), `WORKER_A_INTERNAL_TCP_PORT` (for P2P logic).
*   **Connection**: Connects to the `RENDEZVOUS_SERVICE_URL` defined in `.envrc`.

#### c. Worker Instance B (`holepunch-worker-b`)
*   **Purpose**: The second peer instance for P2P testing, running on Cloud Run.
*   **Deployment**: Deployed by `redeploy.sh` using the worker image from Artifact Registry.
    *   `gcloud run deploy "${WORKER_B_SERVICE_NAME}" ...`
*   **Key Config from `.envrc`**: `WORKER_B_SERVICE_NAME`, `WORKER_IMAGE_TAG`, `WORKER_B_CLOUDRUN_HEALTH_PORT` (for its health check via `$PORT`), `WORKER_B_INTERNAL_TCP_PORT` (for P2P logic).
*   **Connection**: Connects to the `RENDEZVOUS_SERVICE_URL` defined in `.envrc`.

*(The following sections on manual deployment are kept for reference but `redeploy.sh` is the recommended path for the full setup)*

### 2. Manual Deployment Steps (Reference)

#### a. Rendezvous Service

Assuming your image is tagged as `rendezvous-service:latest` (locally) or pushed to Artifact Registry (e.g., `YOUR_REGION-docker.pkg.dev/YOUR_PROJECT_ID/YOUR_REPOSITORY_NAME/rendezvous-service:latest`):

```bash
# Recommended: Deploy from Artifact Registry
# Ensure you have set $PROJECT_ID, $REGION, and $AR_REPO from the prerequisites section.
export RENDEZVOUS_IMAGE_URL="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${RENDEZVOUS_IMAGE_NAME}:${RENDEZVOUS_IMAGE_TAG}" # Ensure RENDEZVOUS_IMAGE_TAG is set from .envrc

# Deploy to Cloud Run
gcloud run deploy rendezvous-service \
    --image "$RENDEZVOUS_IMAGE_URL" \
    --platform managed \
    --region "$REGION" \
    --port 8080               # The port your application listens on (container will use this as $PORT)
    --allow-unauthenticated   # To make it publicly accessible for workers
    --project "$PROJECT_ID"
    --session-affinity # Added from redeploy.sh logic
```

After deployment, `gcloud` will output the URL for your service. This URL will be used as the `RENDEZVOUS_SERVICE_URL` by your worker clients.


#### b. Worker Client (Conceptual for Local/Separate Networks)

The worker client (`holepunch/main.py`) is designed to run as a standalone client that connects *to* the rendezvous service. For a true P2P hole-punching experiment *across different NATs*, these workers would typically run on separate machines/networks outside of Cloud Run.

If you were to adapt the worker code to run as a service on Cloud Run (perhaps for testing aspects of it or for a scenario where Cloud Run instances are the "peers" as demonstrated by `redeploy.sh`), you would deploy it similarly, ensuring the `RENDEZVOUS_SERVICE_URL`, `INTERNAL_TCP_PORT` and health check `PORT` are correctly set via environment variables.

### Google Cloud Networking Considerations for Cloud Run

*   **Public Accessibility:** When you deploy a service to Cloud Run and use the `--allow-unauthenticated` flag, Cloud Run automatically provides a public HTTPS URL. This is how the rendezvous service becomes accessible to the worker clients over the internet.
*   **Ingress:** Cloud Run manages ingress traffic to your service. You typically don't need to configure `gcloud` firewall rules for the Cloud Run service itself; access is controlled by IAM and the `--allow-unauthenticated` flag (or lack thereof for internal services).
*   **Egress (Outbound Connections):**
    *   Cloud Run services can make outbound connections to the internet by default (e.g., to call `api.ipify.org` or connect to a WebSocket service).
    *   Outbound IP addresses are generally ephemeral. If you need a static outbound IP address (e.g., for whitelisting by an external service), you would need to configure a Serverless VPC Access connector and Cloud NAT. This is an advanced configuration and not typically required for this basic hole-punching experiment where the rendezvous service is the main public endpoint.
*   **Port Configuration:** The `--port` flag in `gcloud run deploy` specifies the port your container listens on. Cloud Run maps its external port 80/443 to this container port.
*   **WebSocket Support:** Cloud Run supports WebSockets, which is essential for the rendezvous service.

For this project, the rendezvous service deployed to Cloud Run primarily relies on the default public accessibility. The networking complexity usually lies with the worker clients and their local network environments (NATs, firewalls).

## General Notes

*   The code uses structured logging via Python's `logging` module.
*   Type hints are used for better code clarity and maintainability.
*   The Dockerfiles are based on `python:3.9-slim` but can be adjusted to newer Python versions.
*   Successful TCP hole punching depends heavily on the type of NAT devices involved. This experiment may not work in all network environments. 