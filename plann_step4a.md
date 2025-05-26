That's fantastic news\! Seeing those "P2P\_PING" and "P2P\_PONG" messages flowing directly between your Cloud Run workers via UDP is a huge success and confirms that the core UDP hole punching mechanism is working. Congratulations on this significant milestone\!

Now, let's make this interactive and visible in a browser, as you requested. This will be **Step 4: Real-time P2P Data Exchange with Web UI**. We'll break this into a couple of parts as well.

**Step 4 - Part A: Worker Web UI, Local WebSocket Server, and P2P Data Send**

**Objective:**

1.  Modify the **Worker Service** (`holepunch/main.py`) to:
      * Serve a simple HTML page (`index.html`) that will act as the user interface.
      * Run a WebSocket server that this local `index.html` can connect to.
      * When a user types a message into the `index.html` UI and sends it:
          * The message is sent from the browser JavaScript to the worker's Python backend via this new *local* WebSocket.
          * The Python backend then takes this message and sends it over the established P2P UDP connection to its peer worker.
2.  The peer worker (receiving the UDP message) will initially just log it (we'll make it display in its UI in Step 4B).
3.  The `index.html` will have an input field to send messages and an area to display received messages (though receiving display will be fully implemented in 4B).

This step focuses on getting data *from* a browser UI on one worker, through its Python backend, over P2P UDP, to the *Python backend* of the peer worker.

-----

## README - Step 4 (Part A): Worker Web UI, Local WebSocket, and P2P UDP Send

### Prerequisites

1.  **Completion of Step 3B:** Your Rendezvous Service and Worker Services are successfully deployed. UDP hole punching is working, and you can see PING/PONG messages exchanged directly between worker instances in the logs.
2.  All previous project setup (`iceberg-eli`, `us-central1` region, networking, Cloud NAT, etc.) remains the same.
3.  At least two instances of your Worker Service should be deployable.

-----

## Part 4A.1: Modifications to Worker Service (`holepunch/`)

The `holepunch/main.py` will undergo significant changes to incorporate an HTTP server for the HTML page and a WebSocket server for communication with that page, all while maintaining its existing roles (Rendezvous client, STUN client, P2P UDP agent).

### 4A.1.1. Shell Variables (Worker Service)

```bash
export PROJECT_ID="iceberg-eli" 
export REGION="us-central1"   

export AR_WORKER_REPO_NAME="ip-worker-repo" 
export WORKER_SERVICE_NAME="ip-worker-service" 
export WORKER_IMAGE_TAG_V5="v_step4a" # New version tag

# RENDEZVOUS_SERVICE_URL, STUN_HOST, STUN_PORT, INTERNAL_UDP_PORT should be set from Step 3
# Example: export RENDEZVOUS_SERVICE_URL="https://rendezvous-service-xxxx-uc.a.run.app"
# Example: export INTERNAL_UDP_PORT="8081"
```

### 4A.1.2. Create `holepunch/index.html` (NEW FILE)

Create this new file in your `holepunch` directory.

```html
<!DOCTYPE html>
<html>
<head>
    <title>P2P UDP Chat - Worker: <span id="workerIdSpan"></span></title>
    <style>
        body { font-family: sans-serif; margin: 10px; background-color: #f4f4f4; }
        #chatbox { width: 95%; height: 300px; border: 1px solid #ccc; overflow-y: scroll; padding: 10px; background-color: #fff; margin-bottom: 10px; }
        #messageInput { width: 80%; padding: 10px; margin-right: 5px; border: 1px solid #ccc; }
        #sendButton { padding: 10px; background-color: #5cb85c; color: white; border: none; cursor: pointer; }
        #sendButton:hover { background-color: #4cae4c; }
        .message { margin-bottom: 5px; padding: 5px; border-radius: 3px; }
        .local { background-color: #d1e7dd; text-align: right; margin-left: 20%; }
        .peer { background-color: #f8d7da; text-align: left; margin-right: 20%;}
        .system { background-color: #e2e3e5; color: #555; font-style: italic; font-size: 0.9em;}
        #status { margin-top: 10px; font-size: 0.9em; color: #777; }
        #peerInfo { margin-top: 5px; font-size: 0.9em; color: #337ab7; }
    </style>
</head>
<body>
    <h1>P2P UDP Chat - Worker: <span id="workerIdSpan"></span></h1>
    <div id="status">Connecting to local worker backend...</div>
    <div id="peerInfo">Peer: Not connected</div>
    <div id="chatbox"></div>
    <input type="text" id="messageInput" placeholder="Type message..."/>
    <button id="sendButton">Send</button>

    <script>
        const workerIdSpan = document.getElementById('workerIdSpan');
        const chatbox = document.getElementById('chatbox');
        const messageInput = document.getElementById('messageInput');
        const sendButton = document.getElementById('sendButton');
        const statusDiv = document.getElementById('status');
        const peerInfoDiv = document.getElementById('peerInfo');

        let localUiSocket = null;
        let myWorkerId = "Unknown"; // Will be updated by backend

        function addMessage(text, type = "system", sender = "") {
            const messageDiv = document.createElement('div');
            messageDiv.classList.add('message', type);
            if (type === 'local' || type === 'peer') {
                 messageDiv.textContent = `${sender}: ${text}`;
            } else {
                 messageDiv.textContent = text;
            }
            chatbox.appendChild(messageDiv);
            chatbox.scrollTop = chatbox.scrollHeight; // Auto-scroll
        }

        function connectToLocalBackend() {
            // The WebSocket server will run on the same host, different port, or same port with specific path.
            // For Cloud Run, it's simpler if it's the same port but a different path.
            // If serving HTTP and WS on same port with `websockets` library, need careful path handling.
            // Let's assume for now worker backend exposes WS on /ui-ws on its main PORT (e.g., 8080)
            const wsProtocol = window.location.protocol === "https:" ? "wss:" : "ws:";
            const localWsUrl = `${wsProtocol}//${window.location.host}/ui_ws`; 
            
            addMessage(`Attempting to connect to local UI WebSocket at ${localWsUrl}`, "system");
            localUiSocket = new WebSocket(localWsUrl);

            localUiSocket.onopen = function(event) {
                statusDiv.textContent = "Connected to local worker backend. Waiting for P2P link...";
                addMessage("Connection to local worker backend established.", "system");
                // Request worker ID or other initial info
                localUiSocket.send(JSON.stringify({type: "ui_client_hello"}));
            };

            localUiSocket.onmessage = function(event) {
                try {
                    const data = JSON.parse(event.data);
                    console.log("Message from local worker backend:", data);
                    if (data.type === "init_info") {
                        myWorkerId = data.worker_id;
                        workerIdSpan.textContent = myWorkerId.substring(0,8) + "...";
                        addMessage(`Worker ID: ${myWorkerId}`, "system");
                         if(data.p2p_peer_id) {
                            peerInfoDiv.textContent = `P2P Connected to Peer: ${data.p2p_peer_id.substring(0,8)}...`;
                            statusDiv.textContent = "P2P Link Active.";
                        }
                    } else if (data.type === "p2p_message_received") {
                        addMessage(data.content, "peer", data.from_peer_id ? data.from_peer_id.substring(0,8)+"..." : "Peer");
                    } else if (data.type === "p2p_status_update") {
                        addMessage(`P2P Status: ${data.message}`, "system");
                        if (data.peer_id) {
                            peerInfoDiv.textContent = `P2P Connected to Peer: ${data.peer_id.substring(0,8)}...`;
                            statusDiv.textContent = "P2P Link Active.";
                        } else {
                             peerInfoDiv.textContent = "Peer: Not connected";
                             if (!data.message.toLowerCase().includes("lost")) {
                                statusDiv.textContent = "P2P Link Inactive.";
                             }
                        }
                    } else if (data.type === "error") {
                        addMessage(`Error from backend: ${data.message}`, "system");
                    }
                } catch (e) {
                    addMessage("Received non-JSON message from backend: " + event.data, "system");
                }
            };

            localUiSocket.onclose = function(event) {
                statusDiv.textContent = "Disconnected from local worker backend. Attempting to reconnect...";
                addMessage("Connection to local worker backend closed. Retrying in 5s...", "system");
                setTimeout(connectToLocalBackend, 5000);
            };

            localUiSocket.onerror = function(error) {
                statusDiv.textContent = "Error connecting to local worker backend.";
                addMessage("WebSocket error with local worker backend: " + error.message, "system");
                console.error("WebSocket Error: ", error);
            };
        }

        sendButton.onclick = function() {
            const messageText = messageInput.value;
            if (messageText && localUiSocket && localUiSocket.readyState === WebSocket.OPEN) {
                localUiSocket.send(JSON.stringify({
                    type: "send_p2p_message",
                    content: messageText
                }));
                addMessage(messageText, "local", "Me (" + (myWorkerId ? myWorkerId.substring(0,8)+"..." : "") + ")");
                messageInput.value = '';
            } else {
                addMessage("Cannot send message. Not connected to local backend or message is empty.", "system");
            }
        };
        
        messageInput.addEventListener("keypress", function(event) {
            if (event.key === "Enter") {
                event.preventDefault();
                sendButton.click();
            }
        });

        // Initial connection attempt
        connectToLocalBackend();
    </script>
