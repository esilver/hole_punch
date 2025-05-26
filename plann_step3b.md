## README - Step 3 (Part B): Peer-to-Peer UDP Connection Attempt

### Objective

This part details the modifications needed to:

1.  Enable the **Rendezvous Service** to actively introduce two registered workers that have valid STUN-discovered UDP endpoints. It does this by sending each a `p2p_connection_offer` message containing the other's details.
2.  Update the **Worker Service** to:
    *   Replace the placeholder UDP listener task with a fully functional `asyncio.DatagramProtocol` (`P2PUDPProtocol`) that listens on its `INTERNAL_UDP_PORT`.
    *   Handle the `p2p_connection_offer` from the Rendezvous service.
    *   Implement the client-side logic for UDP hole punching:
        *   Upon receiving an offer, call a `start_udp_hole_punch` function.
        *   This function sends a burst of UDP PING packets to the peer's specified `NAT_IP:NAT_UDP_Port`.
    *   The `P2PUDPProtocol` will receive incoming UDP packets (e.g., PINGs from the peer) and can send PONGs back, confirming bidirectional P2P UDP communication.

### Prerequisites

1.  **Completion of Step 3A:** Rendezvous and Worker Services are deployed and configured as per `plann_step3a.md`. Workers successfully discover and report their STUN UDP endpoints to the Rendezvous service.
2.  **At least two instances** of the Worker Service are deployable to test P2P interaction.
3.  Relevant GCP project setup (`iceberg-eli`, `us-central1`, networking, Cloud NAT if applicable) remains.

----

## Part 3B.1: Modifications to Rendezvous Service

The Rendezvous service is enhanced to proactively pair workers.

### 3B.1.1. Shell Variables (Rendezvous Service)

```bash
export PROJECT_ID=$(gcloud config get-value project)
export REGION="us-central1"

export AR_RENDEZVOUS_REPO_NAME="rendezvous-repo"
export RENDEZVOUS_SERVICE_NAME="rendezvous-service"
export RENDEZVOUS_IMAGE_TAG_STEP3B="v_step3b"
```

### 3B.1.2. Rendezvous Service Code (`rendezvous_service_code/main.py`) Updates (for Step 3B)

