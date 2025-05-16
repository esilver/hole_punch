# P2P Hole Punching with Cloud Run: Step 2 - Rendezvous Service & Worker Registration

**Project ID:** `iceberg-eli`

## Objective
This step builds upon Step 1. We will:
1.  Create a **Rendezvous Service** using FastAPI and WebSockets, deployed on Cloud Run. This service will accept connections from workers.
2.  Modify the **Worker Service** to connect to the Rendezvous Service via WebSocket upon startup.
3.  The Rendezvous Service will log and store (in memory for this PoC) the public NAT IP address and source port of each connecting worker.

This establishes the mechanism for workers to announce their presence and their network endpoint (as seen by the Rendezvous server) to a central point.

## Prerequisites
* All prerequisites from Step 1 (Google Cloud SDK, Docker, enabled APIs).
* Completion of Step 1, including the VPC, subnet, and Cloud NAT gateway (`ip-worker-nat`) setup. We will reuse these.
* Familiarity with Python, FastAPI, and WebSockets is helpful.

## Directory Structure (Recommended)
To keep things organized, consider a structure like this for Step 2:
iceberg-eli-p2p-project/├── step1_ip_observer/          # Files from your completed Step 1│   ├── main.py│   └── Dockerfile├── step2_rendezvous_registration/│   ├── rendezvous_service/       # NEW: For the Rendezvous service│   │   ├── main.py│   │   ├── requirements.txt│   │   └── Dockerfile│   └── worker_service/           # UPDATED: For the modified Worker service│       ├── main.py│       ├── requirements.txt│       ├── Dockerfile│       └── .env.example          # For local testing configuration└── README_step2.md               # This file
## Part 1: Create the Rendezvous Service

This service will listen for WebSocket connections from workers.

**Location:** `step2_rendezvous_registration/rendezvous_service/`