</body>
</html>
```

### 4A.1.3. Worker Service Code (`holepunch/main.py`) Updates

This is the most significant part. We need to add an HTTP server to serve `index.html` and a WebSocket server for the UI, and integrate them with the existing `asyncio` loop and P2P logic.

```python
import asyncio
import os
import uuid
import websockets # For Rendezvous client AND UI server
import signal
import threading
import http.server 
import socketserver 
import requests 
import json
import socket
import stun # pystun3
from typing import Optional, Tuple, Set # Added Set for UI clients

from pathlib import Path # For serving HTML file

# --- Global Variables ---
worker_id = str(uuid.uuid4())
stop_signal_received = False
p2p_udp_transport: Optional[asyncio.DatagramTransport] = None
our_stun_discovered_udp_ip: Optional[str] = None
our_stun_discovered_udp_port: Optional[int] = None
current_p2p_peer_id: Optional[str] = None # To know who we are talking to via UDP
current_p2p_peer_addr: Optional[Tuple[str, int]] = None # (ip, port) for current UDP peer

DEFAULT_STUN_HOST = os.environ.get("STUN_HOST", "stun.l.google.com")
DEFAULT_STUN_PORT = int(os.environ.get("STUN_PORT", "19302"))
INTERNAL_UDP_PORT = int(os.environ.get("INTERNAL_UDP_PORT", "8081"))
HTTP_PORT_FOR_UI = int(os.environ.get("PORT", 8080)) # Cloud Run provides PORT

