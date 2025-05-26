## README - Step 3 (Part A): Worker UDP Endpoint Discovery and Registration

### Objective

This part focuses on enabling each Worker service to:

1.  Create and manage a local UDP socket intended for P2P communication, binding it to a configurable `INTERNAL_UDP_PORT`.
2.  Utilize a public STUN (Session Traversal Utilities for NAT) server to discover its own public IP address and port mapping for this UDP socket (as created by the NAT, e.g., Cloud NAT).
3.  Report this discovered public UDP endpoint (`NAT_IP:NAT_UDP_Port`) to the Rendezvous service via the existing WebSocket connection using an `update_udp_endpoint` message.
4.  The Rendezvous service will be updated to store this UDP endpoint information and acknowledge receipt.

This step is crucial for gathering the necessary addressing information for UDP hole punching.

### Prerequisites

1.  **Completion of Step 2:** Your Rendezvous Service and Worker Service (with WebSocket registration and HTTP IP reporting) should be deployed and working correctly as per `plann_step2.md`.
2.  **Project Setup:** `gcloud` configured for `iceberg-eli`, `us-central1` region. If deploying to Cloud Run and expecting NAT, relevant networking (VPC, Subnet, Cloud NAT) should be in place.
3.  **Understanding STUN:** STUN helps clients discover their public IP and NAT mapping. We'll use a server like Google's (`stun.l.google.com:19302`) and the `pystun3` library.

----

## Part 3A.1: Modifications to Rendezvous Service

The Rendezvous service is updated to accept and store STUN-discovered UDP endpoints.

### 3A.1.1. Shell Variables (Rendezvous Service)

```bash
export PROJECT_ID=$(gcloud config get-value project)
export REGION="us-central1"

export AR_RENDEZVOUS_REPO_NAME="rendezvous-repo" 
export RENDEZVOUS_SERVICE_NAME="rendezvous-service"
export RENDEZVOUS_IMAGE_TAG_STEP3A="v_step3a"
```

### 3A.1.2. Rendezvous Service Code (`rendezvous_service_code/main.py`) Updates (for Step 3A)