```python
import asyncio
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from typing import Dict, Any, Optional, List # Added List
import os
import json

app = FastAPI(title="Rendezvous Service - Step 3B")

# Data structure for connected_workers (same as end of Step 3A)
connected_workers: Dict[str, Dict[str, Any]] = {}

# New: List of worker_ids that have reported UDP info and are waiting for a peer
workers_ready_for_pairing: List[str] = []

async def attempt_to_pair_workers(newly_ready_worker_id: Optional[str] = None):
    global workers_ready_for_pairing, connected_workers
    
    # Ensure the worker that just became ready is valid and in the list
    if newly_ready_worker_id:
        initiator_data = connected_workers.get(newly_ready_worker_id)
        if not (initiator_data and initiator_data.get("stun_reported_udp_ip") and initiator_data.get("stun_reported_udp_port") and initiator_data.get("websocket")):
            print(f"Pairing: Worker '{newly_ready_worker_id}' is not valid for pairing (missing data/ws). Removing from ready list if present.")
            if newly_ready_worker_id in workers_ready_for_pairing: workers_ready_for_pairing.remove(newly_ready_worker_id)
            return # Cannot initiate with this worker
        if newly_ready_worker_id not in workers_ready_for_pairing:
            workers_ready_for_pairing.append(newly_ready_worker_id)
            print(f"Rendezvous: Worker '{newly_ready_worker_id}' added to ready_for_pairing list. Total ready: {len(workers_ready_for_pairing)}")

    if len(workers_ready_for_pairing) < 2:
        print(f"Rendezvous: Not enough workers ({len(workers_ready_for_pairing)}) ready for pairing. Waiting for more.")
        return

    # Attempt to pair the first two distinct, valid, and connected workers
    peer_a_id = None
    peer_b_id = None
    
    temp_ready_list = list(workers_ready_for_pairing) # Iterate over a copy
    
    for worker_id_candidate in temp_ready_list:
        candidate_data = connected_workers.get(worker_id_candidate)
        ws_candidate = candidate_data.get("websocket") if candidate_data else None
        
        # Check if candidate is still valid (has UDP info and active WebSocket)
        if not (candidate_data and candidate_data.get("stun_reported_udp_ip") and candidate_data.get("stun_reported_udp_port") and 
                ws_candidate and hasattr(ws_candidate, 'client_state') and ws_candidate.client_state.value == 1):
            print(f"Pairing: Worker '{worker_id_candidate}' in ready_list is stale or disconnected. Removing.")
            if worker_id_candidate in workers_ready_for_pairing: workers_ready_for_pairing.remove(worker_id_candidate)
            # Also remove from main connected_workers if websocket is bad
            if worker_id_candidate in connected_workers and (not ws_candidate or not hasattr(ws_candidate, 'client_state') or ws_candidate.client_state.value != 1):
                del connected_workers[worker_id_candidate]
                print(f"Cleaned up stale worker '{worker_id_candidate}' from connected_workers during pairing.")
            continue
        
        # Assign to peer_a_id or peer_b_id
        if peer_a_id is None:
            peer_a_id = worker_id_candidate
        elif peer_a_id != worker_id_candidate: # Ensure distinct peer
            peer_b_id = worker_id_candidate
            break # Found a pair

    if not (peer_a_id and peer_b_id):
        print(f"Rendezvous: Could not find two distinct, valid peers in the ready list. Current ready: {workers_ready_for_pairing}")
        return

    # Found a pair: peer_a_id and peer_b_id. Remove them from ready list.
    workers_ready_for_pairing.remove(peer_a_id)
    workers_ready_for_pairing.remove(peer_b_id)
    print(f"Rendezvous: Selected '{peer_a_id}' and '{peer_b_id}' for pairing. Removed from ready list.")

    peer_a_data = connected_workers[peer_a_id] # Already validated above
    peer_b_data = connected_workers[peer_b_id] # Already validated above
    peer_a_ws = peer_a_data["websocket"]
    peer_b_ws = peer_b_data["websocket"]

    offer_to_a_payload = {
        "type": "p2p_connection_offer",
        "peer_worker_id": peer_b_id,
        "peer_udp_ip": peer_b_data["stun_reported_udp_ip"],
        "peer_udp_port": peer_b_data["stun_reported_udp_port"]
    }
    offer_to_b_payload = {
        "type": "p2p_connection_offer",
        "peer_worker_id": peer_a_id,
        "peer_udp_ip": peer_a_data["stun_reported_udp_ip"],
        "peer_udp_port": peer_a_data["stun_reported_udp_port"]
    }

    try:
        print(f"Rendezvous: Sending P2P offer to Worker '{peer_a_id}' (for peer '{peer_b_id}').")
        await peer_a_ws.send_text(json.dumps(offer_to_a_payload))
        
        print(f"Rendezvous: Sending P2P offer to Worker '{peer_b_id}' (for peer '{peer_a_id}').")
        await peer_b_ws.send_text(json.dumps(offer_to_b_payload))
        print(f"Rendezvous: P2P offers sent successfully to '{peer_a_id}' and '{peer_b_id}'.")
    except Exception as e_send_offer:
        print(f"Rendezvous: Error sending P2P connection offers: {e_send_offer}")
        # If sending failed, try to add them back to ready list if they are still valid and have UDP info
        for p_id in [peer_a_id, peer_b_id]:
            p_data = connected_workers.get(p_id)
            if p_data and p_data.get("stun_reported_udp_ip") and p_id not in workers_ready_for_pairing:
                # Check WebSocket status again before re-adding
                p_ws = p_data.get("websocket")
                if p_ws and hasattr(p_ws, 'client_state') and p_ws.client_state.value == 1:
                    workers_ready_for_pairing.append(p_id)
                    print(f"Re-added worker '{p_id}' to ready list after send offer failure.")
                else:
                    print(f"Worker '{p_id}' not re-added to ready list as WebSocket is no longer active.")

@app.websocket("/ws/register/{worker_id}")
async def websocket_register_worker(websocket: WebSocket, worker_id: str):
    global connected_workers, workers_ready_for_pairing # Ensure global access
    await websocket.accept()
    client_host = websocket.client.host
    client_port = websocket.client.port
    print(f"Worker '{worker_id}' connecting from WebSocket: {client_host}:{client_port}")

    if worker_id in connected_workers:
        print(f"Worker '{worker_id}' re-connecting. Closing old session.")
        old_ws = connected_workers[worker_id].get("websocket")
        if old_ws and hasattr(old_ws, 'client_state') and old_ws.client_state.value == 1:
             try: await old_ws.close(code=1000, reason="New connection from same ID")
             except Exception: pass
        if worker_id in workers_ready_for_pairing: # Remove from ready list if re-connecting
            workers_ready_for_pairing.remove(worker_id)

    connected_workers[worker_id] = {
        "websocket_observed_ip": client_host, "websocket_observed_port": client_port,
        "http_reported_public_ip": None, "stun_reported_udp_ip": None,
        "stun_reported_udp_port": None, "websocket": websocket
    }
    print(f"Worker '{worker_id}' registered. Total: {len(connected_workers)}")

    try:
        while True: 
            raw_data = await websocket.receive_text()
            print(f"Rendezvous: Received from '{worker_id}': {raw_data}")
            try:
                message = json.loads(raw_data)
                msg_type = message.get("type")

                if msg_type == "register_public_ip":
                    # ... (same as Step 3A) ...
                    new_ip = message.get("ip")
                    if new_ip and worker_id in connected_workers:
                        connected_workers[worker_id]["http_reported_public_ip"] = new_ip
                        print(f"Worker '{worker_id}' reported HTTP IP: {new_ip}")
                
                elif msg_type == "update_udp_endpoint": 
                    udp_ip = message.get("udp_ip")
                    udp_port = message.get("udp_port")
                    if udp_ip and udp_port is not None and worker_id in connected_workers:
                        connected_workers[worker_id]["stun_reported_udp_ip"] = udp_ip
                        connected_workers[worker_id]["stun_reported_udp_port"] = int(udp_port)
                        print(f"Worker '{worker_id}' updated STUN UDP to: {udp_ip}:{udp_port}")
                        await websocket.send_text(json.dumps({"type": "udp_endpoint_ack", "status": "success"}))
                        # Key change for Step 3B: Attempt to pair this worker now that it has UDP info
                        await attempt_to_pair_workers(worker_id)
                    else:
                        await websocket.send_text(json.dumps({"type": "udp_endpoint_ack", "status": "error", "detail": "Missing UDP IP/Port"}))
                
                elif msg_type == "echo_request":
                    # ... (same as Step 3A) ...
                    payload = message.get("payload", "")
                    await websocket.send_text(json.dumps({"type": "echo_response", "original_payload": payload, "processed_by_rendezvous": f"Echo for {worker_id}"}))
                else:
                    print(f"Worker '{worker_id}' sent unhandled msg type: {msg_type}")
            # ... (error handling same as Step 3A) ...
            except json.JSONDecodeError: print(f"Worker '{worker_id}' sent non-JSON: {raw_data}")
            except KeyError: print(f"Worker '{worker_id}' no longer in dict.")
            except Exception as e_proc: print(f"Error processing from '{worker_id}': {e_proc}")

    except WebSocketDisconnect:
        print(f"Worker '{worker_id}' disconnected from {client_host}:{client_port}.")
    except Exception as e_ws: print(f"Error with '{worker_id}' WS: {e_ws}")
    finally:
        if worker_id in connected_workers and connected_workers[worker_id].get("websocket") == websocket:
            del connected_workers[worker_id]
            print(f"Worker '{worker_id}' de-registered. Total: {len(connected_workers)}")
        if worker_id in workers_ready_for_pairing: # Clean up from ready list on disconnect
            workers_ready_for_pairing.remove(worker_id)
            print(f"Worker '{worker_id}' removed from ready_for_pairing list due to disconnect.")

@app.get("/")
async def read_root(): return {"message": "Rendezvous Service (Step 3B Version) is running."}

@app.get("/debug/list_workers")
async def list_workers_debug():
    # ... (debug endpoint definition same as Step 3A, but now reflects workers_ready_for_pairing count) ...
    workers_info = {}
    for w_id, data in list(connected_workers.items()):
        ws = data.get("websocket")
        is_conn = ws and hasattr(ws, 'client_state') and ws.client_state.value == 1
        workers_info[w_id] = {
            "ws_ip": data.get("websocket_observed_ip"), "ws_port": data.get("websocket_observed_port"),
            "http_ip": data.get("http_reported_public_ip"),
            "stun_udp_ip": data.get("stun_reported_udp_ip"), "stun_udp_port": data.get("stun_reported_udp_port"),
            "ws_connected": is_conn
        }
    return {"connected_workers_count": len(connected_workers), "workers_ready_for_pairing_count": len(workers_ready_for_pairing), "workers_ready_list": list(workers_ready_for_pairing), "workers_details": workers_info}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
```