# For UI WebSocket server
ui_websocket_clients: Set[websockets.WebSocketServerProtocol] = set()

# --- Signal Handler (no major changes from Step 3B) ---
def handle_shutdown_signal(signum, frame):
    global stop_signal_received, p2p_udp_transport
    print(f"Shutdown signal ({signum}) received. Worker '{worker_id}' attempting graceful shutdown.")
    stop_signal_received = True
    if p2p_udp_transport:
        try: p2p_udp_transport.close(); print(f"Worker '{worker_id}': P2P UDP transport closed.")
        except Exception as e: print(f"Worker '{worker_id}': Error closing P2P UDP transport: {e}")
    # Also try to close UI WebSockets gracefully
    for ws_client in list(ui_websocket_clients):
        asyncio.create_task(ws_client.close(reason="Server shutting down"))


# --- HTTP Server for index.html (Async) ---
async def serve_index_html(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        # Basic HTTP request parsing (enough for GET /)
        request_line = await reader.readline()
        if not request_line: # Connection closed by client
             writer.close()
             await writer.wait_closed()
             return

        header = request_line.decode().strip()
        # print(f"HTTP Request: {header}") # Debug

        # Read headers
        while True:
            line = await reader.readline()
            if not line.strip(): # End of headers
                break
        
        if header.startswith("GET / "):
            try:
                html_path = Path(__file__).parent / "index.html"
                with open(html_path, "r") as f:
                    content = f.read()
                response = f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(content)}\r\nConnection: close\r\n\r\n{content}"
            except FileNotFoundError:
                response = "HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\nContent-Length: 13\r\n\r\nFile Not Found"
            except Exception as e_file:
                print(f"Error serving index.html: {e_file}")
                response = "HTTP/1.1 500 Internal Server Error\r\nContent-Type: text/plain\r\nContent-Length: 21\r\n\r\nInternal Server Error"
        else: # Default to 404 for other paths for simplicity
            response = "HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\nContent-Length: 9\r\n\r\nNot Found"

        writer.write(response.encode())
        await writer.drain()
    except ConnectionResetError:
        print("HTTP Client connection reset.") # Common if browser closes connection
    except Exception as e_http:
        print(f"HTTP server error: {e_http}")
    finally:
        if not writer.is_closing():
            writer.close()
            await writer.wait_closed()

# --- WebSocket Server for UI ---
async def ui_websocket_handler(websocket: websockets.WebSocketServerProtocol, path: str):
    global ui_websocket_clients, worker_id, current_p2p_peer_id, p2p_udp_transport, current_p2p_peer_addr
    ui_websocket_clients.add(websocket)
    print(f"Worker '{worker_id}': UI WebSocket client connected from {websocket.remote_address}")
    try:
        # Send initial info to UI
        await websocket.send(json.dumps({
            "type": "init_info", 
            "worker_id": worker_id,
            "p2p_peer_id": current_p2p_peer_id # Send current peer if already connected
        }))

        async for message_raw in websocket:
            print(f"Worker '{worker_id}': Message from UI WebSocket: {message_raw}")
            try:
                message = json.loads(message_raw)
                msg_type = message.get("type")

                if msg_type == "send_p2p_message":
                    content = message.get("content")
                    if content and current_p2p_peer_addr and p2p_udp_transport:
                        print(f"Worker '{worker_id}': Sending P2P UDP message '{content}' to peer {current_p2p_peer_id} at {current_p2p_peer_addr}")
                        p2p_message = {
                            "type": "chat_message", # Define a simple protocol
                            "from_worker_id": worker_id,
                            "content": content
                        }
                        p2p_udp_transport.sendto(json.dumps(p2p_message).encode(), current_p2p_peer_addr)
                    elif not current_p2p_peer_addr:
                         await websocket.send(json.dumps({"type": "error", "message": "Not connected to a P2P peer."}))
                    elif not content:
                         await websocket.send(json.dumps({"type": "error", "message": "Cannot send empty message."}))

                elif msg_type == "ui_client_hello": # UI confirms it's ready
                    print(f"Worker '{worker_id}': UI Client says hello.")
                    # Resend peer info in case it connected after P2P link was established
                    if current_p2p_peer_id:
                         await websocket.send(json.dumps({
                            "type": "p2p_status_update", 
                            "message": f"P2P link active with {current_p2p_peer_id[:8]}...",
                            "peer_id": current_p2p_peer_id
                        }))


            except json.JSONDecodeError:
                print(f"Worker '{worker_id}': UI WebSocket received non-JSON: {message_raw}")
            except Exception as e_ui_msg:
                print(f"Worker '{worker_id}': Error processing UI WebSocket message: {e_ui_msg}")
    except websockets.exceptions.ConnectionClosed:
        print(f"Worker '{worker_id}': UI WebSocket client {websocket.remote_address} disconnected.")
    except Exception as e_ui_conn:
        print(f"Worker '{worker_id}': Error with UI WebSocket connection {websocket.remote_address}: {e_ui_conn}")
    finally:
        ui_websocket_clients.remove(websocket)
        print(f"Worker '{worker_id}': UI WebSocket client {websocket.remote_address} removed.")


