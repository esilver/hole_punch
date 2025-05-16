Okay, this is an exciting next step\! Now that we've proven your "worker" Cloud Run service can present a stable public IP (via Cloud NAT), let's build the system that allows workers to discover each other.

This involves two main parts:

1.  **Creating a Rendezvous Service:** This is a new, central service that workers will connect to. It will learn about each worker's public NAT endpoint (`IP:Port`).
2.  **Modifying the Worker Service:** Your existing worker service will be updated to connect to this new Rendezvous service via WebSockets and register itself.

This `README.md` will guide you through this. I've been extra careful with library versions and syntax, referencing current best practices.

**Project ID:** `iceberg-eli`
**Region:** `us-central1` (continuing from previous setup)

-----

## README - Step 2: Rendezvous Service and Worker Registration

### Objective

This step aims to:

1.  Create and deploy a **Rendezvous Service** on Cloud Run. This service will accept WebSocket connections from workers, observe their public NAT IP address and port, and keep track of active workers.
2.  Modify the existing **Worker Service** (from Step 1) to:
      * Generate a unique ID for itself upon startup.
      * Connect to the Rendezvous Service via a WebSocket.
      * Register itself by sending its unique ID.
      * Maintain this WebSocket connection (with basic reconnection logic).

This lays the groundwork for peer discovery, which is essential for P2P hole punching.

### Prerequisites

1.  **Completion of Step 1:** You must have successfully completed the previous step where you set up the Worker Service with Cloud NAT and verified its public IP observation. The VPC, subnet, Cloud Router, and Cloud NAT gateway (`ip-worker-nat`) should still be in place.
2.  **gcloud SDK and Configuration:**
      * `gcloud` installed and authenticated.
      * Project set: `gcloud config set project iceberg-eli`
      * Default region set (optional, but helps): `gcloud config set compute/region us-central1`
3.  **APIs Enabled:** The same APIs from Step 1 should still be enabled:
    ```bash
    gcloud services enable \
        cloudbuild.googleapis.com \
        run.googleapis.com \
        compute.googleapis.com \
        artifactregistry.googleapis.com \
        iam.googleapis.com
    ```

-----

## Part 1: The Rendezvous Service

This is a **NEW** service you will create.

### 1.1. Define Shell Variables (for Rendezvous Service)

```bash
export PROJECT_ID="iceberg-eli"
export REGION="us-central1"
export AR_RENDEZVOUS_REPO_NAME="rendezvous-repo" # New Artifact Registry repo
export RENDEZVOUS_SERVICE_NAME="rendezvous-service" # New Cloud Run service name
export RENDEZVOUS_IMAGE_TAG_LATEST="latest"
```

### 1.2. Rendezvous Service Code

Create a new directory for the Rendezvous service (e.g., `rendezvous_service_code`). Inside this directory, create the following files:

**`rendezvous_service_code/main.py`:**