### 3B.1.3. Rendezvous Service `requirements.txt` and `Dockerfile.rendezvous`

No changes from Step 3A.

### 3B.1.4. Build and Re-deploy the Rendezvous Service (Step 3B Version)

```bash
# Build (from rendezvous_service_code dir)
gcloud builds submit --region=$REGION \\
    --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:${RENDEZVOUS_IMAGE_TAG_STEP3B} . \\
    --dockerfile=Dockerfile.rendezvous

# Deploy
gcloud run deploy $RENDEZVOUS_SERVICE_NAME \\
    --project=$PROJECT_ID \\
    --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_RENDEZVOUS_REPO_NAME}/${RENDEZVOUS_SERVICE_NAME}:${RENDEZVOUS_IMAGE_TAG_STEP3B} \\
    --platform=managed --region=$REGION --allow-unauthenticated --session-affinity --min-instances=1
```

----

## Part 3B.2: Modifications to Worker Service

The Worker implements `P2PUDPProtocol`, handles `p2p_connection_offer`, and attempts UDP hole punching.

### 3B.2.1. Shell Variables (Worker Service)

```bash
export PROJECT_ID=$(gcloud config get-value project)
export REGION="us-central1"
export AR_WORKER_REPO_NAME="ip-worker-repo"
export WORKER_SERVICE_NAME="ip-worker-service"
export WORKER_IMAGE_TAG_STEP3B="v_step3b"

# RENDEZVOUS_SERVICE_URL, STUN_HOST_ENV, STUN_PORT_ENV, INTERNAL_UDP_PORT_ENV from Step 3A
# Ensure they are set in your environment.
```

