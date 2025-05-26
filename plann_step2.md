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

1.  Create and deploy a **Rendezvous Service** on Cloud Run. This service will accept WebSocket connections from workers, observe their public IP address (from the WebSocket connection) and port, and keep track of active workers. Workers can also send a self-discovered HTTP-based public IP.
2.  Modify the existing **Worker Service** to:
    *   Generate a unique ID for itself upon startup.
    *   Connect to the Rendezvous Service via a WebSocket.
    *   Register itself by sending its unique ID and its HTTP-discovered public IP (e.g., from `ipify.org`).
    *   Maintain this WebSocket connection (with basic reconnection logic).

This lays the groundwork for peer discovery and STUN-based UDP endpoint reporting in subsequent steps.

### Prerequisites

1.  **Basic Project Setup:** Familiarity with `gcloud`, Docker, and a GCP project. The networking setup from a conceptual "Step 1" (VPC, NAT for workers) is assumed if deploying to Cloud Run and expecting workers to be NATed.
2.  **gcloud SDK and Configuration:**
    *   `gcloud` installed and authenticated.
    *   Project set: `gcloud config set project iceberg-eli`
    *   Default region set (optional, but helps): `gcloud config set compute/region us-central1`
3.  **APIs Enabled:**
    ```bash
    gcloud services enable \\
        cloudbuild.googleapis.com \\
        run.googleapis.com \\
        artifactregistry.googleapis.com
    ```

----

## Part 1: The Rendezvous Service

This is a **NEW** service you will create.

### 1.1. Define Shell Variables (for Rendezvous Service)

```bash
export PROJECT_ID=$(gcloud config get-value project)
export REGION="us-central1" # Or your preferred region

export AR_RENDEZVOUS_REPO_NAME="rendezvous-repo"
export RENDEZVOUS_SERVICE_NAME="rendezvous-service"
export RENDEZVOUS_IMAGE_TAG_STEP2="v_step2"
```

### 1.2. Rendezvous Service Code

Create a new directory for the Rendezvous service (e.g., `rendezvous_service_code`). Inside this directory, create the following files:

**`rendezvous_service_code/main.py` (for Step 2):**