# --- Asyncio Datagram Protocol for P2P UDP Listener (from Step 3B) ---
class P2PUDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, worker_id_val: str):
        self.worker_id = worker_id_val
        self.transport: Optional[asyncio.DatagramTransport] = None
        print(f"Worker '{self.worker_id}': P2PUDPProtocol instance created.")

    def connection_made(self, transport: asyncio.DatagramTransport):
        global p2p_udp_transport 
        self.transport = transport
        p2p_udp_transport = transport 
        local_addr = transport.get_extra_info('sockname')
        print(f"Worker '{self.worker_id}': P2P UDP listener active on {local_addr} (Internal Port: {INTERNAL_UDP_PORT}).")

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        global current_p2p_peer_addr, current_p2p_peer_id
        message_str = data.decode(errors='ignore')
        print(f"Worker '{self.worker_id}': == UDP Packet Received from {addr}: '{message_str}' ==")
        
        # If this is the peer we expect, update current_p2p_peer_addr if not set or changed
        # This helps in sending replies if the source port changes mid-communication (less common with EIM)
        # current_p2p_peer_addr = addr 

        try:
            p2p_message = json.loads(message_str)
            msg_type = p2p_message.get("type")
            from_id = p2p_message.get("from_worker_id")
            content = p2p_message.get("content")

            if msg_type == "chat_message":
                print(f"Worker '{self.worker_id}': Received P2P chat message from '{from_id}': '{content}'")
                # Forward to all connected UI clients
                for ui_client_ws in list(ui_websocket_clients):
                    asyncio.create_task(ui_client_ws.send(json.dumps({
                        "type": "p2p_message_received",
                        "from_peer_id": from_id,
                        "content": content
                    })))
            # Handle PING/PONG if you keep them for basic connectivity test
            elif "P2P_PING_FROM_" in message_str: # Legacy from 3B, can be replaced by JSON chat
                print(f"Worker '{self.worker_id}': !!! P2P UDP Ping (legacy) received from {addr} !!!")
                # ... (optional PONG response) ...
        except json.JSONDecodeError:
            print(f"Worker '{self.worker_id}': Received non-JSON UDP packet from {addr}: {message_str}")


    def error_received(self, exc: Exception): # ... (same as 3B) ...
        print(f"Worker '{self.worker_id}': P2P UDP listener error: {exc}")
    def connection_lost(self, exc: Optional[Exception]): # ... (same as 3B) ...
        global p2p_udp_transport
        print(f"Worker '{self.worker_id}': P2P UDP listener connection lost: {exc if exc else 'Closed normally'}")
        if self.transport == p2p_udp_transport: p2p_udp_transport = None

# --- STUN Discovery (largely same as Step 3B) ---
async def discover_and_report_stun_udp_endpoint(websocket_conn_to_rendezvous):
    # ... (This function remains the same as your Step 3B version in holepunch/main.py) ...
    # It uses a temporary socket for STUN and sends "update_udp_endpoint" to Rendezvous.
    # Ensure it sets global `our_stun_discovered_udp_ip` and `our_stun_discovered_udp_port`.
    global our_stun_discovered_udp_ip, our_stun_discovered_udp_port, worker_id
    temp_stun_socket_for_discovery = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        temp_stun_socket_for_discovery.bind(("0.0.0.0", 0))
        local_stun_query_port = temp_stun_socket_for_discovery.getsockname()[1]
        stun_host = os.environ.get("STUN_HOST", DEFAULT_STUN_HOST)
        stun_port = int(os.environ.get("STUN_PORT", DEFAULT_STUN_PORT))
        print(f"Worker '{worker_id}': Attempting STUN discovery via {stun_host}:{stun_port} using temp local UDP port {local_stun_query_port}.")
        nat_type, external_ip, external_port = stun.get_ip_info(
            source_ip="0.0.0.0", source_port=local_stun_query_port, 
            stun_host=stun_host, stun_port=stun_port
        )
        print(f"Worker '{worker_id}': STUN: NAT='{nat_type}', External IP='{external_ip}', Port={external_port}")
        if external_ip and external_port:
            our_stun_discovered_udp_ip = external_ip
            our_stun_discovered_udp_port = external_port
            await websocket_conn_to_rendezvous.send(json.dumps({
                "type": "update_udp_endpoint", "udp_ip": external_ip, "udp_port": external_port
            }))
            print(f"Worker '{worker_id}': Sent STUN UDP endpoint ({external_ip}:{external_port}) to Rendezvous.")
            return True
        return False
    except Exception as e: print(f"Worker '{worker_id}': STUN error: {type(e).__name__} - {e}"); return False
    finally: 
        if temp_stun_socket_for_discovery: temp_stun_socket_for_discovery.close()