### 3B.2.2. Worker Service Code (`main.py`) Updates (for Step 3B)

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
from typing import Optional, Tuple # Added Tuple

# Global variables (some from Step 3A, new ones for P2P state)
worker_id = str(uuid.uuid4())
stop_signal_received = False
p2p_udp_transport: Optional[asyncio.DatagramTransport] = None # For the actual P2P listener
our_stun_discovered_udp_ip: Optional[str] = None
our_stun_discovered_udp_port: Optional[int] = None
current_p2p_peer_id: Optional[str] = None       # ID of the peer we are trying to communicate with
current_p2p_peer_addr: Optional[Tuple[str, int]] = None # (ip, port) of the peer from connection offer

# Config (same as Step 3A)
DEFAULT_STUN_HOST = os.environ.get("STUN_HOST_ENV", "stun.l.google.com")
DEFAULT_STUN_PORT = int(os.environ.get("STUN_PORT_ENV", "19302"))
INTERNAL_UDP_PORT = int(os.environ.get("INTERNAL_UDP_PORT_ENV", "8081"))

# Minimal health check server (same as Step 3A)
import http.server
import socketserver
import threading
HTTP_PORT_FOR_HEALTH = int(os.environ.get("PORT", 8080))

def start_minimal_health_check_server():
    class HealthCheckHandler(http.server.BaseHTTPRequestHandler): 
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, format, *args):
            return # Suppress logs
    httpd = socketserver.TCPServer(("", HTTP_PORT_FOR_HEALTH), HealthCheckHandler)
    threading.Thread(target=httpd.serve_forever,daemon=True).start()
    print(f"Worker '{worker_id}': Health server on {HTTP_PORT_FOR_HEALTH}")

def handle_shutdown_signal(signum, frame):
    global stop_signal_received, p2p_udp_transport
    print(f"Worker '{worker_id}': Shutdown signal ({signum}).")
    stop_signal_received = True
    if p2p_udp_transport: # Close P2P UDP transport on shutdown
        try: p2p_udp_transport.close(); print(f"Worker '{worker_id}': P2P UDP transport closed.")
        except Exception as e_udp_close: print(f"Error closing UDP transport: {e_udp_close}")

