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

# This worker no longer needs Flask for this step, it will be a dedicated WebSocket client.
# If you need HTTP endpoints on the worker later, Flask can be re-added (e.g., in a separate thread).

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

    while not stop_signal_received:
        try:
            async with websockets.connect(
                rendezvous_ws_url,
                ping_interval=float(os.environ.get("PING_INTERVAL_SEC", "25")),
                ping_timeout=float(os.environ.get("PING_TIMEOUT_SEC", "25")),
            ) as websocket:
                print(f"Worker '{worker_id}' connected to Rendezvous. Public IP:Port should be registered by Rendezvous.")

                # Get self public IP and send to rendezvous service
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

                # Keep the connection alive and listen for messages from Rendezvous
                # (e.g., instructions to connect to a peer in a later step)
                while not stop_signal_received:
                    try:
                        message = await asyncio.wait_for(websocket.recv(), timeout=30.0) # Keepalive/timeout
                        print(f"Received message from Rendezvous: {message}")
                    except asyncio.TimeoutError:
                        # No message received, send a ping to keep connection alive if supported by server
                        # Or just continue to show the connection is active on the client side
                        # print(f"Worker '{worker_id}' still connected, no message in 30s.")
                        # await websocket.ping() # Requires server to handle pongs
                        pass 
                    except websockets.exceptions.ConnectionClosed:
                        print(f"Worker '{worker_id}': Rendezvous WebSocket connection closed by server.")
                        break # Break inner loop to attempt reconnection

        except websockets.exceptions.ConnectionClosedOK:
            print(f"Worker '{worker_id}': Rendezvous WebSocket connection closed gracefully by server.")
        except websockets.exceptions.InvalidURI:
            print(f"Worker '{worker_id}': Invalid Rendezvous WebSocket URI: {rendezvous_ws_url}. Exiting.")
            return # Cannot recover from this
        except ConnectionRefusedError:
            print(f"Worker '{worker_id}': Connection to Rendezvous refused. Retrying in 10 seconds...")
        except Exception as e:
            print(f"Worker '{worker_id}': Error connecting/communicating with Rendezvous: {e}. Retrying in 10 seconds...")
        
        if not stop_signal_received:
            await asyncio.sleep(10) # Wait before retrying connection
        else:
            break # Exit outer loop if stop signal was received

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