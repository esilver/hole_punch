import asyncio
import os
import uuid
import websockets # For Rendezvous client AND UI server
from websockets.datastructures import ConnectionState # ADDED FOR EXPLICIT STATE CHECK
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
from websockets.server import serve as websockets_serve # Alias to avoid confusion
from websockets.http import Headers # For process_request

# --- Global Variables ---
worker_id = str(uuid.uuid4())
stop_signal_received = False
p2p_udp_transport: Optional[asyncio.DatagramTransport] = None
our_stun_discovered_udp_ip: Optional[str] = None # STUN Result IP
our_stun_discovered_udp_port: Optional[int] = None # STUN Result Port
current_p2p_peer_id: Optional[str] = None # To know who we are talking to via UDP
current_p2p_peer_addr: Optional[Tuple[str, int]] = None # (ip, port) for current UDP peer

DEFAULT_STUN_HOST = os.environ.get("STUN_HOST", "stun.l.google.com")
DEFAULT_STUN_PORT = int(os.environ.get("STUN_PORT", "19302"))
INTERNAL_UDP_PORT = int(os.environ.get("INTERNAL_UDP_PORT", "8081"))
HTTP_PORT_FOR_UI = int(os.environ.get("PORT", 8080)) # Cloud Run provides PORT

ui_websocket_clients: Set[websockets.WebSocketServerProtocol] = set()

async def _send_to_ui_client_robustly(ui_client_ws: websockets.WebSocketServerProtocol, message_json_str: str, log_prefix: str):
    """Helper to send a JSON string message to a UI client with error handling."""
    try:
        await ui_client_ws.send(message_json_str)
    except websockets.exceptions.ConnectionClosed:
        print(f"{log_prefix}: UI client {ui_client_ws.remote_address} disconnected before message could be sent or during send.")
    except Exception as e_send_ui:
        print(f"{log_prefix}: Error sending message to UI client {ui_client_ws.remote_address}: {type(e_send_ui).__name__} - {e_send_ui}")

def handle_shutdown_signal(signum, frame):
    global stop_signal_received, p2p_udp_transport
    print(f"Shutdown signal ({signum}) received. Worker '{worker_id}' attempting graceful shutdown.")
    stop_signal_received = True
    if p2p_udp_transport:
        try: p2p_udp_transport.close(); print(f"Worker '{worker_id}': P2P UDP transport closed.")
        except Exception as e: print(f"Worker '{worker_id}': Error closing P2P UDP transport: {e}")
    for ws_client in list(ui_websocket_clients):
        asyncio.create_task(ws_client.close(reason="Server shutting down"))

async def process_http_request(path: str, request_headers: Headers) -> Optional[Tuple[int, Headers, bytes]]:
    if path == "/ui_ws": 
        return None  
    if path == "/":
        try:
            html_path = Path(__file__).parent / "index.html"
            with open(html_path, "rb") as f: content = f.read()
            headers = Headers([("Content-Type", "text/html"), ("Content-Length", str(len(content)))])
            return (200, headers, content)
        except FileNotFoundError:
            content = b"index.html not found"
            headers = Headers([("Content-Type", "text/plain"), ("Content-Length", str(len(content)))])
            return (404, headers, content)
        except Exception as e_file:
            print(f"Error serving index.html: {e_file}")
            content = b"Internal Server Error"
            headers = Headers([("Content-Type", "text/plain"), ("Content-Length", str(len(content)))])
            return (500, headers, content)
    elif path == "/health": 
        content = b"OK"
        headers = Headers([("Content-Type", "text/plain"), ("Content-Length", str(len(content)))])
        return (200, headers, content)
    else:
        content = b"Not Found"
        headers = Headers([("Content-Type", "text/plain"), ("Content-Length", str(len(content)))])
        return (404, headers, content)