```python
import asyncio
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from typing import Dict, Any
import os
import json

app = FastAPI(title="Rendezvous Service - Step 2")

# In-memory storage for connected workers for Step 2.
# Format: {worker_id: {"websocket_observed_ip": str, 
#                       "websocket_observed_port": int,
#                       "http_reported_public_ip": Optional[str],
#                       "websocket": WebSocket}}
connected_workers: Dict[str, Dict[str, Any]] = {}

@app.websocket("/ws/register/{worker_id}")
async def websocket_register_worker(websocket: WebSocket, worker_id: str):
    await websocket.accept()
    client_host = websocket.client.host
    client_port = websocket.client.port
    
    print(f"Worker '{worker_id}' connecting from WebSocket: {client_host}:{client_port}")

    if worker_id in connected_workers:
        print(f"Worker '{worker_id}' re-connecting or duplicate ID. Closing old session if active.")
        old_ws = connected_workers[worker_id].get("websocket")
        if old_ws and hasattr(old_ws, 'client_state') and old_ws.client_state.value == 1: # WebSocketState.CONNECTED
             try:
                await old_ws.close(code=1000, reason="New connection from same worker ID")
             except Exception as e_close:
                print(f"Error closing old WebSocket for '{worker_id}': {e_close}")

    connected_workers[worker_id] = {
        "websocket_observed_ip": client_host,
        "websocket_observed_port": client_port,
        "http_reported_public_ip": None, # Will be updated by worker message
        "websocket": websocket 
    }
    print(f"Worker '{worker_id}' registered. WS Endpoint: {client_host}:{client_port}. Total workers: {len(connected_workers)}")

    try:
        while True:
            raw_data = await websocket.receive_text()
            print(f"Rendezvous: Received from '{worker_id}': {raw_data}")
            try:
                message = json.loads(raw_data)
                msg_type = message.get("type")

                if msg_type == "register_public_ip": # Worker sends its HTTP-discovered IP
                    new_ip = message.get("ip")
                    if new_ip and worker_id in connected_workers:
                        connected_workers[worker_id]["http_reported_public_ip"] = new_ip
                        print(f"Worker '{worker_id}' reported HTTP-based public IP: {new_ip}")
                    elif not new_ip:
                        print(f"Worker '{worker_id}' sent 'register_public_ip' without an IP.")
                
                elif msg_type == "echo_request": # Simple echo for testing
                    payload = message.get("payload", "")
                    await websocket.send_text(json.dumps({
                        "type": "echo_response", 
                        "original_payload": payload,
                        "processed_by_rendezvous": f"Rendezvous processed echo for worker {worker_id}"
                    }))
                    print(f"Sent echo_response to worker '{worker_id}'")
                else:
                    print(f"Worker '{worker_id}' sent unhandled message type: {msg_type}")

            except json.JSONDecodeError:
                print(f"Worker '{worker_id}' sent non-JSON message: {raw_data}")
            except KeyError:
                 print(f"Worker '{worker_id}' no longer in connected_workers dict.")
            except Exception as e_proc:
                print(f"Error processing message from '{worker_id}': {type(e_proc).__name__} - {e_proc}")


    except WebSocketDisconnect:
        print(f"Worker '{worker_id}' disconnected from {client_host}:{client_port}.")
    except Exception as e_ws:
        print(f"Error with worker '{worker_id}' WebSocket: {type(e_ws).__name__} - {e_ws}")
    finally:
        if worker_id in connected_workers and connected_workers[worker_id]["websocket"] == websocket:
            del connected_workers[worker_id]
            print(f"Worker '{worker_id}' de-registered. Total workers: {len(connected_workers)}")

@app.get("/")
async def read_root():
    return {"message": "Rendezvous Service (Step 2 Version) is running. Connect via WebSocket at /ws/register/{worker_id}"}

@app.get("/debug/list_workers")
async def list_workers_debug():
    workers_info = {}
    for w_id, data in list(connected_workers.items()): # Iterate over a copy
        ws_object = data.get("websocket")
        is_connected = False
        if ws_object and hasattr(ws_object, 'client_state'):
            is_connected = (ws_object.client_state.value == 1) # WebSocketState.CONNECTED.value

        workers_info[w_id] = {
            "websocket_observed_ip": data.get("websocket_observed_ip"),
            "websocket_observed_port": data.get("websocket_observed_port"),
            "http_reported_public_ip": data.get("http_reported_public_ip"),
            "websocket_connected": is_connected
        }
    return {"connected_workers_count": len(workers_info), "workers": workers_info}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
```

**`rendezvous_service_code/requirements.txt` (for Step 2):**

```
fastapi>=0.110.0 # Or a recent stable version
uvicorn[standard]>=0.27.0 # Or a recent stable version
```