# New: Asyncio Datagram Protocol for P2P UDP Listener
class P2PUDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, worker_id_val: str):
        self.worker_id = worker_id_val
        self.transport: Optional[asyncio.DatagramTransport] = None
        print(f"Worker '{self.worker_id}': P2PUDPProtocol instance created.")

    def connection_made(self, transport: asyncio.DatagramTransport):
        global p2p_udp_transport # Store the transport globally for sending from other tasks
        self.transport = transport
        p2p_udp_transport = transport 
        local_addr = transport.get_extra_info('sockname')
        print(f"Worker '{self.worker_id}': P2P UDP listener active on {local_addr} (Bound to INTERNAL_UDP_PORT: {INTERNAL_UDP_PORT}).")

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        message_str = data.decode(errors='ignore')
        print(f"Worker '{self.worker_id}': == UDP Packet Received from {addr}: '{message_str}' ==")
        
        # Basic PING/PONG logic for Step 3B verification
        if message_str.startswith("P2P_PING_FROM_"):
            try:
                # Extract peer_id and original ping number for a more specific PONG
                parts = message_str.split("_FROM_")[1].split("_NUM_")
                peer_id_part = parts[0]
                ping_num_part = parts[1] if len(parts) > 1 else "UNKNOWN"
                print(f"Worker '{self.worker_id}': !!! P2P UDP Ping (Num: {ping_num_part}) received from peer (likely {peer_id_part}) via {addr} !!!")
                response_message = f"P2P_PONG_FROM_{self.worker_id}_TO_{peer_id_part}_ACK_YOUR_PING_NUM_{ping_num_part}".encode()
                self.transport.sendto(response_message, addr) # Send PONG back to the sender's address
                print(f"Worker '{self.worker_id}': Sent UDP PONG to {addr}")
            except IndexError:
                 print(f"Worker '{self.worker_id}': Received malformed PING from {addr}: {message_str}")
            except Exception as e_pong:
                print(f"Worker '{self.worker_id}': Error sending UDP PONG: {e_pong}")
        elif message_str.startswith("P2P_PONG_FROM_"):
            print(f"Worker '{self.worker_id}': !!! P2P UDP Pong received from {addr} !!! Confirmation of P2P link.")
        # In later steps, this will parse JSON for chat/benchmark messages

    def error_received(self, exc: Exception):
        print(f"Worker '{self.worker_id}': P2P UDP listener error: {exc}")

    def connection_lost(self, exc: Optional[Exception]):
        global p2p_udp_transport
        print(f"Worker '{self.worker_id}': P2P UDP listener connection lost: {exc if exc else 'Closed normally'}")
        if self.transport == p2p_udp_transport: # Clear global ref if this was the active one
            p2p_udp_transport = None

# STUN Discovery function (same as Step 3A, ensure it uses INTERNAL_UDP_PORT for source_port)
async def discover_and_report_stun_udp_endpoint(websocket_conn_to_rendezvous):
    # ... (Code from plann_step3a.md for this function) ...
    # Ensure it sets global our_stun_discovered_udp_ip and our_stun_discovered_udp_port
    # And uses INTERNAL_UDP_PORT in stun.get_ip_info's source_port argument.
    # (Copied from previous step, minor log adjustments for clarity)
    global our_stun_discovered_udp_ip, our_stun_discovered_udp_port, worker_id, INTERNAL_UDP_PORT
    stun_host = os.environ.get("STUN_HOST_ENV", DEFAULT_STUN_HOST)
    stun_port = int(os.environ.get("STUN_PORT_ENV", DEFAULT_STUN_PORT))
    print(f"Worker '{worker_id}': STUN check for internal port {INTERNAL_UDP_PORT} via {stun_host}:{stun_port}.")
    try:
        nat_type, external_ip, external_port = stun.get_ip_info(source_ip="0.0.0.0", source_port=INTERNAL_UDP_PORT, stun_host=stun_host, stun_port=stun_port)
        if external_ip and external_port is not None:
            our_stun_discovered_udp_ip, our_stun_discovered_udp_port = external_ip, external_port
            await websocket_conn_to_rendezvous.send(json.dumps({"type": "update_udp_endpoint", "udp_ip": external_ip, "udp_port": external_port}))
            print(f"Worker '{worker_id}': Sent STUN UDP endpoint ({external_ip}:{external_port}) to Rendezvous.")
            return True
        else: print(f"Worker '{worker_id}': STUN failed to return IP/Port."); return False
    except Exception as e: print(f"Worker '{worker_id}': STUN error: {e}"); return False