# --- UDP Hole Punching (sends initial pings - can be adapted to send first chat message) ---
async def start_udp_hole_punch(peer_udp_ip: str, peer_udp_port: int, peer_worker_id: str):
    # ... (This function remains similar to Step 3B, sending initial UDP packets) ...
    # It now primarily serves to "wake up" the NAT path. Actual chat data is sent on demand.
    global worker_id, stop_signal_received, p2p_udp_transport, current_p2p_peer_addr, current_p2p_peer_id
    
    if not p2p_udp_transport:
        print(f"Worker '{worker_id}': UDP transport not ready for hole punch to '{peer_worker_id}'.")
        return
    
    current_p2p_peer_id = peer_worker_id
    current_p2p_peer_addr = (peer_udp_ip, peer_udp_port) # Store for sending chat messages

    print(f"Worker '{worker_id}': Starting UDP hole punch PINGs towards '{peer_worker_id}' at {current_p2p_peer_addr}")
    
    for i in range(1, 4): # Send a few initial pings
        if stop_signal_received: break
        try:
            message_content = f"P2P_HOLE_PUNCH_PING_FROM_{worker_id}_NUM_{i}"
            p2p_udp_transport.sendto(message_content.encode(), current_p2p_peer_addr)
            print(f"Worker '{worker_id}': Sent UDP Hole Punch PING {i} to {current_p2p_peer_addr}")
        except Exception as e:
            print(f"Worker '{worker_id}': Error sending UDP Hole Punch PING {i}: {e}")
        await asyncio.sleep(0.5)
    
    print(f"Worker '{worker_id}': Finished UDP Hole Punch PING burst to '{peer_worker_id}'.")
    # Notify UI that P2P link is now (hopefully) active
    for ui_client_ws in list(ui_websocket_clients):
        asyncio.create_task(ui_client_ws.send(json.dumps({
            "type": "p2p_status_update", 
            "message": f"P2P link attempt initiated with {peer_worker_id[:8]}...",
            "peer_id": peer_worker_id
            })))


