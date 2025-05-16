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
                print(f"Worker '{worker_id}' connected to Rendezvous. Public IP:Port should be registered by Rendezvous.")
                print(f"WORKER '{worker_id}' connected to Rendezvous. Type of websocket object: {type(websocket)}")
                print(f"WORKER '{worker_id}' Attributes of websocket object: {dir(websocket)}")

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

                # NEW: Task to send periodic "echo_request" and "get_my_details"
                async def send_test_messages():
                    print(f"WORKER '{worker_id}': Starting send_test_messages task...")
                    counter = 0
                    try:
                        while not stop_signal_received and websocket.state.name == "OPEN":
                            print(f"WORKER '{worker_id}': send_test_messages loop, websocket.state={websocket.state.name}, counter={counter}")
                            await asyncio.sleep(15) # Send a message every 15 seconds
                            if websocket.state.name != "OPEN":
                                print(f"WORKER '{worker_id}': send_test_messages - WebSocket no longer OPEN after sleep, before send. State: {websocket.state.name}. Exiting loop.")
                                break
                        
                            counter += 1
                            # Send an echo request
                            echo_req_msg = {
                                "type": "echo_request",
                                "payload": f"Test message from {worker_id}, count: {counter}"
                            }
                            try:
                                await websocket.send(json.dumps(echo_req_msg))
                                print(f"Worker '{worker_id}' sent: {echo_req_msg['type']}. WebSocket state: {websocket.state.name}")
                            except websockets.exceptions.ConnectionClosed: 
                                print(f"WORKER '{worker_id}': send_test_messages - ConnectionClosed during echo_request send. Exiting loop.")
                                break

                            if websocket.state.name != "OPEN":
                                print(f"WORKER '{worker_id}': send_test_messages - WebSocket no longer OPEN after echo_request, before details_req. State: {websocket.state.name}. Exiting loop.")
                                break
                            await asyncio.sleep(2) # Small delay

                            # Send a request for its own details
                            details_req_msg = {"type": "get_my_details"}
                            try:
                                await websocket.send(json.dumps(details_req_msg))
                                print(f"Worker '{worker_id}' sent: {details_req_msg['type']}. WebSocket state: {websocket.state.name}")
                            except websockets.exceptions.ConnectionClosed: 
                                print(f"WORKER '{worker_id}': send_test_messages - ConnectionClosed during details_req send. Exiting loop.")
                                break
                        print(f"WORKER '{worker_id}': send_test_messages loop finished. stop_signal={stop_signal_received}, websocket.state={websocket.state.name}")
                    except AttributeError as ae:
                        print(f"WORKER '{worker_id}': ATTRIBUTE ERROR in send_test_messages task: {ae}. Attributes: {dir(websocket)}")
                    except Exception as e_task:
                        print(f"WORKER '{worker_id}': EXCEPTION in send_test_messages task: {e_task}")
                    finally:
                        print(f"WORKER '{worker_id}': send_test_messages task finally block.")
                
                message_sender_task = asyncio.create_task(send_test_messages())

                # Main receive loop
                while not stop_signal_received:
                    try:
                        message_raw = await asyncio.wait_for(websocket.recv(), timeout=max(30.0, ping_interval + ping_timeout + 5))
                        print(f"RAW Received from Rendezvous: {message_raw}")
                        try:
                            message_data = json.loads(message_raw)
                            msg_type = message_data.get("type")
                            if msg_type == "echo_response":
                                print(f"Echo Response from Rendezvous: {message_data.get('processed_by_rendezvous')}")
                            elif msg_type == "my_details_response":
                                print(f"My Details from Rendezvous: IP={message_data.get('registered_ip')}, Port={message_data.get('registered_port')}")
                            else:
                                print(f"Received unhandled message type from Rendezvous: {msg_type}")
                        except json.JSONDecodeError:
                            print(f"Received non-JSON message from Rendezvous: {message_raw}")
                    except asyncio.TimeoutError:
                        pass 
                    except websockets.exceptions.ConnectionClosed:
                        print(f"Worker '{worker_id}': Rendezvous WebSocket connection closed by server.")
                        break 
                
                message_sender_task.cancel()
                try:
                    await message_sender_task
                except asyncio.CancelledError:
                    print("Message sender task cancelled.")

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