```python
import asyncio
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from typing import Dict, Any, Optional # Added Optional
import os
import json

app = FastAPI(title="Rendezvous Service - Step 3A")

# Updated storage for Step 3A
# Format: {worker_id: {"websocket_observed_ip": str, 
#                       "websocket_observed_port": int,
#                       "http_reported_public_ip": Optional[str],
#                       "stun_reported_udp_ip": Optional[str],
#                       "stun_reported_udp_port": Optional[int],
#                       "websocket": WebSocket}}
connected_workers: Dict[str, Dict[str, Any]] = {}

@app.websocket("/ws/register/{worker_id}")
async def websocket_register_worker(websocket: WebSocket, worker_id: str):
    await websocket.accept()
    client_host = websocket.client.host
    client_port = websocket.client.port
    
    print(f"Worker '{worker_id}' connecting from WebSocket: {client_host}:{client_port}")

    if worker_id in connected_workers:
        print(f"Worker '{worker_id}' re-connecting. Closing old session if active.")
        old_ws = connected_workers[worker_id].get("websocket")
        if old_ws and hasattr(old_ws, 'client_state') and old_ws.client_state.value == 1:
             try: await old_ws.close(code=1000, reason="New connection from same worker ID")
             except Exception: pass 

    connected_workers[worker_id] = {
        "websocket_observed_ip": client_host, 
        "websocket_observed_port": client_port, 
        "http_reported_public_ip": None,
        "stun_reported_udp_ip": None, # New field for STUN IP
        "stun_reported_udp_port": None, # New field for STUN Port
        "websocket": websocket
    }
    print(f"Worker '{worker_id}' registered. WS Endpoint: {client_host}:{client_port}. Total: {len(connected_workers)}")

    try:
        while True: 
            raw_data = await websocket.receive_text()
            print(f"Rendezvous: Received from '{worker_id}': {raw_data}")
            try:
                message = json.loads(raw_data)
                msg_type = message.get("type")

                if msg_type == "register_public_ip":
                    new_ip = message.get("ip")
                    if new_ip and worker_id in connected_workers:
                        connected_workers[worker_id]["http_reported_public_ip"] = new_ip
                        print(f"Worker '{worker_id}' reported HTTP-based public IP: {new_ip}")
                
                elif msg_type == "update_udp_endpoint": # New message type for STUN info
                    udp_ip = message.get("udp_ip")
                    udp_port = message.get("udp_port")
                    if udp_ip and udp_port is not None and worker_id in connected_workers:
                        connected_workers[worker_id]["stun_reported_udp_ip"] = udp_ip
                        connected_workers[worker_id]["stun_reported_udp_port"] = int(udp_port) 
                        print(f"Worker '{worker_id}' updated STUN UDP endpoint to: {udp_ip}:{udp_port}")
                        await websocket.send_text(json.dumps({"type": "udp_endpoint_ack", "status": "success"}))
                    else:
                        print(f"Worker '{worker_id}' sent incomplete 'update_udp_endpoint'. IP: {udp_ip}, Port: {udp_port}")
                        await websocket.send_text(json.dumps({"type": "udp_endpoint_ack", "status": "error", "detail": "Missing UDP IP or Port"}))
                
                elif msg_type == "echo_request":
                    payload = message.get("payload", "")
                    await websocket.send_text(json.dumps({
                        "type": "echo_response", "original_payload": payload,
                        "processed_by_rendezvous": f"Rendezvous processed echo for {worker_id}"
                    }))
                else:
                    print(f"Worker '{worker_id}' sent unhandled message type: {msg_type}")

            except json.JSONDecodeError:
                print(f"Worker '{worker_id}' sent non-JSON: {raw_data}")
            except KeyError:
                 print(f"Worker '{worker_id}' no longer in dict during message processing.")
            except Exception as e_proc:
                print(f"Error processing message from '{worker_id}': {type(e_proc).__name__} - {e_proc}")

    except WebSocketDisconnect:
        print(f"Worker '{worker_id}' disconnected from {client_host}:{client_port}.")
    except Exception as e_ws:
        print(f"Error with '{worker_id}' WebSocket: {type(e_ws).__name__} - {e_ws}")
    finally:
        if worker_id in connected_workers and connected_workers[worker_id].get("websocket") == websocket:
            del connected_workers[worker_id]
            print(f"Worker '{worker_id}' de-registered. Total: {len(connected_workers)}")

@app.get("/")
async def read_root():
    return {"message": "Rendezvous Service (Step 3A Version) is running."}

@app.get("/debug/list_workers")
async def list_workers_debug():
    workers_info = {}
    for w_id, data in list(connected_workers.items()): 
        ws_object = data.get("websocket")
        is_connected = False
        if ws_object and hasattr(ws_object, 'client_state'):
            is_connected = (ws_object.client_state.value == 1)
        
        workers_info[w_id] = {
            "websocket_observed_ip": data.get("websocket_observed_ip"),
            "websocket_observed_port": data.get("websocket_observed_port"),
            "http_reported_public_ip": data.get("http_reported_public_ip"),
            "stun_reported_udp_ip": data.get("stun_reported_udp_ip"),
            "stun_reported_udp_port": data.get("stun_reported_udp_port"),
            "websocket_connected": is_connected
        }
    return {"connected_workers_count": len(workers_info), "workers": workers_info}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
```

### 3A.1.3. Rendezvous Service `requirements.txt` and `Dockerfile.rendezvous`

No changes are needed to these files from Step 2 (`plann_step2.md`). `fastapi` and `uvicorn` are sufficient.

### 3A.1.4. Build and Re-deploy the Rendezvous Service (Step 3A Version)