```python
import asyncio
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from typing import Dict, Tuple
import os
import json

app = FastAPI(title="Rendezvous Service")

# In-memory storage for connected workers.
# Format: {worker_id: {"public_ip": str, "public_port": int, "websocket": WebSocket}}
# WARNING: This is for PoC only. Data will be lost if the service restarts or scales to zero.
# For production, use an external store like Redis or Firestore.
connected_workers: Dict[str, Dict] = {}

@app.websocket("/ws/register/{worker_id}")
async def websocket_register_worker(websocket: WebSocket, worker_id: str):
    await websocket.accept()
    client_host = websocket.client.host
    client_port = websocket.client.port
    
    print(f"Worker '{worker_id}' connecting from {client_host}:{client_port}")

    if worker_id in connected_workers:
        print(f"Worker '{worker_id}' re-connecting or duplicate ID detected.")
        old_ws = connected_workers[worker_id].get("websocket")
        if old_ws and hasattr(old_ws, 'client_state') and old_ws.client_state.value == 1: # WebSocketState.CONNECTED
             try:
                await old_ws.close(code=1000, reason="New connection from same worker ID")
             except Exception:
                pass 

    connected_workers[worker_id] = {
        "public_ip": client_host,
        "public_port": client_port,
        "websocket": websocket 
    }
    print(f"Worker '{worker_id}' registered with initial endpoint: {client_host}:{client_port}. Total workers: {len(connected_workers)}")

    try:
        while True:
            raw_data = await websocket.receive_text()
            print(f"Received raw message from '{worker_id}': {raw_data}")
            try:
                message = json.loads(raw_data)
                msg_type = message.get("type")

                if msg_type == "register_public_ip":
                    new_ip = message.get("ip")
                    if new_ip and worker_id in connected_workers: # Check worker_id still exists
                        print(f"Worker '{worker_id}' self-reported public IP: {new_ip}. Updating from {connected_workers[worker_id]['public_ip']}.")
                        connected_workers[worker_id]["public_ip"] = new_ip
                    elif not new_ip:
                        print(f"Worker '{worker_id}' sent register_public_ip message without an IP.")
                    # else: worker might have disconnected before IP update processed
                else:
                    print(f"Worker '{worker_id}' sent unhandled message type: {msg_type}")

            except json.JSONDecodeError:
                print(f"Worker '{worker_id}' sent non-JSON message: {raw_data}")
            except AttributeError: 
                print(f"Worker '{worker_id}' sent malformed JSON message: {raw_data}")
            except KeyError:
                 print(f"Worker '{worker_id}' no longer in connected_workers dictionary, could not update IP.")

    except WebSocketDisconnect:
        print(f"Worker '{worker_id}' disconnected from {client_host}:{client_port}.")
    except Exception as e:
        print(f"Error with worker '{worker_id}': {e}")
    finally:
        if worker_id in connected_workers and connected_workers[worker_id]["websocket"] == websocket:
            del connected_workers[worker_id]
            print(f"Worker '{worker_id}' de-registered. Total workers: {len(connected_workers)}")

@app.get("/")
async def read_root():
    return {"message": "Rendezvous Service is running. Connect via WebSocket at /ws/register/{worker_id}"}

@app.get("/debug/list_workers")
async def list_workers():
    workers_info = {}
    for worker_id, data_val in connected_workers.items():
        ws_object = data_val["websocket"]
        current_client_state_value = None
        is_connected = False
        if ws_object and hasattr(ws_object, 'client_state'):
            current_client_state = ws_object.client_state
            current_client_state_value = current_client_state.value
            is_connected = (current_client_state_value == 1) # WebSocketState.CONNECTED.value
            print(f"DEBUG: Worker {worker_id}, WebSocket object: {ws_object}, client_state enum: {current_client_state}, raw value: {current_client_state_value}")
        else:
            print(f"DEBUG: Worker {worker_id}, no WebSocket object or client_state found in data.")

        workers_info[worker_id] = {
            "public_ip": data_val["public_ip"],
            "public_port": data_val["public_port"],
            "connected": is_connected,
            "raw_state": current_client_state_value
        }
    return {"connected_workers_count": len(workers_info), "workers": workers_info}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
```

**`rendezvous_service_code/requirements.txt`:**

```
fastapi>=0.111.0
uvicorn[standard]>=0.29.0 
# Using [standard] includes websockets and other useful dependencies for uvicorn.
# Check for latest stable versions if deploying much later.
```

*(Searched May 15, 2025: FastAPI 0.111.0 and Uvicorn 0.29.0 are current. Adjust if necessary based on actual release dates when you implement.)*

**`rendezvous_service_code/Dockerfile.rendezvous`:** (Note the specific Dockerfile name)

```dockerfile
# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file to the working directory
COPY requirements.txt .

# Install dependencies
# --no-cache-dir reduces image size
# --prefer-binary can speed up installs for packages with binary distributions
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt

# Copy the application code into the container
COPY main.py .

# Make port 8080 available (Cloud Run default)
EXPOSE 8080
ENV PORT 8080

# Command to run the Uvicorn server for FastAPI
# --host 0.0.0.0 makes it accessible from outside the container
# --port $PORT uses the port specified by the environment variable (set by Cloud Run)
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
```

### 1.3. Build and Deploy the Rendezvous Service

