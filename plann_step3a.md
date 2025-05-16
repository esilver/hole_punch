## README - Step 3 (Part A): Worker UDP Endpoint Discovery and Registration

### Objective

This part focuses on enabling each Worker service to:

1.  Create and manage a local UDP socket.
2.  Utilize a public STUN (Session Traversal Utilities for NAT) server to discover its own public IP address and port mapping for this UDP socket, as created by the Cloud NAT gateway.
3.  Report this discovered public UDP endpoint (`NAT_IP:NAT_UDP_Port`) to the Rendezvous service via the existing WebSocket connection.
4.  The Rendezvous service will be updated to store this UDP endpoint information alongside the existing worker details.

This step is crucial for gathering the necessary addressing information for actual UDP hole punching between peers, which will be covered in a subsequent part.

### Prerequisites

1.  **Completion of Step 2:** Your Rendezvous Service and modified Worker Service (with WebSocket registration and health checks) should be deployed and working correctly. Workers should be registering with the Rendezvous service.
2.  **Project Setup:** `gcloud` configured for `iceberg-eli`, `us-central1` region, APIs enabled, and existing networking (VPC, Subnet, Cloud Router, Cloud NAT from Step 1) should be in place.
3.  **Understanding STUN:** Briefly, STUN helps clients discover their public IP address and the type of NAT they are behind, without requiring any special logic on the NAT device itself. We'll use a public STUN server like Google's (`stun.l.google.com:19302`).

-----

## Part 3A.1: Modifications to Rendezvous Service

The Rendezvous service needs to be updated to accept and store the UDP endpoint information from the workers.

### 3A.1.1. Shell Variables (Rendezvous Service - mostly existing)

```bash
# Dynamically get Project ID and set Region (example for us-central1)
export PROJECT_ID=$(gcloud config get-value project)
export REGION="us-central1"

export AR_RENDEZVOUS_REPO_NAME="rendezvous-repo" 
export RENDEZVOUS_SERVICE_NAME="rendezvous-service"
export RENDEZVOUS_IMAGE_TAG_V2="v_step3a" # New version tag for this step
```

### 3A.1.2. Rendezvous Service Code (`rendezvous_service_code/main.py`) Updates