```bash
# Build (from rendezvous_service_code dir)
gcloud builds submit --region=$REGION \\
    --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:${RENDEZVOUS_IMAGE_TAG_STEP3A} . \\
    --dockerfile=Dockerfile.rendezvous

# Deploy
gcloud run deploy $RENDEZVOUS_SERVICE_NAME \\
    --project=$PROJECT_ID \\
    --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:${RENDEZVOUS_IMAGE_TAG_STEP3A} \\
    --platform=managed --region=$REGION --allow-unauthenticated --session-affinity --min-instances=1
```

----

## Part 3A.2: Modifications to Worker Service

The Worker service adds STUN logic and reports its UDP endpoint.

### 3A.2.1. Shell Variables (Worker Service)

```bash
export PROJECT_ID=$(gcloud config get-value project)
export REGION="us-central1"
export AR_WORKER_REPO_NAME="ip-worker-repo" 
export WORKER_SERVICE_NAME="ip-worker-service" 
export WORKER_IMAGE_TAG_STEP3A="v_step3a"

# Ensure RENDEZVOUS_SERVICE_URL is set (from Step 2 deployment)
# export RENDEZVOUS_SERVICE_URL="YOUR_RENDEZVOUS_SERVICE_URL_HERE"

# STUN and UDP port settings
export STUN_HOST_ENV="stun.l.google.com"
export STUN_PORT_ENV="19302"
export INTERNAL_UDP_PORT_ENV="8081"
```
**ACTION: Ensure `RENDEZVOUS_SERVICE_URL`, `STUN_HOST_ENV`, `STUN_PORT_ENV`, and `INTERNAL_UDP_PORT_ENV` are correctly set before deploying.**

### 3A.2.2. Worker Service Code (`main.py`) Updates (for Step 3A)