### 1. `rendezvous_service/requirements.txt`
```txt
fastapi~=0.111.0
uvicorn[standard]~=0.29.0
websockets~=12.0
2. rendezvous_service/main.pyimport uvicorn
import uuid
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from typing import Dict, Tuple

app = FastAPI(title="Rendezvous Service")

# In-memory store for connected workers.
# Format: { "worker_id": {"public_ip": "x.x.x.x", "public_port": 12345, "websocket": WebSocket} }
# For production, use a persistent store like Redis or Firestore.
connected_workers: Dict[str, Dict] = {}

@app.websocket("/ws/register/{worker_id}")
async def websocket_endpoint(websocket: WebSocket, worker_id: str):
    """
    Handles WebSocket connections from workers for registration.
    The worker_id is expected to be a unique identifier sent by the worker.
    """
    await websocket.accept()
    client_host = websocket.client.host  # This is the NAT public IP
    client_port = websocket.client.port  # This is the NAT source port for this connection

    # If worker_id is already connected, handle re-connection or deny
    if worker_id in connected_workers:
        print(f"Worker {worker_id} reconnected or attempted duplicate connection from {client_host}:{client_port}.")
        # Optionally, close the old connection if it exists and is active
        # old_ws = connected_workers[worker_id].get("websocket")
        # if old_ws and old_ws.client_state == WebSocketState.CONNECTED:
        #     await old_ws.close(code=1008, reason="New connection established")
    
    connected_workers[worker_id] = {
        "public_ip": client_host,
        "public_port": client_port,
        "websocket": websocket  # Store the WebSocket object for potential future communication
    }
    print(f"Worker '{worker_id}' connected from {client_host}:{client_port}. Total workers: {len(connected_workers)}")
    print(f"Current worker details: { {k: {'ip': v['public_ip'], 'port': v['public_port']} for k,v in connected_workers.items()} }")

    try:
        while True:
            # Keep the connection alive, listen for messages (optional for this step)
            # For this PoC, we're mainly interested in the registration.
            # In a full P2P setup, workers might send/receive signals here.
            data = await websocket.receive_text() 
            print(f"Received message from {worker_id} ({client_host}:{client_port}): {data}")
            # Example: await websocket.send_text(f"Message received: {data}")
            
    except WebSocketDisconnect:
        print(f"Worker '{worker_id}' from {client_host}:{client_port} disconnected.")
    except Exception as e:
        print(f"Error with worker '{worker_id}' ({client_host}:{client_port}): {e}")
    finally:
        if worker_id in connected_workers:
            # Ensure the stored websocket is the one we are closing
            if connected_workers[worker_id]["websocket"] == websocket:
                 del connected_workers[worker_id]
                 print(f"Worker '{worker_id}' removed. Total workers: {len(connected_workers)}")


@app.get("/")
async def read_root():
    return {"message": "Rendezvous Service is running. Connect via WebSocket at /ws/register/{worker_id}"}

@app.get("/peers")
async def get_peers():
    """
    HTTP endpoint to view currently registered peers (their NAT IP:Port).
    This is for observation and debugging.
    """
    # Return a simplified list without the WebSocket objects
    peer_info = {
        worker_id: {"public_ip": data["public_ip"], "public_port": data["public_port"]}
        for worker_id, data in connected_workers.items()
    }
    return {"connected_workers": peer_info, "count": len(peer_info)}

if __name__ == "__main__":
    # This is for local testing. Cloud Run will use the CMD in Dockerfile.
    # Note: For local testing, ensure Uvicorn is run with --host 0.0.0.0
    # Example: uvicorn main:app --host 0.0.0.0 --port 8000 --reload
    print("Starting Rendezvous Service locally on port 8000 (intended for Cloud Run deployment via Gunicorn/Uvicorn worker)")
3. rendezvous_service/Dockerfile# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file to the working directory
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code into the container
COPY main.py .

# Expose the port the app runs on (FastAPI default via Uvicorn is 8000 or 80)
# Cloud Run will set the PORT environment variable. Uvicorn will respect it.
EXPOSE 8080 

# Command to run the Uvicorn server
# Uvicorn with Gunicorn is a common setup for production.
# Cloud Run's default Gunicorn will use Uvicorn workers for FastAPI.
# The PORT environment variable will be automatically picked up by Uvicorn.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
4. Define Shell Variables for Rendezvous Serviceexport RENDEZVOUS_SERVICE_NAME="rendezvous-service"
export RENDEZVOUS_AR_REPO_NAME="rendezvous-repo" # Or use the same repo as worker: ip-worker-repo

# Ensure PROJECT_ID and REGION are still set from Step 1
# export PROJECT_ID="iceberg-eli"
# export REGION="us-central1"
5. Build and Deploy the Rendezvous ServiceCreate Artifact Registry (if using a new one):gcloud artifacts repositories create $RENDEZVOUS_AR_REPO_NAME \
    --project=$PROJECT_ID \
    --repository-format=docker \
    --location=$REGION \
    --description="Docker repository for Rendezvous service"
(Ensure Cloud Build service account has roles/artifactregistry.writer as in Step 1)Build and Push (from step2_rendezvous_registration/rendezvous_service/ directory):gcloud builds submit --region=$REGION --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/${RENDEZVOUS_AR_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:latest
Deploy to Cloud Run:gcloud run deploy $RENDEZVOUS_SERVICE_NAME \
    --project=$PROJECT_ID \
    --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/${RENDEZVOUS_AR_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:latest \
    --platform=managed \
    --region=$REGION \
    --allow-unauthenticated \
    --cpu-boost # Good for services that need quick startup for WebSocket handshakes
    # --session-affinity # Consider enabling this for WebSockets if clients might reconnect often
    # Rendezvous service does NOT need VPC egress for this step.
Note the URL of the deployed Rendezvous service (e.g., https://rendezvous-service-xxxx.a.run.app). You'll need this for the worker. Let's call this YOUR_RENDEZVOUS_SERVICE_URL.Part 2: Modify the Worker ServiceThis service will now connect to the Rendezvous service.Location: step2_rendezvous_registration/worker_service/(You can copy Dockerfile from Step 1 and modify main.py and add requirements.txt)1. worker_service/requirements.txtFlask~=3.0.3
requests~=2.31.0
gunicorn~=22.0.0
websockets~=12.0 # For WebSocket client
python-dotenv~=1.0.1 # For managing environment variables locally
2. worker_service/.env.exampleCreate this file for local testing. Do not commit actual .env files with secrets.# For local testing: URL of your deployed Rendezvous Service (use wss:// for secure WebSockets)
# Example: RENDEZVOUS_URL=wss://rendezvous-service-xxxx-uc.a.run.app/ws/register
RENDEZVOUS_URL=
3. worker_service/main.py (Updated)import os
import requests
import asyncio
import websockets # WebSocket client library
import uuid
import threading
import time
from flask import Flask
from dotenv import load_dotenv

load_dotenv() # Load environment variables from .env file for local development

app = Flask(__name__)

IP_ECHO_SERVICE_URL = "[https://api.ipify.org](https://api.ipify.org)" 
WORKER_ID = str(uuid.uuid4()) # Generate a unique ID for this worker instance

# Global variable to store the Rendezvous Service URL
# For Cloud Run, set this as an environment variable in the service configuration.
# For local testing, it can be loaded from a .env file.
RENDEZVOUS_URL_TEMPLATE = os.environ.get("RENDEZVOUS_URL") # e.g., wss://<your-rendezvous-url>/ws/register
RENDEZVOUS_CONNECTION_URL = f"{RENDEZVOUS_URL_TEMPLATE}/{WORKER_ID}" if RENDEZVOUS_URL_TEMPLATE else None

websocket_connection = None # To hold the active WebSocket connection object
stop_websocket_thread = threading.Event()

async def connect_to_rendezvous():
    """
    Connects to the Rendezvous service via WebSocket and keeps the connection alive.
    """
    global websocket_connection
    if not RENDEZVOUS_CONNECTION_URL:
        print("RENDEZVOUS_URL not set. Cannot connect to Rendezvous service.")
        return

    retry_delay = 5  # seconds
    while not stop_websocket_thread.is_set():
        try:
            print(f"Worker '{WORKER_ID}' attempting to connect to Rendezvous: {RENDEZVOUS_CONNECTION_URL}")
            async with websockets.connect(RENDEZVOUS_CONNECTION_URL) as websocket:
                websocket_connection = websocket # Store the connection
                print(f"Worker '{WORKER_ID}' connected to Rendezvous service.")
                await websocket.send(f"Hello from worker {WORKER_ID}") # Optional: send an initial message

                # Keep connection alive and listen for messages
                while not stop_websocket_thread.is_set():
                    try:
                        # Add a timeout to periodically check stop_websocket_thread
                        message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                        print(f"Worker '{WORKER_ID}' received message from Rendezvous: {message}")
                        # Handle incoming messages from Rendezvous (for future steps)
                    except asyncio.TimeoutError:
                        continue # No message received, continue loop
                    except websockets.exceptions.ConnectionClosed:
                        print(f"Worker '{WORKER_ID}': Rendezvous connection closed.")
                        break # Break inner loop to trigger reconnection
            
        except websockets.exceptions.InvalidURI:
            print(f"Worker '{WORKER_ID}': Invalid Rendezvous URI: {RENDEZVOUS_CONNECTION_URL}. Please check the URL format (e.g. wss://).")
            break # Stop trying if URI is invalid
        except websockets.exceptions.ConnectionClosedError as e:
            print(f"Worker '{WORKER_ID}': Connection to Rendezvous closed unexpectedly: {e}. Retrying in {retry_delay}s...")
        except ConnectionRefusedError:
            print(f"Worker '{WORKER_ID}': Connection to Rendezvous refused. Is the service running? Retrying in {retry_delay}s...")
        except Exception as e:
            print(f"Worker '{WORKER_ID}': Error connecting to Rendezvous - {type(e).__name__}: {e}. Retrying in {retry_delay}s...")
        finally:
            websocket_connection = None # Clear connection on disconnect/error
            if not stop_websocket_thread.is_set():
                 await asyncio.sleep(retry_delay)
                 retry_delay = min(retry_delay * 2, 60) # Exponential backoff up to 60s

def run_websocket_client_in_thread():
    """
    Runs the asyncio event loop for the WebSocket client in a separate thread.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(connect_to_rendezvous())
    finally:
        loop.close()

@app.before_request
def start_websocket_client():
    # This ensures the WebSocket client thread is started when the first request comes in,
    # or you could start it directly when the Flask app initializes if preferred.
    # Using a global flag to ensure it only starts once.
    if not hasattr(app, 'websocket_thread_started') and RENDEZVOUS_CONNECTION_URL:
        print("Initializing WebSocket client thread for Rendezvous connection...")
        stop_websocket_thread.clear()
        thread = threading.Thread(target=run_websocket_client_in_thread, daemon=True)
        thread.start()
        app.websocket_thread_started = True
        print("WebSocket client thread started.")
    elif not RENDEZVOUS_CONNECTION_URL:
         print("RENDEZVOUS_URL not configured. WebSocket client not started.")


@app.route('/')
def get_my_public_ip_and_status():
    """
    Same as Step 1, but also shows Rendezvous connection status.
    """
    nat_ip_info = "Could not retrieve NAT IP."
    try:
        response = requests.get(IP_ECHO_SERVICE_URL, timeout=5)
        response.raise_for_status()
        public_ip = response.text.strip()
        nat_ip_info = f"Observed NAT Public IP (via {IP_ECHO_SERVICE_URL}): {public_ip}"
        print(nat_ip_info)
    except requests.exceptions.RequestException as e:
        nat_ip_info = f"Error getting NAT IP: {e}"
        print(nat_ip_info)

    rendezvous_status = "Rendezvous client not configured (RENDEZVOUS_URL not set)."
    if RENDEZVOUS_CONNECTION_URL:
        if websocket_connection and websocket_connection.open:
            rendezvous_status = f"Connected to Rendezvous as Worker ID '{WORKER_ID}'."
        else:
            rendezvous_status = f"Attempting to connect to Rendezvous as Worker ID '{WORKER_ID}' (currently disconnected)."
    
    return f"Worker ID: {WORKER_ID}<br>{nat_ip_info}<br>{rendezvous_status}", 200

# Teardown logic for Cloud Run (though SIGTERM handling can be complex with threads)
# For a more robust shutdown, consider signal handling if long-running tasks in thread.
# This is a simplified approach.
def on_shutdown():
    print("Worker service shutting down. Stopping WebSocket thread.")
    stop_websocket_thread.set()
    # Give the thread a moment to close connections if possible
    # In a real app, you might join the thread with a timeout.

if __name__ == "__main__":
    if not os.environ.get("K_SERVICE"): # Local execution
        # For local testing, ensure RENDEZVOUS_URL is in your .env file
        # e.g., RENDEZVOUS_URL=ws://localhost:8000/ws/register (if rendezvous is local)
        # or RENDEZVOUS_URL=wss://<your-deployed-rendezvous-url>/ws/register
        if RENDEZVOUS_CONNECTION_URL:
            print(f"Local: Worker '{WORKER_ID}' will attempt to connect to: {RENDEZVOUS_CONNECTION_URL}")
            start_websocket_client() # Start client immediately for local dev
        else:
            print("Local: RENDEZVOUS_URL not found in .env file. WebSocket client not started.")
        
        port = int(os.environ.get("PORT", 8080))
        app.run(host='0.0.0.0', port=port, debug=True)
    else:
        # When on Cloud Run, Gunicorn runs the app.
        # The @app.before_request will trigger the WebSocket client thread.
        # Consider more robust ways to manage background tasks in production Cloud Run.
        pass

# Note: For Cloud Run, background threads started outside of request scope
# need "CPU always allocated" if they must run continuously.
# For this PoC, the thread starts on first request. If the instance idles
# and CPU is not allocated, the WebSocket connection might drop and only
# re-establish on a new incoming request that wakes up the instance.
4. worker_service/Dockerfile(Can be the same as Step 1, just ensure requirements.txt is copied and installed)# Use an official Python runtime as a parent image
FROM python:3.9-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file to the working directory
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code into the container
COPY main.py .

# Expose the port the app runs on
EXPOSE 8080

# Define environment variable
ENV PORT 8080

# Run the web server using Gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "main:app"]
5. Define Shell Variables for Worker Service# PROJECT_ID and REGION should still be set
# WORKER_SERVICE_NAME is 'ip-worker-service' from Step 1
export WORKER_SERVICE_NAME="ip-worker-service" 
export WORKER_AR_REPO_NAME="ip-worker-repo" # From Step 1

# **IMPORTANT**: Set this to the URL of your deployed Rendezvous Service
# Example: export YOUR_RENDEZVOUS_SERVICE_URL="wss://rendezvous-service-abcdef-uc.a.run.app/ws/register"
# Ensure you use wss:// for secure WebSockets if your Rendezvous service is on HTTPS (Cloud Run default)
export YOUR_RENDEZVOUS_SERVICE_URL="YOUR_ACTUAL_DEPLOYED_RENDEZVOUS_SERVICE_URL_ENDING_WITH_/ws/register" 
Replace YOUR_ACTUAL_DEPLOYED_RENDEZVOUS_SERVICE_URL_ENDING_WITH_/ws/register with the actual URL. Make sure it includes the /ws/register path but not the {worker_id} part, as the worker code appends its own ID.6. Rebuild and Redeploy the Worker ServiceBuild and Push (from step2_rendezvous_registration/worker_service/ directory):gcloud builds submit --region=$REGION --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/${WORKER_AR_REPO_NAME}/${WORKER_SERVICE_NAME}:step2
(Using a new tag step2 to differentiate from the Step 1 image)Redeploy to Cloud Run with the new environment variable:gcloud run deploy $WORKER_SERVICE_NAME \
    --project=$PROJECT_ID \
    --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/${WORKER_AR_REPO_NAME}/${WORKER_SERVICE_NAME}:step2 \
    --platform=managed \
    --region=$REGION \
    --allow-unauthenticated \
    --vpc-egress=all-traffic \
    --network=$VPC_NETWORK_NAME \
    --subnet=$SUBNET_NAME \
    --set-env-vars="RENDEZVOUS_URL=${YOUR_RENDEZVOUS_SERVICE_URL}" \
    --cpu-always-allocated # Recommended for persistent WebSocket client
    # --max-instances=2 # Deploy at least two workers to see multiple registrations
--set-env-vars: This is crucial for passing the Rendezvous service URL to the worker.--cpu-always-allocated: Important if you want the WebSocket client in the worker to run continuously in the background. Without it, the Cloud Run instance might idle, and the WebSocket connection could drop until a new HTTP request wakes the instance.Part 3: Test and VerifyCheck Rendezvous Service Logs:Go to Google Cloud Console -> Cloud Run -> Select rendezvous-service.View its logs. You should see messages like:Worker 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx' connected from <NAT_PUBLIC_IP>:<NAT_SOURCE_PORT>. Total workers: 1Current worker details: { ... }The <NAT_PUBLIC_IP> should be one of the IPs of your ip-worker-nat Cloud NAT gateway.The <NAT_SOURCE_PORT> is the ephemeral port used by the NAT for this specific worker's connection.Access Rendezvous Service /peers Endpoint:Open https://<your-rendezvous-service-url>/peers in a browser.You should see a JSON response listing the connected workers and their observed public_ip and public_port.Check Worker Service Logs:Go to Google Cloud Console -> Cloud Run -> Select ip-worker-service.View its logs. You should see messages like:Worker 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx' attempting to connect to Rendezvous: wss://...Worker 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx' connected to Rendezvous service.Access Worker Service Endpoint:Open the URL of your ip-worker-service.The output should now include its Worker ID and status of its connection to the Rendezvous service.Scale Workers (Optional):If you deployed the worker service with --max-instances=2 or more (or manually scale it), you should see multiple distinct worker IDs registering with the Rendezvous service, potentially from the same NAT IP but different source ports.Part 4: Cleanup (Optional)To avoid ongoing charges, delete the created resources:# Delete Worker Service (ip-worker-service)
gcloud run services delete $WORKER_SERVICE_NAME --project=$PROJECT_ID --platform=managed --region=$REGION --quiet

# Delete Rendezvous Service
gcloud run services delete $RENDEZVOUS_SERVICE_NAME --project=$PROJECT_ID --platform=managed --region=$REGION --quiet

# Delete Artifact Registry Repositories (if you created a new one for rendezvous)
# gcloud artifacts repositories delete $WORKER_AR_REPO_NAME --project=$PROJECT_ID --location=$REGION --quiet
# gcloud artifacts repositories delete $RENDEZVOUS_AR_REPO_NAME --project=$PROJECT_ID --location=$REGION --quiet

# Networking resources from Step 1 (Cloud NAT, Router, Subnet, VPC) can be deleted if no longer needed.
# Refer to Step 1 cleanup, but be cautious if other services use them.
# gcloud compute nats delete ip-worker-nat --router=ip-worker-router --project=$PROJECT_ID --region=$REGION --quiet
# gcloud compute routers delete ip-worker-router --project=$PROJECT_ID --region=$REGION --quiet
# gcloud compute networks subnets delete ip-worker-subnet --project=$PROJECT_ID --region=$REGION --quiet
# gcloud compute networks delete ip-worker-vpc --project=$PROJECT_ID --quiet
This step is more involved, but it sets