1.  **Create Artifact Registry Repository (if you haven't for this service):**

    ```bash
    gcloud artifacts repositories create $AR_RENDEZVOUS_REPO_NAME \
        --project=$PROJECT_ID \
        --repository-format=docker \
        --location=$REGION \
        --description="Docker repository for Rendezvous Service"
    ```

    *(Ensure the Cloud Build service account has `roles/artifactregistry.writer` as done in Step 1. The command was `export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)'); gcloud projects add-iam-policy-binding $PROJECT_ID --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" --role="roles/artifactregistry.writer"`)*

2.  **Build and Push Image (from inside `rendezvous_service_code` directory):**

    ```bash
    # Ensure you are in the 'rendezvous_service_code' directory
    cd path/to/your/rendezvous_service_code 

    # Option 1: Using Cloud Build (recommended for CI/CD)
    # Temporarily rename Dockerfile.rendezvous to Dockerfile for Cloud Build, then rename back
    mv Dockerfile.rendezvous Dockerfile
    gcloud builds submit --region=$REGION \
        --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:${RENDEZVOUS_IMAGE_TAG_LATEST} .
    mv Dockerfile Dockerfile.rendezvous

    # Option 2: Using local Docker (e.g., Docker Desktop)
    # If building on a non-amd64 machine (e.g., Apple M1/M2/M3), specify platform for Cloud Run compatibility:
    # docker buildx build --platform linux/amd64 -f Dockerfile.rendezvous -t ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:${RENDEZVOUS_IMAGE_TAG_LATEST} . --load
    # Otherwise, for amd64 machines:
    # docker build -f Dockerfile.rendezvous -t ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:${RENDEZVOUS_IMAGE_TAG_LATEST} .
    # docker push ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:${RENDEZVOUS_IMAGE_TAG_LATEST}
    ```

    *The `gcloud builds submit` command implicitly uses `Dockerfile` in the current directory. If your file is named `Dockerfile.rendezvous`, you either rename it for the build or use a `cloudbuild.yaml` to specify the Dockerfile name (not covered here). For local Docker builds, use the `-f` flag.* 

3.  **Deploy to Cloud Run:**
    The Rendezvous service needs to be publicly accessible.

    ```bash
    gcloud run deploy $RENDEZVOUS_SERVICE_NAME \
        --project=$PROJECT_ID \
        --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:${RENDEZVOUS_IMAGE_TAG_LATEST} \
        --platform=managed \
        --region=$REGION \
        --allow-unauthenticated \
        --session-affinity # Enable session affinity for WebSockets (good practice)
                           # --min-instances=1 # Optional: consider for production to reduce cold starts
    ```

      * `--session-affinity`: Helps ensure that subsequent requests from the same client (important for WebSocket re-establishment or related HTTP calls) are routed to the same Cloud Run instance, if you scale the Rendezvous service.
      * Note the URL of the deployed Rendezvous Service. You'll need it for the worker.

-----

## Part 2: Modified Worker Service

Now, we modify your existing `ip-worker-service` (from the `holepunch` directory in your provided files) to connect to the Rendezvous service.

### 2.1. Define Shell Variables (for Worker Service - some are from Step 1)

```bash
export PROJECT_ID="iceberg-eli"
export REGION="us-central1"
export AR_WORKER_REPO_NAME="ip-worker-repo" # Existing Artifact Registry repo from Step 1
export WORKER_SERVICE_NAME="ip-worker-service" # Existing Cloud Run service from Step 1
export WORKER_IMAGE_TAG_V2="v2" # New version tag for the worker image

# You'll need the URL of the deployed Rendezvous Service from Part 1.3
# Example: export RENDEZVOUS_SERVICE_URL="https://rendezvous-service-xxxx-uc.a.run.app" 
# SET THIS MANUALLY AFTER DEPLOYING RENDEZVOUS SERVICE:
export RENDEZVOUS_SERVICE_URL="YOUR_RENDEZVOUS_SERVICE_URL_HERE" 
```

**ACTION: Replace `YOUR_RENDEZVOUS_SERVICE_URL_HERE` with the actual URL after deploying the Rendezvous service.**

### 2.2. Worker Service Code Updates

Modify the files in your original `holepunch` directory (or a copy).

**`holepunch/main.py` (Modified Worker):**

```python
import asyncio
import os
import uuid
import websockets # Using the 'websockets' library
import signal # For graceful shutdown
import threading # For health check server
import http.server # For health check server
import socketserver # For health check server
import requests # For ipify.org
import json # For WebSocket messages

# This worker primarily functions as a WebSocket client.
# A minimal HTTP server is run in a background thread for Cloud Run health checks.

worker_id = str(uuid.uuid4())
stop_signal_received = False

def handle_shutdown_signal(signum, frame):
    global stop_signal_received
    print(f"Shutdown signal ({signum}) received. Attempting to close WebSocket connection.")
    stop_signal_received = True

async def connect_to_rendezvous(rendezvous_ws_url: str):
    global stop_signal_received
    print(f"WORKER CONNECT_TO_RENDEZVOUS: Entered function for URL: {rendezvous_ws_url}. stop_signal_received={stop_signal_received}")
    print(f"Worker '{worker_id}' attempting to connect to Rendezvous: {rendezvous_ws_url}")
    
    ip_echo_service_url = "https://api.ipify.org"
    # Default ping interval (seconds), can be overridden by environment variable
    ping_interval = float(os.environ.get("PING_INTERVAL_SEC", "25"))
    ping_timeout = float(os.environ.get("PING_TIMEOUT_SEC", "25"))

    while not stop_signal_received:
        try:
            async with websockets.connect(
                rendezvous_ws_url,
                ping_interval=ping_interval, # Send pings to keep connection alive
                ping_timeout=ping_timeout
            ) as websocket:
                print(f"Worker '{worker_id}' connected to Rendezvous.")

                try:
                    print(f"Worker '{worker_id}' fetching its public IP from {ip_echo_service_url}...")
                    response = requests.get(ip_echo_service_url, timeout=10)
                    response.raise_for_status()
                    public_ip = response.text.strip()
                    print(f"Worker '{worker_id}' identified public IP as: {public_ip}")
                    await websocket.send(json.dumps({
                        "type": "register_public_ip",
                        "ip": public_ip
                    }))
                    print(f"Worker '{worker_id}' sent public IP to Rendezvous.")
                except requests.exceptions.RequestException as e:
                    print(f"Worker '{worker_id}': Error fetching/sending public IP: {e}. Will rely on Rendezvous observed IP.")

                while not stop_signal_received:
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=max(30.0, ping_interval + ping_timeout + 5))
                        print(f"Received message from Rendezvous: {message}")
                    except asyncio.TimeoutError:
                        # No message received, pings are handling keepalive.
                        pass 
                    except websockets.exceptions.ConnectionClosed:
                        print(f"Worker '{worker_id}': Rendezvous WebSocket connection closed by server.")
                        break 

        except websockets.exceptions.ConnectionClosedOK:
            print(f"Worker '{worker_id}': Rendezvous WebSocket connection closed gracefully by server.")
        except websockets.exceptions.InvalidURI:
            print(f"Worker '{worker_id}': Invalid Rendezvous WebSocket URI: {rendezvous_ws_url}. Exiting.")
            return 
        except ConnectionRefusedError:
            print(f"Worker '{worker_id}': Connection to Rendezvous refused. Retrying in 10 seconds...")
        except Exception as e: # Catch other websocket errors like handshake timeouts
            print(f"Worker '{worker_id}': Error connecting/communicating with Rendezvous: {e}. Retrying in 10 seconds...")
        
        if not stop_signal_received:
            await asyncio.sleep(10) 
        else:
            break 

    print(f"Worker '{worker_id}' has stopped WebSocket connection attempts.")

# Minimal health-check HTTP server (as per Update 2025-05-16)
def start_healthcheck_http_server():
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self): self.send_response(200); self.send_header("Content-Type","text/plain"); self.end_headers(); self.wfile.write(b"OK")
        def log_message(self, format, *args): return
    port = int(os.environ.get("PORT", 8080))
    httpd = socketserver.TCPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"Health-check HTTP server listening on 0.0.0.0:{port}")

start_healthcheck_http_server()
print("HEALTHCHECK SERVER STARTED --- WORKER MAIN SCRIPT CONTINUING...")

if __name__ == "__main__":
    print("WORKER SCRIPT: Inside __main__ block.")
    rendezvous_base_url = os.environ.get("RENDEZVOUS_SERVICE_URL")
    if not rendezvous_base_url:
        print("Error: RENDEZVOUS_SERVICE_URL environment variable not set. Exiting.")
        exit(1)

    if rendezvous_base_url.startswith("http://"):
        rendezvous_ws_url_constructed = rendezvous_base_url.replace("http://", "ws://", 1)
    elif rendezvous_base_url.startswith("https://"):
        rendezvous_ws_url_constructed = rendezvous_base_url.replace("https://", "wss://", 1)
    else:
        rendezvous_ws_url_constructed = rendezvous_base_url 

    full_rendezvous_ws_url = f"{rendezvous_ws_url_constructed}/ws/register/{worker_id}"
    print(f"WORKER SCRIPT: About to call asyncio.run(connect_to_rendezvous) for URL: {full_rendezvous_ws_url}")
    
    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)

    try:
        asyncio.run(connect_to_rendezvous(full_rendezvous_ws_url))
    except KeyboardInterrupt:
        print(f"Worker '{worker_id}' interrupted by user. Shutting down.")
    finally:
        print(f"Worker '{worker_id}' main process finished.")
```

**`holepunch/requirements.txt` (Worker):**

```
websockets>=12.0
requests>=2.0.0 # For ipify.org
```

*(Note: `requests` was re-added. Original comment about Flask removal is still valid.)*

**`holepunch/Dockerfile.worker`:** (Note the specific Dockerfile name)

```dockerfile
# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt

# Copy the application code
COPY main.py .

# This worker doesn't run a Gunicorn server anymore; it runs the main.py script directly.
# No EXPOSE needed if it's only an outbound client.
# Using -u for unbuffered Python output, ensuring logs appear promptly in Cloud Run.
CMD ["python", "-u", "main.py"]
```

**Update (2025-05-16): Cloud Run health-check compatibility**  
Cloud Run expects every service revision to start an HTTP server that listens on the port provided in the `PORT` environment variable (default `8080`).  
Because our worker is a long-running WebSocket _client_ with no HTTP endpoints, Cloud Run would mark the revision unhealthy. To satisfy the default health-check we now start a **minimal background HTTP server** inside `main.py`. It responds with `200 OK` to any path and has negligible overhead. This tiny server is implemented with Python's standard `http.server` in a daemon thread and does **not** interfere with the worker's WebSocket logic.

You can see this in the top of the new `main.py` (look for `start_healthcheck_http_server()`).

### 2.3. Build and Re-deploy the Worker Service

1.  **Build and Push Worker Image (from inside `holepunch` directory):**

    ```bash
    # Ensure you are in the 'holepunch' directory (project root for this service)
    # cd path/to/your/holepunch 

    # Option 1: Using Cloud Build (recommended for CI/CD)
    # Temporarily rename Dockerfile.worker to Dockerfile for Cloud Build, then rename back
    mv Dockerfile.worker Dockerfile
    gcloud builds submit --region=$REGION \
        --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG_V2} .
    mv Dockerfile Dockerfile.worker

    # Option 2: Using local Docker (e.g., Docker Desktop)
    # If building on a non-amd64 machine (e.g., Apple M1/M2/M3), specify platform for Cloud Run compatibility:
    # docker buildx build --platform linux/amd64 -f Dockerfile.worker -t ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG_V2} . --load
    # Otherwise, for amd64 machines:
    # docker build -f Dockerfile.worker -t ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG_V2} .
    # docker push ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG_V2}
    ```
    *(Note: `AR_WORKER_REPO_NAME` is `ip-worker-repo` from Step 1. Ensure it exists. Use a new tag like `:v3`, `:v4` for subsequent builds.)*

2.  **Re-deploy the Worker Service to Cloud Run:**
    This updates your existing `ip-worker-service`. Crucially, we add the `RENDEZVOUS_SERVICE_URL` environment variable.
    **Remember to set `RENDEZVOUS_SERVICE_URL` in your shell first, from Part 2.1\!**

    ```bash
    if [ -z "$RENDEZVOUS_SERVICE_URL" ] || [ "$RENDEZVOUS_SERVICE_URL" == "YOUR_RENDEZVOUS_SERVICE_URL_HERE" ]; then
        echo "Error: RENDEZVOUS_SERVICE_URL is not set. Please set it to the URL of your deployed Rendezvous service."
        exit 1
    fi

    gcloud run deploy $WORKER_SERVICE_NAME \
        --project=$PROJECT_ID \
        --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG_V2} \
        --platform=managed \
        --region=$REGION \
        --set-env-vars="RENDEZVOUS_SERVICE_URL=${RENDEZVOUS_SERVICE_URL}" \
        # To update/add multiple env vars, including for WebSocket pings:
        # --update-env-vars="RENDEZVOUS_SERVICE_URL=${RENDEZVOUS_SERVICE_URL},PING_INTERVAL_SEC=25,PING_TIMEOUT_SEC=25" \
        --vpc-egress=all-traffic \
        --network=$VPC_NETWORK_NAME \ # From Step 1: ip-worker-vpc
        --subnet=$SUBNET_NAME \       # From Step 1: ip-worker-subnet
        --max-instances=2 # Example: deploy 2 worker instances to see them both register
                          # Keep other settings like CPU allocation as default or adjust as needed.
                          # Allow unauthenticated is not needed as this service doesn't serve inbound HTTP now.
                          # If you removed the Flask server, you might want to configure a startup CPU boost
                          # or set min-instances to 1 if you want it always running,
                          # but for this PoC, on-demand startup is fine.
    ```

      * The `--allow-unauthenticated` flag is removed as this worker no longer serves HTTP traffic. It only makes outbound connections.
      * Ensure the VPC and Subnet names (`$VPC_NETWORK_NAME`, `$SUBNET_NAME`) are the same as those used in Step 1 for Cloud NAT.

-----

## Part 3: Verification

1.  **Check Rendezvous Service Logs:**

      * Go to Google Cloud Console -> Cloud Run -> Select `rendezvous-service`.
      * View its logs. You should see messages like:
        `Worker 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx' connecting from <INITIAL_IP>:<INITIAL_PORT>`
        `Worker 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx' registered with initial endpoint: <INITIAL_IP>:<INITIAL_PORT>. Total workers: X`
        `Received raw message from 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx': {"type": "register_public_ip", "ip": "<ACTUAL_PUBLIC_NAT_IP>"}`
        `Worker 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx' self-reported public IP: <ACTUAL_PUBLIC_NAT_IP>. Updating from <INITIAL_IP>.`
      * The `<ACTUAL_PUBLIC_NAT_IP>` should be one of the IPs of your `ip-worker-nat` Cloud NAT gateway.
      * When calling `/debug/list_workers`, you might see `DEBUG: Worker xxxxxxxx..., client_state raw value: 1` (or similar for `WebSocketState.CONNECTED`).

2.  **Check Worker Service Logs:**
      * (`CMD ["python", "-u", "main.py"]` in `Dockerfile.worker` helps ensure logs appear promptly.)
      * Go to Google Cloud Console -> Cloud Run -> Select `ip-worker-service`.
      * View its logs. Each worker instance should log:
        `HEALTHCHECK SERVER STARTED --- WORKER MAIN SCRIPT CONTINUING...`
        `WORKER SCRIPT: Inside __main__ block.`
        `WORKER SCRIPT: About to call asyncio.run(connect_to_rendezvous) for URL: wss://<your-rendezvous-url>/ws/register/<worker_id>`
        `WORKER CONNECT_TO_RENDEZVOUS: Entered function for URL: ...`
        `Worker '...' attempting to connect to Rendezvous: ...`
        (Potentially some connection timeout/retry messages)
        `Worker '...' connected to Rendezvous.`
        `Worker '...' fetching its public IP from https://api.ipify.org...`
        `Worker '...' identified public IP as: <ACTUAL_PUBLIC_NAT_IP>`
        `Worker '...' sent public IP to Rendezvous.`

3.  **Use the Rendezvous Debug Endpoint (Optional but Recommended):**

      * If you deployed the Rendezvous service and it's running with `--allow-unauthenticated`, open its public URL in a browser and navigate to `/debug/list_workers`.
      * Example: `https://rendezvous-service-xxxx-uc.a.run.app/debug/list_workers`
      * This should show a JSON list of registered workers, e.g.:
        ```json
        {
          "connected_workers_count": 1,
          "workers": {
            "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx": {
              "public_ip": "<ACTUAL_PUBLIC_NAT_IP>",
              "public_port": <WORKER_INITIALLY_OBSERVED_PORT>,
              "connected": true,
              "raw_state": 1 
            }
          }
        }
        ```
      * `public_ip` should be the self-reported external NAT IP.
      * `connected` should be `true` if the WebSocket is active (derived from `raw_state == 1`).

-----