```python
import asyncio
import os
import uuid
import websockets
import signal
import json
import requests
import socket
import stun # pystun3
from typing import Optional

# Global variables for Step 3A
worker_id = str(uuid.uuid4())
stop_signal_received = False

# Network-related globals for P2P and STUN
our_stun_discovered_udp_ip: Optional[str] = None
our_stun_discovered_udp_port: Optional[int] = None

# Configuration from environment variables
DEFAULT_STUN_HOST = os.environ.get("STUN_HOST_ENV", "stun.l.google.com")
DEFAULT_STUN_PORT = int(os.environ.get("STUN_PORT_ENV", "19302"))
INTERNAL_UDP_PORT = int(os.environ.get("INTERNAL_UDP_PORT_ENV", "8081"))

# Minimal threaded health check server (from Step 2)
import http.server
import socketserver
import threading
HTTP_PORT_FOR_HEALTH = int(os.environ.get("PORT", 8080))

def start_minimal_health_check_server(): # Same as plann_step2.md
    class HealthCheckHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header("Content-type", "text/plain"); self.end_headers()
            self.wfile.write(b"Worker active - health check OK")
        def log_message(self, format, *args): return
    httpd = socketserver.TCPServer(("", HTTP_PORT_FOR_HEALTH), HealthCheckHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"Worker '{worker_id}': Health check server on port {HTTP_PORT_FOR_HEALTH}")

def handle_shutdown_signal(signum, frame):
    global stop_signal_received
    print(f"Worker '{worker_id}': Shutdown signal ({signum}) received.")
    stop_signal_received = True

async def discover_and_report_stun_udp_endpoint(websocket_conn_to_rendezvous):
    global our_stun_discovered_udp_ip, our_stun_discovered_udp_port, worker_id, INTERNAL_UDP_PORT
    
    stun_host_to_use = os.environ.get("STUN_HOST_ENV", DEFAULT_STUN_HOST)
    stun_port_to_use = int(os.environ.get("STUN_PORT_ENV", DEFAULT_STUN_PORT))
    
    # For STUN discovery, pystun3's get_ip_info needs a local (source_ip, source_port)
    # to determine the mapping *for that specific local port*.
    # We use "0.0.0.0" for source_ip to let the OS pick the outbound IP.
    # The source_port *must* be the port our actual UDP listener will use (INTERNAL_UDP_PORT).
    print(f"Worker '{worker_id}': Attempting STUN discovery for local port {INTERNAL_UDP_PORT} via STUN server {stun_host_to_use}:{stun_port_to_use}.")
    
    # STUN discovery can fail (e.g., restrictive NAT, STUN server down, network issues).
    # Adding a simple retry mechanism (can be enhanced in future steps).
    # Your actual code has STUN_MAX_RETRIES and STUN_RETRY_DELAY_SEC.
    # This plan simplifies to one attempt for brevity for Step 3A initial plan.
    try:
        nat_type, external_ip, external_port = stun.get_ip_info(
            source_ip="0.0.0.0",
            source_port=INTERNAL_UDP_PORT, # Crucial: Use the port we intend to listen on for P2P
            stun_host=stun_host_to_use,
            stun_port=stun_port_to_use
        )
        print(f"Worker '{worker_id}': STUN Result: NAT='{nat_type}', External IP='{external_ip}', External Port={external_port} (for internal port {INTERNAL_UDP_PORT})")

        if external_ip and external_port is not None:
            our_stun_discovered_udp_ip = external_ip
            our_stun_discovered_udp_port = external_port
            
            await websocket_conn_to_rendezvous.send(json.dumps({
                "type": "update_udp_endpoint",
                "udp_ip": our_stun_discovered_udp_ip,
                "udp_port": our_stun_discovered_udp_port
            }))
            print(f"Worker '{worker_id}': Sent STUN-discovered UDP endpoint ({our_stun_discovered_udp_ip}:{our_stun_discovered_udp_port}) to Rendezvous.")
            return True # STUN success
        else:
            print(f"Worker '{worker_id}': STUN discovery failed to return a valid external IP/Port.")
            return False # STUN failed

    except stun.StunException as e_stun:
        print(f"Worker '{worker_id}': STUN protocol error: {type(e_stun).__name__} - {e_stun}")
    except socket.gaierror as e_gaierror: 
        print(f"Worker '{worker_id}': STUN host DNS resolution error ('{stun_host_to_use}'): {e_gaierror}")
    except OSError as e_os: # e.g. Address already in use if INTERNAL_UDP_PORT is held, or network unreachable
        print(f"Worker '{worker_id}': OS error during STUN (e.g., socket bind/issue for port {INTERNAL_UDP_PORT}): {type(e_os).__name__} - {e_os}")
    except Exception as e_stun_general:
        print(f"Worker '{worker_id}': General error during STUN discovery: {type(e_stun_general).__name__} - {e_stun_general}")
    
    return False # STUN failed if any exception occurred
    
async def placeholder_udp_listener_task():
    """Placeholder: In Step 3B, this becomes a real asyncio DatagramProtocol listener."""
    global stop_signal_received, worker_id, INTERNAL_UDP_PORT
    print(f"Worker '{worker_id}': Placeholder UDP listener task started for port {INTERNAL_UDP_PORT}. Would listen here.")
    # Simulate being active
    while not stop_signal_received:
        await asyncio.sleep(30) # Check periodically
        if not stop_signal_received:
            print(f"Worker '{worker_id}': Placeholder UDP listener still 'active' on port {INTERNAL_UDP_PORT}.")
    print(f"Worker '{worker_id}': Placeholder UDP listener task stopped.")

async def connect_to_rendezvous(rendezvous_ws_url_base: str):
    global stop_signal_received, worker_id, our_stun_discovered_udp_ip, our_stun_discovered_udp_port
    
    full_rendezvous_ws_url = f"{rendezvous_ws_url_base}/ws/register/{worker_id}"
    print(f"Worker '{worker_id}': Attempting Rendezvous connection: {full_rendezvous_ws_url}")
    
    ip_echo_service_url = "https://api.ipify.org"
    ping_interval = float(os.environ.get("PING_INTERVAL_SEC", "25"))
    ping_timeout = float(os.environ.get("PING_TIMEOUT_SEC", "25"))
    udp_listener_handle = None # For the placeholder task

    while not stop_signal_received:
        try:
            async with websockets.connect(
                full_rendezvous_ws_url,
                ping_interval=ping_interval,
                ping_timeout=ping_timeout
            ) as websocket:
                print(f"Worker '{worker_id}': Connected to Rendezvous.")

                # Report HTTP-based IP (from Step 2)
                try:
                    response = requests.get(ip_echo_service_url, timeout=10)
                    response.raise_for_status(); http_public_ip = response.text.strip()
                    await websocket.send(json.dumps({"type": "register_public_ip", "ip": http_public_ip}))
                    print(f"Worker '{worker_id}': Sent HTTP-based IP ({http_public_ip}) to Rendezvous.")
                except Exception as e_http_ip: print(f"Worker '{worker_id}': Error with HTTP IP: {e_http_ip}")

                # New for Step 3A: Discover and report STUN UDP endpoint
                stun_success = await discover_and_report_stun_udp_endpoint(websocket)

                if stun_success and not udp_listener_handle: # Start placeholder UDP listener if STUN worked
                    # In a real scenario, the UDP socket for STUN (if get_ip_info used it directly)
                    # would be the one we listen on. Here INTERNAL_UDP_PORT is key.
                    udp_listener_handle = asyncio.create_task(placeholder_udp_listener_task())
                
                # Main WebSocket receive loop
                while not stop_signal_received:
                    try:
                        message_raw = await asyncio.wait_for(websocket.recv(), timeout=ping_interval + 10)
                        print(f"Worker '{worker_id}': Received from Rendezvous: {message_raw}")
                        message_data = json.loads(message_raw)
                        msg_type = message_data.get("type")

                        if msg_type == "udp_endpoint_ack":
                            print(f"Worker '{worker_id}': Received UDP endpoint ack from Rendezvous: Status '{message_data.get('status')}'")
                        elif msg_type == "echo_response": 
                            print(f"Worker '{worker_id}': Echo Response: {message_data.get('processed_by_rendezvous')}")
                        # No p2p_connection_offer handling in Step 3A
                        else:
                            print(f"Worker '{worker_id}': Unhandled message type from Rendezvous: {msg_type}")
                    except asyncio.TimeoutError: pass 
                    except websockets.exceptions.ConnectionClosed:
                        print(f"Worker '{worker_id}': Rendezvous WS closed by server."); break 
                    except Exception as e_recv_loop:
                        print(f"Worker '{worker_id}': Error in WS recv loop: {e_recv_loop}"); break
                
                if stop_signal_received: break
            
        except Exception as e_connect: # Broadly catch connection issues
            print(f"Worker '{worker_id}': Error with Rendezvous WS connection ({full_rendezvous_ws_url}): {type(e_connect).__name__} - {e_connect}.")
        
        finally: # Cleanup for this connection attempt cycle
            if udp_listener_handle and not udp_listener_handle.done():
                udp_listener_handle.cancel()
                try: await udp_listener_handle
                except asyncio.CancelledError: print(f"Worker '{worker_id}': Placeholder UDP listener task cancelled.")
            udp_listener_handle = None # Reset for next attempt
        
        if not stop_signal_received:
            print(f"Worker '{worker_id}': Retrying Rendezvous connection in 10s...")
            await asyncio.sleep(10) 
        else: break

    print(f"Worker '{worker_id}': Stopped Rendezvous connection attempts.")

if __name__ == "__main__":
    print(f"WORKER SCRIPT (ID: {worker_id}): Initializing for Step 3A...")
    start_minimal_health_check_server() # For Cloud Run health checks

    rendezvous_base_url = os.environ.get("RENDEZVOUS_SERVICE_URL")
    if not rendezvous_base_url: exit(f"CRITICAL: Worker '{worker_id}': RENDEZVOUS_SERVICE_URL not set.")

    ws_scheme = "wss" if rendezvous_base_url.startswith("https://") else "ws"
    rendezvous_ws_url_base_constructed = f"{ws_scheme}://{rendezvous_base_url.replace('https://', '').replace('http://', '')}"
    
    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)

    try: asyncio.run(connect_to_rendezvous(rendezvous_ws_url_base_constructed))
    except KeyboardInterrupt: print(f"Worker '{worker_id}': Interrupted by user.")
    except Exception as e_main: print(f"Worker '{worker_id}': CRITICAL in main: {type(e_main).__name__} - {e_main}")
    finally: print(f"Worker '{worker_id}': Main process finished.")
```