# New: UDP Hole Punching Logic
async def start_udp_hole_punch(target_peer_udp_ip: str, target_peer_udp_port: int, target_peer_worker_id: str):
    global worker_id, stop_signal_received, p2p_udp_transport, current_p2p_peer_id, current_p2p_peer_addr
    
    if not p2p_udp_transport: 
        print(f"Worker '{worker_id}': P2P UDP transport (listener) not ready. Cannot send hole punch PINGs."); return
    if not our_stun_discovered_udp_ip: # Should be set if P2P transport is ready after STUN
        print(f"Worker '{worker_id}': Own STUN UDP endpoint not discovered. Cannot reliably hole punch."); return

    # Set current peer for context (e.g., if P2PUDPProtocol needs to know who it expects msgs from)
    current_p2p_peer_id = target_peer_worker_id
    current_p2p_peer_addr = (target_peer_udp_ip, target_peer_udp_port)

    print(f"Worker '{worker_id}': Starting UDP hole punch PING burst towards '{target_peer_worker_id}' at {current_p2p_peer_addr}")
    print(f"Worker '{worker_id}': My STUN-discovered endpoint: {our_stun_discovered_udp_ip}:{our_stun_discovered_udp_port} (listening on internal {INTERNAL_UDP_PORT})")

    for i in range(1, 6): # Send 5 PINGs
        if stop_signal_received: break
        try:
            message_content = f"P2P_PING_FROM_{worker_id}_NUM_{i}"
            p2p_udp_transport.sendto(message_content.encode(), current_p2p_peer_addr)
            print(f"Worker '{worker_id}': Sent UDP PING {i} to {current_p2p_peer_addr}")
        except Exception as e_ping:
            print(f"Worker '{worker_id}': Error sending UDP PING {i}: {e_ping}")
        await asyncio.sleep(0.75) # Stagger pings
    
    print(f"Worker '{worker_id}': Finished UDP PING burst to '{target_peer_worker_id}'. Now relying on P2PUDPProtocol listener.")
    # In later steps, UI can be notified here about connection attempt status.