# --- Main WebSocket Connection Logic to Rendezvous (adapting from Step 3B) ---
async def connect_to_rendezvous(rendezvous_ws_url: str, http_server_port: int):
    global stop_signal_received, p2p_udp_transport, INTERNAL_UDP_PORT, ui_websocket_clients
    # ... (Most of this function from your current `holepunch/main.py` (Step 3A/3B) remains the same) ...
    # Key changes:
    # 1. Start the asyncio HTTP server for index.html
    # 2. Start the asyncio WebSocket server for the UI
    # 3. The P2P UDP listener is started *after* STUN success.

    loop = asyncio.get_running_loop()

    # Start local HTTP server for index.html
    # The existing threaded health_check_server is on HTTP_PORT_FOR_UI (e.g. 8080)
    # If we want our UI on a *different* port or path, that's fine.
    # For Cloud Run, all traffic comes to PORT. We need path differentiation.
    # The health_check_server satisfies Cloud Run's need for *something* on PORT.
    # Our UI server (HTTP + WebSocket) could run on PORT, and differentiate by path.
    # Let's make the UI WebSocket server run on PORT as well, path /ui_ws
    # And the HTTP server for / also on PORT, serving index.html

    # This approach uses `websockets.serve` which includes its own HTTP handling for the WS upgrade.
    # For serving index.html, the existing threaded HTTP server is already there on $PORT.
    # We just need to make sure IT serves index.html at "/"
    # And our UI websocket server listens on $PORT at path "/ui_ws"

    # The `start_healthcheck_http_server` from your code needs modification to serve index.html at "/"
    # and 404 others, while our `ui_websocket_handler` is served by `websockets.serve` on a different path.
    # OR, use one server for both. FastAPI is good for this, but adds a dependency.
    # Let's try to make the existing threaded HTTP server smarter.
    
    # For this PoC, we'll keep the health_check_server simple (just OK for /)
    # and start a *separate* asyncio HTTP server for index.html, AND a separate UI WebSocket server.
    # This requires careful port management if running locally.
    # On Cloud Run, $PORT is king.
    # The `websockets` library can co-exist with an HTTP server by path.

    # The `websockets.serve` function handles the HTTP upgrade for WebSockets.
    # It can run on the same port as your main HTTP server if paths are distinct
    # or if the HTTP server knows to delegate WebSocket upgrade requests.
    # For simplicity, the `websockets.serve` will handle its own HTTP for the /ui_ws path.
    # The main HTTP health check is already running.
    
    # Start UI WebSocket Server (on the main $PORT, at path /ui_ws)
    # The `websockets` server needs to be started alongside the `connect_to_rendezvous` client logic.
    # This is tricky because `websockets.serve` is a long-running server.
    # We need to integrate all these async components.

    # --- This function will now be the main async orchestrator ---

    # 1. Start P2P UDP Listener (on INTERNAL_UDP_PORT)
    # This will be started *after* STUN discovery confirms we have an external endpoint
    p2p_listener_transport_local = None # Use local var for this instance of connect_to_rendezvous

    # --- WebSocket to Rendezvous ---
    # (Your existing loop from holepunch/main.py for connecting to Rendezvous)
    # ...
    ip_echo_service_url = "https://api.ipify.org" 
    ping_interval = float(os.environ.get("PING_INTERVAL_SEC", "25")) 
    ping_timeout = float(os.environ.get("PING_TIMEOUT_SEC", "25"))

    while not stop_signal_received:
        # ... (rest of the try/except block for websockets.connect from your current holepunch/main.py) ...
        try:
            async with websockets.connect(
                rendezvous_ws_url,
                ping_interval=ping_interval,
                ping_timeout=ping_timeout,
            ) as ws_to_rendezvous: # Renamed for clarity
                print(f"Worker '{worker_id}' connected to Rendezvous Service.")

                # Report HTTP-based IP (as in your current code)
                try:
                    response = requests.get(ip_echo_service_url, timeout=10)
                    response.raise_for_status()
                    http_public_ip = response.text.strip()
                    await ws_to_rendezvous.send(json.dumps({"type": "register_public_ip", "ip": http_public_ip}))
                    print(f"Worker '{worker_id}' sent HTTP-based IP ({http_public_ip}) to Rendezvous.")
                except Exception as e_http_ip: print(f"Worker '{worker_id}': Error sending HTTP IP: {e_http_ip}")
                
                # Discover and report STUN UDP endpoint
                stun_success = await discover_and_report_stun_udp_endpoint(ws_to_rendezvous)

                if stun_success and not p2p_listener_transport_local: # Start P2P UDP listener if STUN ok
                    try:
                        # Note: P2PUDPProtocol sets the global p2p_udp_transport on connection_made
                        _transport, _protocol = await loop.create_datagram_endpoint(
                            lambda: P2PUDPProtocol(worker_id),
                            local_addr=('0.0.0.0', INTERNAL_UDP_PORT)
                        )
                        p2p_listener_transport_local = _transport # Keep local ref for this connection cycle
                        await asyncio.sleep(0.1) # allow connection_made to run
                        if p2p_udp_transport:
                             print(f"Worker '{worker_id}': Asyncio P2P UDP listener started on 0.0.0.0:{INTERNAL_UDP_PORT}.")
                        else:
                             print(f"Worker '{worker_id}': P2P UDP listener transport not set after create_datagram_endpoint call.")
                    except Exception as e_udp_listen:
                        print(f"Worker '{worker_id}': Failed to start P2P UDP listener: {e_udp_listen}")
                
                # WebSocket receive loop (for messages from Rendezvous)
                while not stop_signal_received:
                    try:
                        message_raw = await asyncio.wait_for(ws_to_rendezvous.recv(), timeout=60.0)
                        # ... (Handle udp_endpoint_ack, echo_response, my_details_response as in your current code) ...
                        # ... (Handle p2p_connection_offer as in previous Step 3B plan) ...
                        print(f"Worker '{worker_id}': Message from Rendezvous: {message_raw}")
                        message_data = json.loads(message_raw)
                        msg_type = message_data.get("type")

                        if msg_type == "p2p_connection_offer":
                            peer_id = message_data.get("peer_worker_id")
                            peer_ip = message_data.get("peer_udp_ip")
                            peer_port = message_data.get("peer_udp_port")
                            if peer_id and peer_ip and peer_port:
                                print(f"Worker '{worker_id}': Received P2P offer for peer '{peer_id}' at {peer_ip}:{peer_port}")
                                if p2p_udp_transport and our_stun_discovered_udp_ip:
                                    asyncio.create_task(start_udp_hole_punch(peer_ip, int(peer_port), peer_id))
                                else:
                                    print(f"Worker '{worker_id}': Cannot P2P, UDP listener/STUN info not ready.")
                            # Other message handlers (ack, etc.)
                        elif msg_type == "udp_endpoint_ack":
                             print(f"Worker '{worker_id}': UDP Endpoint Ack: {message_data.get('status')}")
                        # ...etc
                    except asyncio.TimeoutError: pass
                    except websockets.exceptions.ConnectionClosed: break
                    except Exception as e_recv: print(f"Error in WS recv loop: {e_recv}"); break
                # End of inner while not stop_signal_received (WebSocket recv loop)
                if websockets.exceptions.ConnectionClosed: break # Exit outer loop if WS closed
            
        # ... (Outer WebSocket connection exception handling from your current code) ...
        except Exception as e_ws_connect:
            print(f"Worker '{worker_id}': Error in WebSocket connection loop: {e_ws_connect}. Retrying...")
        
        finally: # Cleanup for this specific WebSocket connection attempt
            if p2p_listener_transport_local: # Close UDP listener associated with this WS session
                print(f"Worker '{worker_id}': Closing P2P UDP transport from this WS session's finally block.")
                p2p_listener_transport_local.close()
                # If p2p_udp_transport is global and set by this, clear it
                if p2p_udp_transport == p2p_listener_transport_local:
                    p2p_udp_transport = None
                p2p_listener_transport_local = None
        
        if not stop_signal_received: await asyncio.sleep(10)
        else: break
    # End of outer while not stop_signal_received (main connection retry loop)