**`rendezvous_service_code/Dockerfile.rendezvous` (for Step 2):**
(This Dockerfile is standard and likely matches your current `Dockerfile.rendezvous` if it's for FastAPI/Uvicorn)
```dockerfile
FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8080
ENV PORT 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0"]
```

### 1.3. Build and Deploy the Rendezvous Service (Step 2 Version)

1.  **Create Artifact Registry Repository (if not done):**
    ```bash
    gcloud artifacts repositories create $AR_RENDEZVOUS_REPO_NAME \\
        --project=$PROJECT_ID \\
        --repository-format=docker \\
        --location=$REGION \\
        --description="Docker repository for Rendezvous Service"
    # Grant Cloud Build permissions if needed (see original plan for command)
    ```

2.  **Build and Push Image (from `rendezvous_service_code` directory):**
    ```bash
    # cd rendezvous_service_code
    # Ensure your Dockerfile is named Dockerfile or use --dockerfile flag
    gcloud builds submit --region=$REGION \\
        --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:${RENDEZVOUS_IMAGE_TAG_STEP2} . \\
        --dockerfile=Dockerfile.rendezvous 
    # Or use: mv Dockerfile.rendezvous Dockerfile; gcloud builds submit ...; mv Dockerfile Dockerfile.rendezvous
    ```

3.  **Deploy to Cloud Run:**
    ```bash
    gcloud run deploy $RENDEZVOUS_SERVICE_NAME \\
        --project=$PROJECT_ID \\
        --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:${RENDEZVOUS_IMAGE_TAG_STEP2} \\
        --platform=managed \\
        --region=$REGION \\
        --allow-unauthenticated \\
        --session-affinity \\
        --min-instances=1 # Optional: keep one instance warm
    ```
    *Note the URL of the deployed Rendezvous Service for the worker.*

----

## Part 2: Modified Worker Service (Step 2 Version)

Modify your existing worker service (e.g., in the root `holepunch` directory) to connect to the Rendezvous service.

### 2.1. Define Shell Variables (for Worker Service)

```bash
export PROJECT_ID=$(gcloud config get-value project)
export REGION="us-central1"

export AR_WORKER_REPO_NAME="ip-worker-repo" # Assuming this repo exists from a conceptual Step 1
export WORKER_SERVICE_NAME="ip-worker-service" 
export WORKER_IMAGE_TAG_STEP2="v_step2"

# Set RENDEZVOUS_SERVICE_URL from the output of the Rendezvous service deployment
# export RENDEZVOUS_SERVICE_URL="YOUR_RENDEZVOUS_SERVICE_URL_HERE"
# Example:
# RENDEZVOUS_SERVICE_URL=$(gcloud run services describe $RENDEZVOUS_SERVICE_NAME --platform managed --region $REGION --project $PROJECT_ID --format 'value(status.url)')
# echo "Rendezvous URL: $RENDEZVOUS_SERVICE_URL"
```
**ACTION: Ensure `RENDEZVOUS_SERVICE_URL` is correctly set before deploying the worker.**

### 2.2. Worker Service Code Updates (Step 2 Version)

**`main.py` (Worker - for Step 2):**
(This is a simplified version focusing on Rendezvous registration and HTTP IP reporting)

```python
import asyncio
import os
import uuid
import websockets
import signal
import json
import requests # For ipify.org

# Global variables for Step 2
worker_id = str(uuid.uuid4())
stop_signal_received = False

# For Cloud Run health checks (minimal, non-async for this step's focus)
# This will be replaced by an async-integrated HTTP server in later steps.
import http.server
import socketserver
import threading

HTTP_PORT_FOR_HEALTH = int(os.environ.get("PORT", 8080))

def start_minimal_health_check_server():
    class HealthCheckHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/health':
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"OK")
            else: # Default to OK for Cloud Run if it hits '/'
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Worker active - health check OK")
        def log_message(self, format, *args):
            return # Suppress log messages for health checks

    httpd = socketserver.TCPServer(("", HTTP_PORT_FOR_HEALTH), HealthCheckHandler)
    print(f"Worker '{worker_id}': Minimal health check server listening on port {HTTP_PORT_FOR_HEALTH}")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()

def handle_shutdown_signal(signum, frame):
    global stop_signal_received
    print(f"Worker '{worker_id}': Shutdown signal ({signum}) received. Attempting graceful exit.")
    stop_signal_received = True

async def connect_to_rendezvous(rendezvous_ws_url_base: str):
    global stop_signal_received, worker_id
    
    full_rendezvous_ws_url = f"{rendezvous_ws_url_base}/ws/register/{worker_id}"
    print(f"Worker '{worker_id}': Attempting to connect to Rendezvous: {full_rendezvous_ws_url}")
    
    ip_echo_service_url = "https://api.ipify.org"
    ping_interval = float(os.environ.get("PING_INTERVAL_SEC", "25"))
    ping_timeout = float(os.environ.get("PING_TIMEOUT_SEC", "25"))

    while not stop_signal_received:
        try:
            async with websockets.connect(
                full_rendezvous_ws_url,
                ping_interval=ping_interval,
                ping_timeout=ping_timeout
            ) as websocket:
                print(f"Worker '{worker_id}': Connected to Rendezvous.")

                # Try to get public IP via HTTP echo service and send it
                try:
                    print(f"Worker '{worker_id}': Fetching public IP from {ip_echo_service_url}...")
                    response = requests.get(ip_echo_service_url, timeout=10)
                    response.raise_for_status()
                    public_ip = response.text.strip()
                    print(f"Worker '{worker_id}': Identified HTTP-based public IP as: {public_ip}")
                    await websocket.send(json.dumps({
                        "type": "register_public_ip",
                        "ip": public_ip
                    }))
                    print(f"Worker '{worker_id}': Sent HTTP-based public IP to Rendezvous.")
                except requests.exceptions.RequestException as e_req:
                    print(f"Worker '{worker_id}': Error fetching/sending public IP: {e_req}. Will rely on WS observed IP at Rendezvous.")
                except Exception as e_ip_send:
                    print(f"Worker '{worker_id}': Error sending public IP over WebSocket: {e_ip_send}")
                
                # Keep connection alive and listen for messages (e.g., echo_response)
                while not stop_signal_received:
                    try:
                        message_raw = await asyncio.wait_for(websocket.recv(), timeout=ping_interval + 5)
                        print(f"Worker '{worker_id}': Received from Rendezvous: {message_raw}")
                        # Basic message handling for this step
                        try:
                            message_data = json.loads(message_raw)
                            if message_data.get("type") == "echo_response":
                                print(f"Worker '{worker_id}': Echo response content: {message_data.get('processed_by_rendezvous')}")
                        except json.JSONDecodeError:
                            print(f"Worker '{worker_id}': Received non-JSON from Rendezvous: {message_raw}")
                    except asyncio.TimeoutError:
                        # Timeout is expected if no messages, pings handle keepalive
                        pass 
                    except websockets.exceptions.ConnectionClosed:
                        print(f"Worker '{worker_id}': Rendezvous WebSocket connection closed by server.")
                        break 
                    except Exception as e_recv_loop:
                        print(f"Worker '{worker_id}': Error in WebSocket receive loop: {e_recv_loop}")
                        break
                
                if stop_signal_received: break

        except websockets.exceptions.InvalidURI:
            print(f"Worker '{worker_id}': Invalid Rendezvous WebSocket URI: {full_rendezvous_ws_url}. Exiting worker.")
            return # Critical error, stop trying
        except ConnectionRefusedError:
            print(f"Worker '{worker_id}': Connection to Rendezvous refused at {full_rendezvous_ws_url}.")
        except websockets.exceptions.ConnectionClosedError as e_closed_err:
            print(f"Worker '{worker_id}': Rendezvous WS connection unexpectedly closed: {e_closed_err}")
        except Exception as e_connect:
            print(f"Worker '{worker_id}': Error connecting to Rendezvous ({full_rendezvous_ws_url}): {type(e_connect).__name__} - {e_connect}.")
        
        if not stop_signal_received:
            print(f"Worker '{worker_id}': Retrying connection to Rendezvous in 10 seconds...")
            await asyncio.sleep(10)
        else:
            break

    print(f"Worker '{worker_id}': Stopped WebSocket connection attempts to Rendezvous.")

if __name__ == "__main__":
    print(f"WORKER SCRIPT (ID: {worker_id}): Initializing for Step 2...")
    
    start_minimal_health_check_server() # For Cloud Run

    rendezvous_base_url = os.environ.get("RENDEZVOUS_SERVICE_URL")
    if not rendezvous_base_url:
        print(f"CRITICAL ERROR: Worker '{worker_id}': RENDEZVOUS_SERVICE_URL environment variable not set. Exiting.")
        exit(1)

    # Ensure correct WebSocket scheme (ws:// or wss://)
    if rendezvous_base_url.startswith("http://"):
        rendezvous_ws_url_base_constructed = rendezvous_base_url.replace("http://", "ws://", 1)
    elif rendezvous_base_url.startswith("https://"):
        rendezvous_ws_url_base_constructed = rendezvous_base_url.replace("https://", "wss://", 1)
    else:
        # Assuming if no scheme, it's for local testing and might need ws://
        print(f"Warning: RENDEZVOUS_SERVICE_URL ('{rendezvous_base_url}') lacks http/https scheme. Defaulting to ws://")
        rendezvous_ws_url_base_constructed = f"ws://{rendezvous_base_url}"
    
    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)

    try:
        asyncio.run(connect_to_rendezvous(rendezvous_ws_url_base_constructed))
    except KeyboardInterrupt:
        print(f"Worker '{worker_id}': Interrupted by user (KeyboardInterrupt).")
    except Exception as e_main_run:
        print(f"Worker '{worker_id}': CRITICAL ERROR in main asyncio.run: {type(e_main_run).__name__} - {e_main_run}")
    finally:
        print(f"Worker '{worker_id}': Main process finished or exited.")
```

**`requirements.txt` (Worker - for Step 2):**

```
websockets>=12.0
requests>=2.30.0 
# pystun3 is NOT needed for this step
```

**`Dockerfile.worker` (for Step 2):**
(This is a standard Python Dockerfile and likely similar to your current one, ensure CMD is correct)
```dockerfile
FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
# If index.html exists and needs to be served by a full HTTP server eventually,
# copy it too. For Step 2, main.py only runs health checks.
# COPY index.html . 

# PORT for health checks (Cloud Run default is 8080)
ENV PORT 8080 

# -u for unbuffered Python output
CMD ["python", "-u", "main.py"]
```

### 2.3. Build and Re-deploy the Worker Service (Step 2 Version)

1.  **Build and Push Worker Image (from `holepunch` or root directory):**
    ```bash
    # cd path/to/your/project_root 
    # Ensure your Dockerfile for worker is named Dockerfile or use --dockerfile flag
    gcloud builds submit --region=$REGION \\
        --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG_STEP2} . \\
        --dockerfile=Dockerfile.worker
    ```

2.  **Re-deploy the Worker Service to Cloud Run:**
    ```bash
    if [ -z "$RENDEZVOUS_SERVICE_URL" ]; then
        echo "Error: RENDEZVOUS_SERVICE_URL is not set. Please set it."
        # exit 1 # Uncomment to make it a fatal error
    fi

    gcloud run deploy $WORKER_SERVICE_NAME \\
        --project=$PROJECT_ID \\
        --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG_STEP2} \\
        --platform=managed \\
        --region=$REGION \\
        --set-env-vars="RENDEZVOUS_SERVICE_URL=${RENDEZVOUS_SERVICE_URL}" \\
        # For workers behind Cloud NAT, VPC egress is needed
        # --vpc-egress=all-traffic \\
        # --network=$VPC_NETWORK_NAME \\ 
        # --subnet=$SUBNET_NAME \\
        --min-instances=1 # Deploy at least one worker for testing this step
        # --allow-unauthenticated # Not strictly needed as worker has no public HTTP endpoints in this step
    ```

----

## Part 3: Verification (for Step 2)

1.  **Check Rendezvous Service Logs:**
    *   Worker connections: `Worker '...' connecting from WebSocket: ...`
    *   Worker registration: `Worker '...' registered. WS Endpoint: ... Total workers: X`
    *   Receipt of HTTP IP: `Worker '...' reported HTTP-based public IP: ...`

2.  **Check Worker Service Logs:**
    *   Health check server startup: `Worker '...': Minimal health check server listening...`
    *   Connection attempt: `Worker '...': Attempting to connect to Rendezvous: ...`
    *   Successful connection: `Worker '...': Connected to Rendezvous.`
    *   Fetching/sending HTTP IP: `Worker '...': Identified HTTP-based public IP as: ...` and `Sent HTTP-based public IP to Rendezvous.`

3.  **Use the Rendezvous Debug Endpoint (`/debug/list_workers`):**
    *   Open the Rendezvous service URL in a browser and navigate to `/debug/list_workers`.
    *   Confirm workers are listed with their `websocket_observed_ip` and `http_reported_public_ip`.
    *   `websocket_connected` should be `true`.

This completes the setup for basic worker registration with the Rendezvous service. The next step will introduce STUN for UDP endpoint discovery.