async def connect_to_rendezvous(rendezvous_ws_url_base: str):
    global stop_signal_received, worker_id, p2p_udp_transport, INTERNAL_UDP_PORT, current_p2p_peer_id, current_p2p_peer_addr
    # Placeholder UDP listener from Step 3A is now replaced by the actual P2PUDPProtocol setup.
    # The P2PUDPProtocol listener will be (re)started within each successful WebSocket connection cycle after STUN.
    
    full_rendezvous_ws_url = f"{rendezvous_ws_url_base}/ws/register/{worker_id}"
    ip_echo_service_url = "https://api.ipify.org"
    ping_interval = float(os.environ.get("PING_INTERVAL_SEC", "25"))
    ping_timeout = float(os.environ.get("PING_TIMEOUT_SEC", "25"))
    
    # Main connection loop (retries on failure)
    while not stop_signal_received:
        p2p_listener_transport_for_this_cycle: Optional[asyncio.DatagramTransport] = None
        try:
            async with websockets.connect(full_rendezvous_ws_url, ping_interval=ping_interval, ping_timeout=ping_timeout) as websocket:
                print(f"Worker '{worker_id}': Connected to Rendezvous.")
                # Report HTTP IP (Step 2 logic)
                try:
                    r = requests.get(ip_echo_service_url, timeout=10); r.raise_for_status(); ip = r.text.strip()
                    await websocket.send(json.dumps({"type": "register_public_ip", "ip": ip}))
                    print(f"Worker '{worker_id}': Sent HTTP IP ({ip}).")
                except Exception as e_http: print(f"HTTP IP Error: {e_http}")

                # STUN Discovery (Step 3A logic)
                stun_success = await discover_and_report_stun_udp_endpoint(websocket)

                if stun_success:
                    # Start the actual P2P UDP listener IF STUN was successful
                    # And if a listener (global p2p_udp_transport) isn't already somehow active from a previous unclean cycle.
                    if p2p_udp_transport:
                        print(f"Warning: Worker '{worker_id}': Global p2p_udp_transport was already set. Closing it before starting new one.")
                        p2p_udp_transport.close()
                        p2p_udp_transport = None
                    try:
                        loop = asyncio.get_running_loop()
                        # P2PUDPProtocol.connection_made will set global p2p_udp_transport
                        transport, protocol = await loop.create_datagram_endpoint(
                            lambda: P2PUDPProtocol(worker_id),
                            local_addr=('0.0.0.0', INTERNAL_UDP_PORT)
                        )
                        p2p_listener_transport_for_this_cycle = transport # For cleanup specific to this WS cycle
                        await asyncio.sleep(0.1) # Allow connection_made to run and set global
                        if not p2p_udp_transport:
                             print(f"CRITICAL: Worker '{worker_id}': P2P UDP listener transport NOT SET globally after create_datagram_endpoint.")
                    except OSError as e_os_listen: # e.g. Address already in use for INTERNAL_UDP_PORT
                         print(f"Worker '{worker_id}': FAILED to start P2P UDP listener on port {INTERNAL_UDP_PORT}: {e_os_listen}. P2P will not work.")
                    except Exception as e_udp_start:
                        print(f"Worker '{worker_id}': Error starting P2P UDP listener: {e_udp_start}")
                else:
                    print(f"Worker '{worker_id}': STUN discovery failed. P2P UDP listener will not be started.")
                
                # Main WebSocket receive loop for messages from Rendezvous
                while not stop_signal_received:
                    try:
                        message_raw = await asyncio.wait_for(websocket.recv(), timeout=ping_interval + 10)
                        print(f"Worker '{worker_id}': From Rendezvous: {message_raw}")
                        message_data = json.loads(message_raw)
                        msg_type = message_data.get("type")

                        if msg_type == "udp_endpoint_ack": # (from Step 3A)
                            print(f"UDP Endpoint Ack: Status '{message_data.get('status')}'")
                        elif msg_type == "echo_response": # (from Step 2/3A)
                            print(f"Echo Response: '{message_data.get('processed_by_rendezvous')}'")
                        elif msg_type == "p2p_connection_offer": # New for Step 3B
                            peer_id = message_data.get("peer_worker_id")
                            peer_ip = message_data.get("peer_udp_ip")
                            peer_port = message_data.get("peer_udp_port")
                            if peer_id and peer_ip and peer_port is not None:
                                print(f"Worker '{worker_id}': Received P2P offer for '{peer_id}' at {peer_ip}:{peer_port}")
                                if p2p_udp_transport and our_stun_discovered_udp_ip: # Our UDP setup must be ready
                                    asyncio.create_task(start_udp_hole_punch(peer_ip, int(peer_port), peer_id))
                                else:
                                    print(f"Worker '{worker_id}': Cannot start P2P: My UDP listener/STUN info not ready.")
                            else:
                                print(f"Worker '{worker_id}': Received incomplete P2P offer: {message_data}")
                        else: print(f"Unhandled msg from Rendezvous: {msg_type}")
                    except asyncio.TimeoutError: pass
                    except websockets.exceptions.ConnectionClosed: print("Rendezvous WS closed."); break
                    except Exception as e_recv: print(f"WS recv error: {e_recv}"); break
                if stop_signal_received or websocket.closed: break
        
        except Exception as e_conn_cycle: 
            print(f"Worker '{worker_id}': Error in Rendezvous WS connection cycle: {type(e_conn_cycle).__name__} - {e_conn_cycle}")
        finally:
            # Clean up P2P UDP listener specific to THIS WebSocket connection cycle
            if p2p_listener_transport_for_this_cycle:
                print(f"Worker '{worker_id}': Closing P2P UDP transport from this WS cycle.")
                p2p_listener_transport_for_this_cycle.close()
                # If this transport also set the global one, clear the global one
                if p2p_udp_transport == p2p_listener_transport_for_this_cycle:
                    p2p_udp_transport = None
            # Reset P2P peer state as WS connection is lost/re-attempted
            current_p2p_peer_id = None
            current_p2p_peer_addr = None

        if not stop_signal_received: await asyncio.sleep(10); print("Retrying Rendezvous connection...")
        else: break
    print(f"Worker '{worker_id}': Stopped connect_to_rendezvous loop.")