```python
import asyncio
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from typing import Dict, Tuple, Optional # Added Optional
import os
import json

app = FastAPI(title="Rendezvous Service")

# Updated storage for connected workers to include UDP endpoint info
# Format: {worker_id: {
#    "public_ip": str, "public_port": int, # From WebSocket connection (or self-reported HTTP IP)
#    "websocket": WebSocket,
#    "udp_ip": Optional[str], "udp_port": Optional[int] # Discovered via STUN by worker
# }}
connected_workers: Dict[str, Dict] = {}

@app.websocket("/ws/register/{worker_id}")
async def websocket_register_worker(websocket: WebSocket, worker_id: str):
    print(f"RENDEZVOUS_DEBUG: Attempting to register worker '{worker_id}'. Scope: {websocket.scope}") # Early debug
    await websocket.accept()
    client_host = websocket.client.host # IP from WebSocket connection
    client_port = websocket.client.port # Port from WebSocket connection
    
    print(f"Worker '{worker_id}' connecting from WebSocket endpoint: {client_host}:{client_port}")

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
        "websocket": websocket,
        "udp_ip": None, 
        "udp_port": None
    }
    print(f"Worker '{worker_id}' registered. WebSocket EP: {client_host}:{client_port}. Total: {len(connected_workers)}")

    try:
        while True: 
            raw_data = await websocket.receive_text()
            print(f"Received raw message from '{worker_id}': {raw_data}")
            try:
                message = json.loads(raw_data)
                msg_type = message.get("type")
                print(f"RENDEZVOUS_DEBUG: Parsed msg_type: {repr(msg_type)}")

                if msg_type == "register_public_ip":
                    new_ip = message.get("ip")
                    if new_ip and worker_id in connected_workers:
                        print(f"Worker '{worker_id}' self-reported (HTTP-based) public IP: {new_ip}. Storing as 'public_ip'.")
                        connected_workers[worker_id]["public_ip"] = new_ip 
                    elif not new_ip:
                        print(f"Worker '{worker_id}' sent register_public_ip message without an IP.")
                
                elif msg_type == "update_udp_endpoint":
                    udp_ip = message.get("udp_ip")
                    udp_port = message.get("udp_port")
                    if udp_ip and udp_port and worker_id in connected_workers:
                        connected_workers[worker_id]["udp_ip"] = udp_ip
                        connected_workers[worker_id]["udp_port"] = int(udp_port) 
                        print(f"Worker '{worker_id}' updated UDP endpoint to: {udp_ip}:{udp_port}")
                        await websocket.send_text(json.dumps({"type": "udp_endpoint_ack", "status": "success"}))
                    else:
                        print(f"Worker '{worker_id}' sent incomplete update_udp_endpoint message.")
                        await websocket.send_text(json.dumps({"type": "udp_endpoint_ack", "status": "error", "detail": "Missing IP or Port"}))
                
                elif msg_type == "echo_request":
                    payload = message.get("payload", "")
                    response_payload_dict = {
                        "type": "echo_response",
                        "original_payload": payload,
                        "processed_by_rendezvous": f"Rendezvous processed: '{payload.upper()}' for worker {worker_id}"
                    }
                    await websocket.send_text(json.dumps(response_payload_dict))
                    print(f"Sent echo_response back to worker '{worker_id}'")
                
                elif msg_type == "get_my_details":
                    if worker_id in connected_workers:
                        details = connected_workers[worker_id]
                        response_payload_dict = {
                            "type": "my_details_response",
                            "worker_id": worker_id,
                            "registered_ip": details["public_ip"],
                            "registered_port": details["public_port"],
                            "udp_ip": details.get("udp_ip"), # Include UDP info if available
                            "udp_port": details.get("udp_port"),
                            "message": "These are your details as seen by the Rendezvous service."
                        }
                        await websocket.send_text(json.dumps(response_payload_dict))
                        print(f"Sent 'my_details_response' back to worker '{worker_id}'")
                    else:
                        await websocket.send_text(json.dumps({"type": "error", "message": "Could not find your details."}))

                else:
                    print(f"Worker '{worker_id}' sent unhandled message type: {msg_type}")

            except json.JSONDecodeError:
                print(f"Worker '{worker_id}' sent non-JSON message: {raw_data}")
            except AttributeError: 
                print(f"Worker '{worker_id}' sent malformed JSON message: {raw_data}")
            except KeyError:
                 print(f"Worker '{worker_id}' no longer in connected_workers dictionary, could not process message.")

    except WebSocketDisconnect:
        print(f"Worker '{worker_id}' disconnected from WebSocket EP: {client_host}:{client_port}.")
    except Exception as e:
        print(f"Error with worker '{worker_id}' WebSocket: {e}")
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
    for worker_id_key, data_val in list(connected_workers.items()):
        ws_object = data_val.get("websocket")
        current_client_state_value = None
        is_connected = False
        if ws_object and hasattr(ws_object, 'client_state'):
            current_client_state = ws_object.client_state
            current_client_state_value = current_client_state.value
            is_connected = (current_client_state_value == 1) 
        
        workers_info[worker_id_key] = {
            "websocket_observed_ip": data_val.get("public_ip"),
            "websocket_observed_port": data_val.get("public_port"),
            "stun_reported_udp_ip": data_val.get("udp_ip"),
            "stun_reported_udp_port": data_val.get("udp_port"),
            "websocket_connected": is_connected,
            "websocket_raw_state": current_client_state_value
        }
    return {"connected_workers_count": len(workers_info), "workers": workers_info}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
```

### 3A.1.3. Rendezvous Service `requirements.txt` and `Dockerfile.rendezvous`

No changes are needed to these files from Step 2.

### 3A.1.4. Build and Re-deploy the Rendezvous Service

1.  **Build Image:**
    Use your preferred method (Cloud Build or local Docker). If using local Docker on non-amd64, use `docker buildx`:
    ```bash
    # From repo root: 
    # cd rendezvous_service_code # if commands are run from here
    docker buildx build --platform linux/amd64 -f rendezvous_service_code/Dockerfile.rendezvous -t gcr.io/$PROJECT_ID/$AR_RENDEZVOUS_REPO_NAME/$RENDEZVOUS_SERVICE_NAME:${RENDEZVOUS_IMAGE_TAG_V2} ./rendezvous_service_code --load
    docker push gcr.io/$PROJECT_ID/$AR_RENDEZVOUS_REPO_NAME/$RENDEZVOUS_SERVICE_NAME:${RENDEZVOUS_IMAGE_TAG_V2}
    ```