# --- Health Check Server (Threaded - Keep as is from your current code) ---
# This runs on $PORT and answers "/" for Cloud Run health checks.
def start_healthcheck_http_server_threaded():
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            # Serve index.html for GET /
            if self.path == '/':
                try:
                    html_path = Path(__file__).parent / "index.html"
                    with open(html_path, "rb") as f: # Read as bytes
                        content = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                except FileNotFoundError:
                    self.send_response(404)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"index.html not found")
                return
            # Existing simple health check for other paths or if index.html fails
            self.send_response(200); self.send_header("Content-Type","text/plain"); self.end_headers(); self.wfile.write(b"OK (Health Check Server)")
        def log_message(self, format, *args): return # Suppress logs

    port = HTTP_PORT_FOR_UI # Use the $PORT from Cloud Run
    httpd = socketserver.TCPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print(f"Worker '{worker_id}': Threaded HTTP server for index.html and health checks listening on 0.0.0.0:{port}")

async def main_async_runner():
    # This will run the Rendezvous client and the UI WebSocket server concurrently
    loop = asyncio.get_running_loop()

    # Start the UI WebSocket server (on $PORT, path /ui_ws)
    # The websockets.serve also handles HTTP requests for the WebSocket upgrade.
    # We need to ensure this doesn't conflict with the health check HTTP server on the same port.
    # The health check server is very basic. If `websockets.serve` can handle regular HTTP on other paths, great.
    # If not, they need different ports or the main HTTP server needs to proxy/delegate WS upgrades.

    # For Cloud Run, all traffic hits $PORT.
    # The `websockets.serve` will only respond to WebSocket upgrade requests on its specified path.
    # Other HTTP requests to $PORT (like "/") will be handled by our threaded HTTP server.
    
    # Start the UI WebSocket server
    # Note: websockets.serve() handles only WebSocket connections.
    # HTTP requests to this port not matching a WebSocket path will likely be rejected by it.
    # This is why the threaded HTTP server handles GET /.
    ui_ws_server = await websockets.serve(
        ui_websocket_handler, 
        "0.0.0.0", 
        HTTP_PORT_FOR_UI, # Run UI WebSocket on the same port as HTTP server
        subprotocols=["chat"], # Example subprotocol
        process_request=lambda path, headers: None if path == "/ui_ws" else (404, [], b"Not a WebSocket endpoint") # Only handle /ui_ws
    )
    print(f"Worker '{worker_id}': UI WebSocket server listening on 0.0.0.0:{HTTP_PORT_FOR_UI} at path /ui_ws")

    # Construct Rendezvous WebSocket URL (as in your existing __main__)
    rendezvous_base_url_env = os.environ.get("RENDEZVOUS_SERVICE_URL")
    if not rendezvous_base_url_env: # Should have been caught by __main__ but good to check
        print("CRITICAL: RENDEZVOUS_SERVICE_URL missing in main_async_runner."); return

    ws_scheme = "wss" if rendezvous_base_url_env.startswith("https://") else "ws"
    base_url_no_scheme = rendezvous_base_url_env.replace("https://", "").replace("http://", "")
    full_rendezvous_ws_url = f"{ws_scheme}://{base_url_no_scheme}/ws/register/{worker_id}"

    # Start the Rendezvous client connection
    rendezvous_client_task = asyncio.create_task(connect_to_rendezvous(full_rendezvous_ws_url, HTTP_PORT_FOR_UI))

    try:
        await rendezvous_client_task # Keep main running until client task finishes (e.g. on stop_signal)
    except asyncio.CancelledError:
        print(f"Worker '{worker_id}': Rendezvous client task was cancelled.")
    finally:
        ui_ws_server.close()
        await ui_ws_server.wait_closed()
        print(f"Worker '{worker_id}': UI WebSocket server stopped.")
        if p2p_udp_transport: # Ensure UDP is closed if Rendezvous client stops
            p2p_udp_transport.close()
            print(f"Worker '{worker_id}': P2P UDP transport closed from main_async_runner finally.")


