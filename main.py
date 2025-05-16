import asyncio
import os
import uuid
import websockets # Using the 'websockets' library
import signal # For graceful shutdown
import threading
import http.server
import socketserver
import requests
import json
import socket # For UDP sockets
# You will need a STUN client library. pystun3 is a common choice.
# Add 'pystun3' to requirements.txt
import stun # This will be from pystun3
from typing import Optional

# This worker no longer needs Flask for this step, it will be a dedicated WebSocket client.
# If you need HTTP endpoints on the worker later, Flask can be re-added (e.g., in a separate thread).

worker_id = str(uuid.uuid4())
stop_signal_received = False
# Global variable to store the UDP socket so it can be used later for P2P
udp_socket: Optional[socket.socket] = None 
discovered_udp_ip: Optional[str] = None
discovered_udp_port: Optional[int] = None

# Public STUN servers
# Google's STUN servers are often reliable.
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
    print(f"Worker '{worker_id}': Starting UDP listener on {local_ip}:{local_port}...")
    try:
        while not stop_signal_received:
            try:
                await asyncio.sleep(60) 
                if not stop_signal_received:
                     print(f"Worker '{worker_id}': UDP socket still open on local {local_ip}:{local_port} (placeholder for listener)")

            except Exception as e:
                if not stop_signal_received: 
                    print(f"Worker '{worker_id}': Error in placeholder UDP listener loop: {e}")
                break 
    finally:
        print(f"Worker '{worker_id}': UDP listener task stopped.")

async def connect_to_rendezvous(rendezvous_ws_url: str):
    global stop_signal_received, discovered_udp_ip, discovered_udp_port
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
                ping_interval=ping_interval,
                ping_timeout=ping_timeout,
            ) as websocket:
                print(f"WORKER '{worker_id}' connected to Rendezvous. Type of websocket object: {type(websocket)}")
                print(f"WORKER '{worker_id}' Attributes of websocket object: {dir(websocket)}")
                print(f"Worker '{worker_id}' connected to Rendezvous via WebSocket.") # Clarified message

                try:
                    print(f"Worker '{worker_id}' fetching its HTTP-based public IP from {ip_echo_service_url}...")
                    response = requests.get(ip_echo_service_url, timeout=10) # Synchronous call
                    response.raise_for_status()
                    http_public_ip = response.text.strip()
                    print(f"Worker '{worker_id}' identified HTTP-based public IP as: {http_public_ip}")
                    await websocket.send(json.dumps({
                        "type": "register_public_ip", 
                        "ip": http_public_ip
                    }))
                    print(f"Worker '{worker_id}' sent HTTP-based public IP to Rendezvous.")
                except requests.exceptions.RequestException as e:
                    print(f"Worker '{worker_id}': Error fetching/sending HTTP-based public IP: {e}.")

                await discover_udp_endpoint_and_report(websocket)

                # listener_task = None # TEMP COMMENT OUT
                # if udp_socket and discovered_udp_ip and discovered_udp_port:
                #     listener_task = asyncio.create_task(udp_listener_task()) # TEMP COMMENT OUT
                
                print(f"WORKER '{worker_id}': Entering main WebSocket receive loop...")

                # Main receive loop for WebSocket messages from Rendezvous
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
                            # Add other P2P message handlers here in Step 3B
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

                # if listener_task: # TEMP COMMENT OUT
                #     listener_task.cancel() # TEMP COMMENT OUT
                #     try: # TEMP COMMENT OUT
                #         await listener_task # TEMP COMMENT OUT
                #     except asyncio.CancelledError: # TEMP COMMENT OUT
                #         print(f"Worker '{worker_id}': UDP listener task cancelled.") # TEMP COMMENT OUT
            
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

# -----------------------------
# Minimal health-check HTTP server
# -----------------------------
def start_healthcheck_http_server():
    """Start a simple HTTP server that always returns 200/OK."""

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

        # Suppress noisy logging
        def log_message(self, format, *args):
            return

    port = int(os.environ.get("PORT", 8080))
    httpd = socketserver.TCPServer(("0.0.0.0", port), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    print(f"Health-check HTTP server listening on 0.0.0.0:{port}")

# Start the HTTP health server as soon as the module is imported so it's ready quickly.
start_healthcheck_http_server()
print("HEALTHCHECK SERVER STARTED --- WORKER MAIN SCRIPT CONTINUING...")

if __name__ == "__main__":
    print("WORKER SCRIPT: Inside __main__ block.")
    rendezvous_base_url = os.environ.get("RENDEZVOUS_SERVICE_URL")
    if not rendezvous_base_url:
        print("Error: RENDEZVOUS_SERVICE_URL environment variable not set. Exiting.")
        exit(1)

    # Ensure the URL uses wss (secure WebSockets) if deployed with https (default for Cloud Run)
    if rendezvous_base_url.startswith("http://"):
        rendezvous_ws_url_constructed = rendezvous_base_url.replace("http://", "ws://", 1)
    elif rendezvous_base_url.startswith("https://"):
        rendezvous_ws_url_constructed = rendezvous_base_url.replace("https://", "wss://", 1)
    else:
        print(f"Warning: Rendezvous URL '{rendezvous_base_url}' does not start with http/https. Assuming it's a base for ws/wss.")
        rendezvous_ws_url_constructed = rendezvous_base_url # Or prepend ws:// or wss:// as appropriate

    # Append the path and worker_id
    full_rendezvous_ws_url = f"{rendezvous_ws_url_constructed}/ws/register/{worker_id}"
    
    print(f"WORKER SCRIPT: About to call asyncio.run(connect_to_rendezvous) for URL: {full_rendezvous_ws_url}")
    
    # Setup signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, handle_shutdown_signal) # Sent by Cloud Run on instance termination
    signal.signal(signal.SIGINT, handle_shutdown_signal)  # For local Ctrl+C

    try:
        asyncio.run(connect_to_rendezvous(full_rendezvous_ws_url))
    except KeyboardInterrupt:
        print(f"Worker '{worker_id}' interrupted by user. Shutting down.")
    finally:
        print(f"Worker '{worker_id}' main process finished.")
        if udp_socket: # Ensure UDP socket is closed on exit
            udp_socket.close()
            print(f"Worker '{worker_id}': UDP socket closed in __main__ finally block.") 