2.  **Re-deploy Rendezvous Service:**
    ```bash
    gcloud run deploy $RENDEZVOUS_SERVICE_NAME \
        --project=$PROJECT_ID \
        --image=gcr.io/$PROJECT_ID/$AR_RENDEZVOUS_REPO_NAME/$RENDEZVOUS_SERVICE_NAME:${RENDEZVOUS_IMAGE_TAG_V2} \
        --platform=managed \
        --region=$REGION \
        --allow-unauthenticated \
        --session-affinity \
        --min-instances=1 # Recommended to keep it warm
    ```

-----

## Part 3A.2: Modifications to Worker Service

The Worker service will now include logic to create a UDP socket, query a STUN server, and report the discovered UDP endpoint.

### 3A.2.1. Shell Variables (Worker Service - mostly existing)

```bash
# Ensure PROJECT_ID and REGION are set (as in Part 1.1)
export AR_WORKER_REPO_NAME="ip-worker-repo" 
export WORKER_SERVICE_NAME="ip-worker-service" 
export WORKER_IMAGE_TAG_V3="v_step3a" # New version tag for this step

# Ensure RENDEZVOUS_SERVICE_URL is set, e.g.:
# export RENDEZVOUS_SERVICE_URL=$(gcloud run services describe $RENDEZVOUS_SERVICE_NAME --platform managed --region $REGION --project $PROJECT_ID --format 'value(status.url)')
if [ -z "$RENDEZVOUS_SERVICE_URL" ]; then echo "Error: RENDEZVOUS_SERVICE_URL is not set."; fi
```

### 3A.2.2. Worker Service Code (`holepunch/main.py`) Updates