if __name__ == "__main__":
    print(f"WORKER SCRIPT (ID: {worker_id}): Initializing...")
    
    # Start the threaded HTTP server for health checks and serving index.html
    start_healthcheck_http_server_threaded()

    rendezvous_base_url_env = os.environ.get("RENDEZVOUS_SERVICE_URL")
    if not rendezvous_base_url_env:
        print("CRITICAL ERROR: RENDEZVOUS_SERVICE_URL environment variable not set. Exiting worker.")
        exit(1) # Critical: Cannot operate without Rendezvous URL

    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)

    try:
        asyncio.run(main_async_runner()) # Run the main async orchestrator
    except KeyboardInterrupt:
        print(f"Worker '{worker_id}' interrupted by user (KeyboardInterrupt in __main__).")
        stop_signal_received = True 
    except Exception as e_main_run:
        print(f"Worker '{worker_id}' CRITICAL ERROR in __main__ asyncio.run: {type(e_main_run).__name__} - {e_main_run}")
    finally:
        print(f"Worker '{worker_id}' main process (_main__) finished or exited.")
        # Ensure final cleanup, though handle_shutdown_signal and task finally blocks should cover most.
```

### 4A.1.4. Worker Service `requirements.txt` (`holepunch/requirements.txt`)

No changes from Step 3B. `websockets` is already included.

```
websockets>=12.0
requests>=2.0.0 
pystun3>=1.1.6 
```

### 4A.1.5. Worker Service `Dockerfile.worker` (`holepunch/Dockerfile.worker`)

No changes needed from Step 3B (still `CMD ["python", "-u", "main.py"]`).

### 4A.1.6. Build and Re-deploy the Worker Service

1.  **Build Image (from `holepunch` directory):**
    ```bash
    # cd path/to/holepunch
    gcloud builds submit --region=$REGION \
        --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG_V5} . \
        --dockerfile=Dockerfile.worker
    ```
2.  **Re-deploy Worker Service:**
    The worker now serves HTTP/WebSocket for its UI, so `--allow-unauthenticated` is needed again if you want to access it directly from your browser without IAP or other auth.
    ```bash
    gcloud run deploy $WORKER_SERVICE_NAME \
        --project=$PROJECT_ID \
        --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_WORKER_REPO_NAME}/${WORKER_SERVICE_NAME}:${WORKER_IMAGE_TAG_V5} \
        --platform=managed \
        --region=$REGION \
        --update-env-vars="RENDEZVOUS_SERVICE_URL=${RENDEZVOUS_SERVICE_URL},STUN_HOST=${STUN_HOST:-stun.l.google.com},STUN_PORT=${STUN_PORT:-19302},INTERNAL_UDP_PORT=${INTERNAL_UDP_PORT_ENV_VAR_VALUE:-8081}" \
        --allow-unauthenticated \
        --vpc-egress=all-traffic \
        --network=$VPC_NETWORK_NAME \
        --subnet=$SUBNET_NAME \
        --min-instances=2 \
        --max-instances=2 \
        --cpu-boost
    ```

-----

## Part 4A.2: Verification

1.  **Deploy/Update both services.**
2.  **Open the Public URL of Worker A in your browser.** You should see the `index.html` page.
      * The status should indicate it's trying to connect to its local backend WebSocket (`/ui_ws`).
      * Once connected, it should display its Worker ID.
      * If P2P pairing with Worker B happens (from Step 3B logic), the UI might update to show connected peer.
3.  **Open the Public URL of Worker B in another browser tab/window.** Same initial behavior.
4.  **Test Sending a Message (Worker A's UI):**
      * Type a message in Worker A's UI input field and click "Send".
      * **Worker A Logs:**
          * Message from UI WebSocket: `{"type": "send_p2p_message", "content": "your message"}`
          * `Sending P2P UDP message 'your message' to peer Worker_B_ID at PeerB_NAT_IP:PeerB_NAT_Port`
      * **Worker B Logs:**
          * **CRUCIAL:** `== UDP Packet Received from PeerA_NAT_IP:PeerA_NAT_Source_Port: '{"type": "chat_message", "from_worker_id": "Worker_A_ID", "content": "your message"}' ==`
          * `Received P2P chat message from 'Worker_A_ID': 'your message'`
5.  **Check Worker A's UI:** The message you sent should appear as a "local" message.
6.  **Check Worker B's UI:** For *this Step 4A*, Worker B's UI will **not** yet display the message received via UDP. We'll implement the P2P UDP -\> UI WebSocket bridge in Step 4B. However, Worker B's *Python logs* should show the UDP packet being received.

**Success for Step 4A means:**

  * Each worker serves its `index.html`.
  * The `index.html` JavaScript successfully connects to its worker's Python backend via a *local* WebSocket (`/ui_ws`).
  * Messages typed into Worker A's UI are sent to Worker A's Python backend.
  * Worker A's Python backend successfully sends these messages as UDP packets to Worker B's NAT endpoint.
  * Worker B's Python backend successfully receives these UDP packets and logs them.

This sets up the pathway for messages from UI to P2P UDP. The next step (4B) will complete the loop by taking UDP messages received by a worker and displaying them in its own UI.