### 3A.2.3. Worker Service `requirements.txt` (`holepunch/requirements.txt` - for Step 3A)

```
websockets>=12.0
requests>=2.30.0 
pystun3>=1.1.6 # Added for STUN. Ensure this or a compatible version.
```

### 3A.2.4. Worker Service `Dockerfile.worker` (`holepunch/Dockerfile.worker` - for Step 3A)

No change from Step 2's Dockerfile typically needed, just ensure `requirements.txt` is copied and installed.
```dockerfile
FROM python:3.9-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
ENV PORT 8080 
CMD ["python", "-u", "main.py"]
```

### 3A.2.5. Build and Re-deploy the Worker Service (Step 3A Version)

```bash
# Build (from project root)
gcloud builds submit --region=$REGION \\
    --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG_STEP3A} . \\
    --dockerfile=Dockerfile.worker

# Deploy
# Ensure RENDEZVOUS_SERVICE_URL, STUN_HOST_ENV, STUN_PORT_ENV, INTERNAL_UDP_PORT_ENV are set
gcloud run deploy $WORKER_SERVICE_NAME \\
    --project=$PROJECT_ID \\
    --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG_STEP3A} \\
    --platform=managed --region=$REGION \\
    --set-env-vars="RENDEZVOUS_SERVICE_URL=${RENDEZVOUS_SERVICE_URL},STUN_HOST_ENV=${STUN_HOST_ENV},STUN_PORT_ENV=${STUN_PORT_ENV},INTERNAL_UDP_PORT_ENV=${INTERNAL_UDP_PORT_ENV}" \\
    # --vpc-egress=all-traffic --network=... --subnet=... # If needed for NAT
    --min-instances=1
```