```python
import asyncio
import os
import uuid
import websockets
import signal
import threading
import http.server
import socketserver
import requests
import json
import socket
import stun # from pystun3
from typing import Optional

worker_id = str(uuid.uuid4())
stop_signal_received = False
udp_socket: Optional[socket.socket] = None 
discovered_udp_ip: Optional[str] = None
discovered_udp_port: Optional[int] = None

DEFAULT_STUN_HOST = "stun.l.google.com"
DEFAULT_STUN_PORT = 19302

def handle_shutdown_signal(signum, frame):
    global stop_signal_received
    print(f"Shutdown signal ({signum}) received. Worker '{worker_id}' attempting graceful shutdown.")
    stop_signal_received = True
    if udp_socket:
        try:
            udp_socket.close()
            print(f"Worker '{worker_id}': UDP socket closed.")
        except Exception as e:
            print(f"Worker '{worker_id}': Error closing UDP socket: {e}")

async def discover_udp_endpoint_and_report(websocket_conn):
    global udp_socket, discovered_udp_ip, discovered_udp_port
    
    if udp_socket is None:
        udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            udp_socket.bind(("0.0.0.0", 0))
            source_ip, source_port = udp_socket.getsockname()
            print(f"Worker '{worker_id}': UDP socket created and bound to local {source_ip}:{source_port}")
        except Exception as e:
            print(f"Worker '{worker_id}': Error binding UDP socket: {e}. Cannot perform STUN discovery.")
            if udp_socket:
                udp_socket.close()
                udp_socket = None
            return

    stun_host = os.environ.get("STUN_HOST", DEFAULT_STUN_HOST)
    stun_port = int(os.environ.get("STUN_PORT", DEFAULT_STUN_PORT))

    print(f"Worker '{worker_id}': Our main UDP socket is bound to local {udp_socket.getsockname()[0]}:{udp_socket.getsockname()[1]}")
    print(f"Worker '{worker_id}': Attempting STUN discovery to {stun_host}:{stun_port} (pystun3 will use its own ephemeral client port)")
    
    try:
        nat_type, external_ip, external_port = stun.get_ip_info(
            stun_host=stun_host, 
            stun_port=stun_port
        )
        print(f"Worker '{worker_id}': STUN discovery result: NAT Type={nat_type}, External IP={external_ip}, External Port={external_port}")

        if external_ip and external_port:
            discovered_udp_ip = external_ip
            discovered_udp_port = external_port
            
            udp_endpoint_message = {
                "type": "update_udp_endpoint",
                "udp_ip": discovered_udp_ip,
                "udp_port": discovered_udp_port
            }
            await websocket_conn.send(json.dumps(udp_endpoint_message))
            print(f"Worker '{worker_id}': Sent discovered UDP endpoint ({discovered_udp_ip}:{discovered_udp_port}) to Rendezvous.")
        else:
            print(f"Worker '{worker_id}': STUN discovery failed to return external IP/Port.")

    except stun.StunError as e: # Catch specific pystun3 error
        print(f"Worker '{worker_id}': STUN error: {e}")
    except socket.gaierror as e: 
        print(f"Worker '{worker_id}': STUN host DNS resolution error: {e}")
    except Exception as e:
        print(f"Worker '{worker_id}': Error during STUN discovery: {type(e).__name__} - {e}")
    
async def udp_listener_task():
    global udp_socket, stop_signal_received
    if not udp_socket:
        print(f"Worker '{worker_id}': UDP socket not initialized. UDP listener cannot start.")
        return

    local_ip, local_port = udp_socket.getsockname()
    print(f"Worker '{worker_id}': Starting UDP listener on {local_ip}:{local_port}... (placeholder)")
    try:
        while not stop_signal_received:
            await asyncio.sleep(60) 
            if not stop_signal_received:
                 print(f"Worker '{worker_id}': UDP socket still open on local {local_ip}:{local_port} (placeholder listener active)")
    except asyncio.CancelledError:
        print(f"Worker '{worker_id}': UDP listener task cancelled.")
    except Exception as e:
        if not stop_signal_received: 
            print(f"Worker '{worker_id}': Error in placeholder UDP listener loop: {e}")
    finally:
        print(f"Worker '{worker_id}': UDP listener task stopped.")

async def connect_to_rendezvous(rendezvous_ws_url: str):
    global stop_signal_received, discovered_udp_ip, discovered_udp_port
    print(f"WORKER CONNECT_TO_RENDEZVOUS: Entered function for URL: {rendezvous_ws_url}. stop_signal_received={stop_signal_received}")
    print(f"Worker '{worker_id}' attempting to connect to Rendezvous: {rendezvous_ws_url}")
    
    ip_echo_service_url = "https://api.ipify.org" 
    ping_interval = float(os.environ.get("PING_INTERVAL_SEC", "25")) 
    ping_timeout = float(os.environ.get("PING_TIMEOUT_SEC", "25")) 

    while not stop_signal_received:
        try:
            async with websockets.connect(
                rendezvous_ws_url,
                ping_interval=ping_interval,
                ping_timeout=ping_timeout,
            ) as websocket:
                print(f"WORKER '{worker_id}' connected to Rendezvous. Type: {type(websocket)}, Dir: {dir(websocket)}")
                print(f"Worker '{worker_id}' connected to Rendezvous via WebSocket.")

                try:
                    print(f"Worker '{worker_id}' fetching its HTTP-based public IP from {ip_echo_service_url}...")
                    response = requests.get(ip_echo_service_url, timeout=10)
                    response.raise_for_status()
                    http_public_ip = response.text.strip()
                    print(f"Worker '{worker_id}' identified HTTP-based public IP as: {http_public_ip}")
                    await websocket.send(json.dumps({"type": "register_public_ip", "ip": http_public_ip}))
                    print(f"Worker '{worker_id}' sent HTTP-based public IP to Rendezvous.")
                except requests.exceptions.RequestException as e:
                    print(f"Worker '{worker_id}': Error fetching/sending HTTP-based public IP: {e}.")

                await discover_udp_endpoint_and_report(websocket)

                listener_task_handle = None
                if udp_socket and discovered_udp_ip and discovered_udp_port:
                    listener_task_handle = asyncio.create_task(udp_listener_task())
                
                print(f"WORKER '{worker_id}': Entering main WebSocket receive loop...")
                while not stop_signal_received:
                    try:
                        message_raw = await asyncio.wait_for(websocket.recv(), timeout=max(30.0, ping_interval + ping_timeout + 10))
                        print(f"RAW Received from Rendezvous (WebSocket): {message_raw}")
                        try:
                            message_data = json.loads(message_raw)
                            msg_type = message_data.get("type")
                            if msg_type == "udp_endpoint_ack":
                                print(f"Received UDP endpoint ack from Rendezvous: {message_data.get('status')}")
                            elif msg_type == "echo_response": 
                                print(f"Echo Response from Rendezvous: {message_data.get('processed_by_rendezvous')}")
                            else:
                                print(f"Received unhandled message type from Rendezvous: {msg_type}")
                        except json.JSONDecodeError:
                            print(f"Received non-JSON message from Rendezvous: {message_raw}")
                    except asyncio.TimeoutError:
                        pass 
                    except websockets.exceptions.ConnectionClosed:
                        print(f"Worker '{worker_id}': Rendezvous WebSocket connection closed by server.")
                        break 
                print(f"WORKER '{worker_id}': Exited main WebSocket receive loop.")
                
                if listener_task_handle and not listener_task_handle.done():
                    listener_task_handle.cancel()
                    try: await listener_task_handle
                    except asyncio.CancelledError: print(f"Worker '{worker_id}': UDP listener task explicitly cancelled.")
            
        except websockets.exceptions.ConnectionClosedOK:
            print(f"Worker '{worker_id}': Rendezvous WebSocket connection closed gracefully by server.")
        except websockets.exceptions.InvalidURI:
            print(f"Worker '{worker_id}': Invalid Rendezvous WebSocket URI: {rendezvous_ws_url}. Exiting.")
            return 
        except ConnectionRefusedError:
            print(f"Worker '{worker_id}': Connection to Rendezvous refused. Retrying in 10 seconds...")
        except Exception as e: 
            print(f"Worker '{worker_id}': Error in WebSocket connect_to_rendezvous outer loop: {e}. Retrying in 10s...")
        
        if not stop_signal_received:
            await asyncio.sleep(10) 
        else:
            break 

    print(f"Worker '{worker_id}' has stopped WebSocket connection attempts.")

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
        if udp_socket: 
            udp_socket.close()
            print(f"Worker '{worker_id}': UDP socket closed in __main__ finally block.")
```