if __name__ == "__main__":
    print(f"WORKER SCRIPT (ID: {worker_id}): Initializing for Step 3B...")
    start_minimal_health_check_server()
    rendezvous_url = os.environ.get("RENDEZVOUS_SERVICE_URL")
    if not rendezvous_url: exit("CRITICAL: RENDEZVOUS_SERVICE_URL not set.")
    ws_scheme = "wss" if rendezvous_url.startswith("https://") else "ws"
    rendezvous_ws_url_base = f"{ws_scheme}://{rendezvous_url.replace('https://', '').replace('http://', '')}"
    signal.signal(signal.SIGTERM, handle_shutdown_signal); signal.signal(signal.SIGINT, handle_shutdown_signal)
    try: asyncio.run(connect_to_rendezvous(rendezvous_ws_url_base))
    except KeyboardInterrupt: print(f"Worker '{worker_id}': Interrupted.")
    except Exception as e: print(f"Worker '{worker_id}': Main CRITICAL: {e}")
    finally:
        print(f"Worker '{worker_id}': Main process exit.")
        if p2p_udp_transport: # Final cleanup of global UDP transport if set
            print(f"Worker '{worker_id}': Final cleanup closing P2P UDP transport.")
            p2p_udp_transport.close()
```

### 3B.2.3. Worker Service `requirements.txt`
Remains the same as Step 3A (`websockets`, `requests`, `pystun3`).

### 3B.2.4. Worker Service `Dockerfile.worker`
Remains the same as Step 3A.

### 3B.2.5. Build and Re-deploy the Worker Service (Step 3B Version)

```bash
# Build (from project root)
gcloud builds submit --region=$REGION \\
    --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG_STEP3B} . \\
    --dockerfile=Dockerfile.worker

# Deploy (ensure RENDEZVOUS_SERVICE_URL and STUN/UDP env vars are set)
# CRITICAL: Deploy AT LEAST 2 INSTANCES for P2P testing.
gcloud run deploy $WORKER_SERVICE_NAME \\
    --project=$PROJECT_ID \\
    --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG_STEP3B} \\
    --platform=managed --region=$REGION \\
    --set-env-vars="RENDEZVOUS_SERVICE_URL=${RENDEZVOUS_SERVICE_URL},STUN_HOST_ENV=${STUN_HOST_ENV},STUN_PORT_ENV=${STUN_PORT_ENV},INTERNAL_UDP_PORT_ENV=${INTERNAL_UDP_PORT_ENV}" \\
    --min-instances=2 --max-instances=2 # Important for P2P test
    # --cpu-boost # Consider for better I/O responsiveness
    # --vpc-egress, --network, --subnet if applicable for NAT
```

----

## Part 3B.3: Verification (for Step 3B)

1.  **Deploy/Update Services:** Rendezvous (Step 3B tag), then two or more Worker instances (Step 3B tag).
2.  **Monitor Rendezvous Logs:**
    *   Confirm workers connect, report HTTP IP, then STUN UDP endpoints.
    *   Look for: `Worker '...' added to ready_for_pairing list...`
    *   Crucially: `Rendezvous: Selected 'WORKER_A_ID' and 'WORKER_B_ID' for pairing...`
    *   `Rendezvous: Sent P2P offer to Worker 'WORKER_A_ID' ...` and `...to Worker 'WORKER_B_ID' ...`
3.  **Monitor Logs of Worker A and Worker B:**
    *   STUN success, P2P UDP listener started on `INTERNAL_UDP_PORT`.
    *   `Received P2P offer for peer 'PEER_ID' at PEER_IP:PEER_PORT`
    *   `Starting UDP hole punch PING burst towards 'PEER_ID' ...`
    *   `Sent UDP PING X to PEER_IP:PEER_PORT`
    *   **SUCCESS INDICATOR (on Worker A receiving from B, and vice-versa):**
        *   `== UDP Packet Received from PeerNatIP:PeerNatSourcePort: 'P2P_PING_FROM_PEER_ID_NUM_X' ==`
        *   `!!! P2P UDP Ping received from peer ... !!!`
        *   `Sent UDP PONG to PeerNatIP:PeerNatSourcePort`
        *   And subsequently: `!!! P2P UDP Pong received from PeerNatIP:PeerNatSourcePort !!!`

If both workers log receiving PINGs directly from each other and PONGs are also exchanged, UDP hole punching is successful for basic PING/PONG messages.