async def ui_websocket_handler(websocket: websockets.WebSocketServerProtocol, path: str):
    global ui_websocket_clients, worker_id, current_p2p_peer_id, p2p_udp_transport, current_p2p_peer_addr
    ui_websocket_clients.add(websocket)
    print(f"Worker '{worker_id}': UI WebSocket client connected from {websocket.remote_address}")
    try:
        await websocket.send(json.dumps({
            "type": "init_info", 
            "worker_id": worker_id,
            "p2p_peer_id": current_p2p_peer_id
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
                        p2p_message = {"type": "chat_message", "from_worker_id": worker_id, "content": content}
                        p2p_udp_transport.sendto(json.dumps(p2p_message).encode(), current_p2p_peer_addr)
                    elif not current_p2p_peer_addr: await websocket.send(json.dumps({"type": "error", "message": "Not connected to a P2P peer."}))
                    elif not content: await websocket.send(json.dumps({"type": "error", "message": "Cannot send empty message."}))
                elif msg_type == "ui_client_hello":
                    print(f"Worker '{worker_id}': UI Client says hello.")
                    if current_p2p_peer_id:
                         await websocket.send(json.dumps({"type": "p2p_status_update", "message": f"P2P link active with {current_p2p_peer_id[:8]}...", "peer_id": current_p2p_peer_id}))
            except json.JSONDecodeError: print(f"Worker '{worker_id}': UI WebSocket received non-JSON: {message_raw}")
            except Exception as e_ui_msg: print(f"Worker '{worker_id}': Error processing UI WebSocket message: {e_ui_msg}")
    except websockets.exceptions.ConnectionClosed: print(f"Worker '{worker_id}': UI WebSocket client {websocket.remote_address} disconnected.")
    except Exception as e_ui_conn: print(f"Worker '{worker_id}': Error with UI WebSocket connection {websocket.remote_address}: {e_ui_conn}")
    finally:
        ui_websocket_clients.remove(websocket)
        print(f"Worker '{worker_id}': UI WebSocket client {websocket.remote_address} removed.")

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
        log_prefix_for_handler = f"Worker '{self.worker_id}'"
        print(f"{log_prefix_for_handler}: == UDP Packet Received from {addr}: '{message_str}' ==")
        try:
            p2p_message = json.loads(message_str)
            msg_type = p2p_message.get("type")
            from_id = p2p_message.get("from_worker_id")
            content = p2p_message.get("content")
            if msg_type == "chat_message":
                print(f"{log_prefix_for_handler}: Received P2P chat message from '{from_id}': '{content}'")
                # Forward to all connected UI clients
                print(f"{log_prefix_for_handler}: Forwarding to {len(ui_websocket_clients)} UI clients.") # DEBUG LINE 1
                for ui_client_ws in list(ui_websocket_clients): 
                    print(f"{log_prefix_for_handler}: Attempting to send to UI client: {ui_client_ws.remote_address}") # DEBUG LINE 2
                    payload_json_str = json.dumps({
                        "type": "p2p_message_received",
                        "from_peer_id": from_id,
                        "content": content
                    })
                    asyncio.create_task(_send_to_ui_client_robustly(ui_client_ws, payload_json_str, log_prefix_for_handler))
            elif "P2P_PING_FROM_" in message_str:
                print(f"{log_prefix_for_handler}: !!! P2P UDP Ping (legacy) received from {addr} !!!")
        except json.JSONDecodeError: print(f"{log_prefix_for_handler}: Received non-JSON UDP packet from {addr}: {message_str}")
    def error_received(self, exc: Exception): print(f"Worker '{self.worker_id}': P2P UDP listener error: {exc}")
    def connection_lost(self, exc: Optional[Exception]): 
        global p2p_udp_transport
        print(f"Worker '{self.worker_id}': P2P UDP listener connection lost: {exc if exc else 'Closed normally'}")
        if self.transport == p2p_udp_transport: p2p_udp_transport = None

async def discover_and_report_stun_udp_endpoint(websocket_conn_to_rendezvous):
    global our_stun_discovered_udp_ip, our_stun_discovered_udp_port, worker_id
    temp_stun_socket_for_discovery = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        temp_stun_socket_for_discovery.bind(("0.0.0.0", 0))
        local_stun_query_port = temp_stun_socket_for_discovery.getsockname()[1]
        stun_host = os.environ.get("STUN_HOST", DEFAULT_STUN_HOST)
        stun_port = int(os.environ.get("STUN_PORT", DEFAULT_STUN_PORT))
        print(f"Worker '{worker_id}': Attempting STUN discovery via {stun_host}:{stun_port} using temp local UDP port {local_stun_query_port}.")
        
        # Broader exception catch specifically for get_ip_info
        try:
            nat_type, external_ip, external_port = stun.get_ip_info(
                stun_host=stun_host, 
                stun_port=stun_port
            )
            print(f"Worker '{worker_id}': STUN: NAT='{nat_type}', External IP='{external_ip}', Port={external_port}")
        except Exception as stun_e: # Catch any error from stun.get_ip_info
            print(f"Worker '{worker_id}': STUN get_ip_info failed: {type(stun_e).__name__} - {stun_e}")
            return False # Indicate STUN failure

        if external_ip and external_port:
            our_stun_discovered_udp_ip = external_ip
            our_stun_discovered_udp_port = external_port
            await websocket_conn_to_rendezvous.send(json.dumps({"type": "update_udp_endpoint", "udp_ip": external_ip, "udp_port": external_port}))
            print(f"Worker '{worker_id}': Sent STUN UDP endpoint ({external_ip}:{external_port}) to Rendezvous.")
            return True
        else:
            print(f"Worker '{worker_id}': STUN discovery did not return valid external IP/Port.")
            return False
            
    except socket.gaierror as e_gaierror: # For DNS errors before calling stun.get_ip_info
        print(f"Worker '{worker_id}': STUN host DNS resolution error: {e_gaierror}")
        return False
    except Exception as e_outer: # For other errors like socket binding
        print(f"Worker '{worker_id}': Error in STUN setup (e.g., socket bind): {type(e_outer).__name__} - {e_outer}")
        return False
    finally: 
        if temp_stun_socket_for_discovery: temp_stun_socket_for_discovery.close()

async def start_udp_hole_punch(peer_udp_ip: str, peer_udp_port: int, peer_worker_id: str):
    global worker_id, stop_signal_received, p2p_udp_transport, current_p2p_peer_addr, current_p2p_peer_id
    if not p2p_udp_transport: print(f"Worker '{worker_id}': UDP transport not ready for hole punch to '{peer_worker_id}'."); return
    current_p2p_peer_id = peer_worker_id
    current_p2p_peer_addr = (peer_udp_ip, peer_udp_port)
    print(f"Worker '{worker_id}': Starting UDP hole punch PINGs towards '{peer_worker_id}' at {current_p2p_peer_addr}")
    for i in range(1, 4):
        if stop_signal_received: break
        try:
            message_content = f"P2P_HOLE_PUNCH_PING_FROM_{worker_id}_NUM_{i}"
            p2p_udp_transport.sendto(message_content.encode(), current_p2p_peer_addr)
            print(f"Worker '{worker_id}': Sent UDP Hole Punch PING {i} to {current_p2p_peer_addr}")
        except Exception as e: print(f"Worker '{worker_id}': Error sending UDP Hole Punch PING {i}: {e}")
        await asyncio.sleep(0.5)
    print(f"Worker '{worker_id}': Finished UDP Hole Punch PING burst to '{peer_worker_id}'.")
    for ui_client_ws in list(ui_websocket_clients):
        asyncio.create_task(ui_client_ws.send(json.dumps({"type": "p2p_status_update", "message": f"P2P link attempt initiated with {peer_worker_id[:8]}...", "peer_id": peer_worker_id})))

async def connect_to_rendezvous(rendezvous_ws_url: str):
    global stop_signal_received, p2p_udp_transport, INTERNAL_UDP_PORT, ui_websocket_clients, our_stun_discovered_udp_ip, our_stun_discovered_udp_port
    ip_echo_service_url = "https://api.ipify.org" 
    ping_interval = float(os.environ.get("PING_INTERVAL_SEC", "25")) 
    ping_timeout = float(os.environ.get("PING_TIMEOUT_SEC", "25")) 
    udp_listener_active = False
    loop = asyncio.get_running_loop()

    while not stop_signal_received:
        p2p_listener_transport_local_ref = None 
        try:
            async with websockets.connect(rendezvous_ws_url, ping_interval=ping_interval, ping_timeout=ping_timeout) as ws_to_rendezvous:
                print(f"Worker '{worker_id}' connected to Rendezvous Service.")
                try:
                    response = requests.get(ip_echo_service_url, timeout=10)
                    response.raise_for_status()
                    http_public_ip = response.text.strip()
                    await ws_to_rendezvous.send(json.dumps({"type": "register_public_ip", "ip": http_public_ip}))
                    print(f"Worker '{worker_id}' sent HTTP-based IP ({http_public_ip}) to Rendezvous.")
                except Exception as e_http_ip: print(f"Worker '{worker_id}': Error sending HTTP IP: {e_http_ip}")
                
                stun_success = await discover_and_report_stun_udp_endpoint(ws_to_rendezvous)

                if stun_success and not udp_listener_active:
                    try:
                        _transport, _protocol = await loop.create_datagram_endpoint(lambda: P2PUDPProtocol(worker_id), local_addr=('0.0.0.0', INTERNAL_UDP_PORT))
                        p2p_listener_transport_local_ref = _transport 
                        await asyncio.sleep(0.1) 
                        if p2p_udp_transport: print(f"Worker '{worker_id}': Asyncio P2P UDP listener appears started on 0.0.0.0:{INTERNAL_UDP_PORT}.")
                        else: print(f"Worker '{worker_id}': P2P UDP listener transport not set globally after create_datagram_endpoint.")
                        udp_listener_active = True
                    except Exception as e_udp_listen: print(f"Worker '{worker_id}': Failed to start P2P UDP listener: {e_udp_listen}")
                
                print(f"WORKER '{worker_id}': Entering main WebSocket receive loop...")
                while not stop_signal_received:
                    try:
                        message_raw = await asyncio.wait_for(ws_to_rendezvous.recv(), timeout=60.0)
                        print(f"Worker '{worker_id}': Message from Rendezvous: {message_raw}")
                        message_data = json.loads(message_raw)
                        msg_type = message_data.get("type")
                        if msg_type == "p2p_connection_offer":
                            peer_id = message_data.get("peer_worker_id")
                            peer_ip = message_data.get("peer_udp_ip")
                            peer_port = message_data.get("peer_udp_port")
                            if peer_id and peer_ip and peer_port:
                                print(f"Worker '{worker_id}': Received P2P offer for peer '{peer_id}' at {peer_ip}:{peer_port}")
                                if udp_listener_active and our_stun_discovered_udp_ip:
                                    asyncio.create_task(start_udp_hole_punch(peer_ip, int(peer_port), peer_id))
                                else: print(f"Worker '{worker_id}': Cannot P2P, UDP listener/STUN info not ready.")
                        elif msg_type == "udp_endpoint_ack": print(f"Worker '{worker_id}': UDP Endpoint Ack: {message_data.get('status')}")
                        elif msg_type == "echo_response": print(f"Worker '{worker_id}': Echo Response: {message_data.get('processed_by_rendezvous')}")
                        else: print(f"Worker '{worker_id}': Unhandled message from Rendezvous: {msg_type}")
                    except asyncio.TimeoutError: pass
                    except websockets.exceptions.ConnectionClosed: print(f"Worker '{worker_id}': Rendezvous WS closed by server."); break
                    except Exception as e_recv: print(f"Error in WS recv loop: {e_recv}"); break
                print(f"WORKER '{worker_id}': Exited main WebSocket receive loop.")
                if ws_to_rendezvous.state == ConnectionState.CLOSED : break # CORRECTED STATE CHECK
        except Exception as e_ws_connect: print(f"Worker '{worker_id}': Error in WS connection loop: {type(e_ws_connect).__name__} - {e_ws_connect}. Retrying...")
        finally: 
            if p2p_listener_transport_local_ref: 
                print(f"Worker '{worker_id}': Closing local P2P UDP transport from this WS session.")
                p2p_listener_transport_local_ref.close()
                if p2p_udp_transport == p2p_listener_transport_local_ref: p2p_udp_transport = None
                udp_listener_active = False
        if not stop_signal_received: await asyncio.sleep(10)
        else: break

async def main_async_orchestrator():
    loop = asyncio.get_running_loop()
    main_server = await websockets_serve(
        ui_websocket_handler, 
        "0.0.0.0", 
        HTTP_PORT_FOR_UI,
        process_request=process_http_request,
        ping_interval=20,
        ping_timeout=20
    )
    print(f"Worker '{worker_id}': HTTP & UI WebSocket server listening on 0.0.0.0:{HTTP_PORT_FOR_UI}")
    print(f"  - Serving index.html at '/'")
    print(f"  - UI WebSocket at '/ui_ws'")
    print(f"  - Health check at '/health'")

    rendezvous_base_url_env = os.environ.get("RENDEZVOUS_SERVICE_URL")
    if not rendezvous_base_url_env: 
        print("CRITICAL: RENDEZVOUS_SERVICE_URL missing in main_async_runner. Worker cannot start rendezvous client.")
        # Decide if server should run without rendezvous client, for now, it will proceed and connect_to_rendezvous will fail/log
    
    full_rendezvous_ws_url = ""
    if rendezvous_base_url_env: # Only construct if not None
        ws_scheme = "wss" if rendezvous_base_url_env.startswith("https://") else "ws"
        base_url_no_scheme = rendezvous_base_url_env.replace("https://", "").replace("http://", "")
        full_rendezvous_ws_url = f"{ws_scheme}://{base_url_no_scheme}/ws/register/{worker_id}"
    
    rendezvous_client_task = asyncio.create_task(connect_to_rendezvous(full_rendezvous_ws_url))

    try:
        await rendezvous_client_task 
    except asyncio.CancelledError:
        print(f"Worker '{worker_id}': Rendezvous client task was cancelled.")
    finally:
        main_server.close()
        await main_server.wait_closed()
        print(f"Worker '{worker_id}': Main HTTP/UI WebSocket server stopped.")
        if p2p_udp_transport: 
            p2p_udp_transport.close()
            print(f"Worker '{worker_id}': P2P UDP transport closed from main_async_orchestrator finally.")

if __name__ == "__main__":
    print(f"WORKER SCRIPT (ID: {worker_id}): Initializing...")
    
    # The main_async_orchestrator will start the unified HTTP/WebSocket server.
    # No separate threaded health check server needed anymore.

    rendezvous_base_url_env = os.environ.get("RENDEZVOUS_SERVICE_URL")
    if not rendezvous_base_url_env:
        print("CRITICAL ERROR: RENDEZVOUS_SERVICE_URL environment variable not set. Exiting worker.")
        exit(1) 

    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)

    try:
        asyncio.run(main_async_orchestrator())
    except KeyboardInterrupt: 
        print(f"Worker '{worker_id}' interrupted by user.")
        stop_signal_received = True 
    except Exception as e_main_run: 
        print(f"Worker '{worker_id}' CRITICAL ERROR in __main__: {type(e_main_run).__name__} - {e_main_run}")
    finally: 
        print(f"Worker '{worker_id}' main EXIT.") 