### 3A.2.3. Worker Service `requirements.txt` (`holepunch/requirements.txt`)

```
websockets>=12.0
requests>=2.0.0 
pystun3>=1.1.6 
# Check for the latest stable version of pystun3
```

### 3A.2.4. Worker Service `Dockerfile.worker` (`holepunch/Dockerfile.worker`)

Make sure the CMD uses `python -u` for unbuffered output:
```dockerfile
# ... (rest of Dockerfile) ...
CMD ["python", "-u", "main.py"]
```

### 3A.2.5. Build and Re-deploy the Worker Service

1.  **Build Image:**
    (Ensure you are in the `holepunch` directory root)
    ```bash
    docker buildx build --platform linux/amd64 -f Dockerfile.worker -t gcr.io/$PROJECT_ID/$AR_WORKER_REPO_NAME/$WORKER_SERVICE_NAME:${WORKER_IMAGE_TAG_V3} . --load
    docker push gcr.io/$PROJECT_ID/$AR_WORKER_REPO_NAME/$WORKER_SERVICE_NAME:${WORKER_IMAGE_TAG_V3}
    ```

2.  **Re-deploy Worker Service:**
    ```bash
    gcloud run deploy $WORKER_SERVICE_NAME \
        --project=$PROJECT_ID \
        --image=gcr.io/$PROJECT_ID/$AR_WORKER_REPO_NAME/$WORKER_SERVICE_NAME:${WORKER_IMAGE_TAG_V3} \
        --platform=managed \
        --region=$REGION \
        --update-env-vars="RENDEZVOUS_SERVICE_URL=${RENDEZVOUS_SERVICE_URL},STUN_HOST=stun.l.google.com,STUN_PORT=19302,PING_INTERVAL_SEC=25,PING_TIMEOUT_SEC=25" \
        --vpc-egress=all-traffic \
        --network=ip-worker-vpc \
        --subnet=ip-worker-subnet \
        --min-instances=1 \
        --max-instances=2 
    ```

-----

## Part 3A.3: Verification

1.  **Deploy both services** (Rendezvous first, then Worker).
2.  **Check Worker Logs:**
      * UDP socket creation: `Worker '...' UDP socket created and bound to local ...`
      * STUN discovery attempt: `Worker '...' Attempting STUN discovery to stun.l.google.com:19302 ...`
      * STUN result: `Worker '...' STUN discovery result: NAT Type=..., External IP=X.X.X.X, External Port=Y`
      * Message sent to Rendezvous: `Worker '...' Sent discovered UDP endpoint (...) to Rendezvous.`
      * Ack received: `Received UDP endpoint ack from Rendezvous: success`
      * Placeholder UDP listener: `Worker '...' Starting UDP listener on ...` and `UDP socket still open ...`
3.  **Check Rendezvous Logs:**
      * Worker WebSocket connection.
      * Receipt of `register_public_ip`.
      * Receipt of `update_udp_endpoint` message.
      * Log showing UDP IP/port update: `Worker '...' updated UDP endpoint to: X.X.X.X:Y`
4.  **Use Rendezvous Debug Endpoint (`/debug/list_workers`):**
      * Verify `stun_reported_udp_ip` and `stun_reported_udp_port` are populated.
      * Verify `websocket_connected` is `true`.

This confirms Step 3A: workers discover and register their public UDP endpoints. 