----

## Part 3A.3: Verification (for Step 3A)

1.  **Deploy/Update both services** (Rendezvous first, then Worker with new env vars).
2.  **Check Worker Logs:**
      * Health check server startup.
      * STUN discovery attempt: `Worker '...' Attempting STUN discovery for local port {INTERNAL_UDP_PORT} ...`
      * STUN result: `Worker '...' STUN Result: NAT Type=..., External IP=X.X.X.X, External Port=Y ...`
      * Message sent to Rendezvous: `Worker '...' Sent STUN-discovered UDP endpoint (X.X.X.X:Y) to Rendezvous.`
      * Ack received: `Worker '...' Received UDP endpoint ack from Rendezvous: Status 'success'`
      * Placeholder UDP listener: `Worker '...' Placeholder UDP listener task started...`
3.  **Check Rendezvous Logs:**
      * Worker WebSocket connection, HTTP IP registration (from Step 2 logic).
      * Receipt of `update_udp_endpoint` message from worker.
      * Log showing UDP IP/port update: `Worker '...' updated STUN UDP endpoint to: X.X.X.X:Y`
      * Sent `udp_endpoint_ack` back to worker.
4.  **Use Rendezvous Debug Endpoint (`/debug/list_workers`):**
      * Verify `stun_reported_udp_ip` and `stun_reported_udp_port` are populated for the worker.
      * `websocket_connected` should be `true`.

This confirms workers can discover and register their public UDP endpoints. Step 3B will use this for P2P attempts. 