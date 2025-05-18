import asyncio
import os
import uuid
import websockets # For Rendezvous client AND UI server
import aiohttp
import signal
import http.server 
import socketserver 
import requests 
import json
import socket
import stun # pystun3
import sys # Added for sys.exit()
from typing import Optional, Tuple, Set, Dict, Callable, List, Any # Added Callable and List
from pathlib import Path
from websockets.server import serve as websockets_serve
from websockets.http import Headers
import time # For benchmark timing
import base64 # For encoding benchmark payload
import functools # ADDED for partial

# --- QUIC Imports ---
# from aioquic.quic.configuration import QuicConfiguration # Moved to quic_tunnel.py
# from aioquic.quic.connection import QuicConnection # Moved to quic_tunnel.py
# from aioquic.quic.events import StreamDataReceived, HandshakeCompleted, ConnectionTerminated # Moved to quic_tunnel.py
# from aioquic.tls import SessionTicket # Optional for session resumption

# Additional imports for runtime self-signed certificate generation # Moved to quic_tunnel.py
# from cryptography import x509 # Moved to quic_tunnel.py
# from cryptography.x509.oid import NameOID # Moved to quic_tunnel.py
# from cryptography.hazmat.primitives import hashes, serialization # Moved to quic_tunnel.py
# from cryptography.hazmat.primitives.asymmetric import rsa # Moved to quic_tunnel.py
# import datetime # Moved to quic_tunnel.py

from quic_tunnel import QuicTunnel
from network_utils import is_probable_quic_datagram, download_benchmark_file, discover_stun_endpoint, benchmark_send_udp_data
from ui_server import process_http_request, ui_websocket_handler, broadcast_to_all_ui_clients # MODIFIED IMPORT
from p2p_protocol import P2PUDPProtocol # ADDED
# Import the rendezvous client functions
from rendezvous_client import connect_to_rendezvous
import rendezvous_client as rendezvous_client_module # NEW

# --- Global Variables ---
worker_id = str(uuid.uuid4())
stop_signal_received = False
active_rendezvous_websocket: Optional[websockets.WebSocketClientProtocol] = None # Added for explicit deregister
p2p_udp_transport: Optional[asyncio.DatagramTransport] = None
p2p_protocol_instance: Optional[P2PUDPProtocol] = None  # Keep reference to the P2P protocol for QUIC routing
our_stun_discovered_udp_ip: Optional[str] = None
our_stun_discovered_udp_port: Optional[int] = None
current_p2p_peer_id: Optional[str] = None
current_p2p_peer_addr: Optional[Tuple[str, int]] = None

DEFAULT_STUN_HOST = os.environ.get("STUN_HOST", "stun.l.google.com")
DEFAULT_STUN_PORT = int(os.environ.get("STUN_PORT", "19302"))
INTERNAL_UDP_PORT = int(os.environ.get("INTERNAL_UDP_PORT", "8081"))
HTTP_PORT_FOR_UI = int(os.environ.get("PORT", 8080))

# ui_websocket_clients: Set[websockets.WebSocketServerProtocol] = set() # REMOVED - managed in ui_server.py

# Benchmark related globals
benchmark_sessions: Dict[str, Dict] = {} # Key: peer_addr_str, Value: {received_bytes, received_chunks, start_time, total_chunks (from sender)}
BENCHMARK_CHUNK_SIZE = int(os.environ.get("BENCHMARK_CHUNK_SIZE", "1024")) # Read from env, default 1KB
BENCHMARK_FILE_URL = os.environ.get("BENCHMARK_GCS_URL")

# --- QUIC Tunnel Globals ---
quic_engine: Optional[QuicTunnel] = None # Updated type hint
# IMPORTANT: Define where the tunnelled TCP data should go locally.
# Ensure a service is listening on this host/port.
# LOCAL_QUIC_RELAY_HOST = "127.0.0.1" # Moved to quic_tunnel.py and imported
# LOCAL_QUIC_RELAY_PORT = int(os.environ.get("LOCAL_QUIC_RELAY_PORT", "9000")) # Moved to quic_tunnel.py and imported

# ---------------------------------------------------------------------------
# Optional built-in TCP echo service
# ---------------------------------------------------------------------------
# For quick smoke-tests you can start each worker with:
#   ENABLE_QUIC_ECHO_SERVER=1 LOCAL_QUIC_RELAY_PORT=9000 python main.py
# This launches a tiny asyncio.TCPServer that simply echoes back whatever it
# receives.  Production deployments should disable this and run the real
# service behind the tunnel instead.
# ---------------------------------------------------------------------------
ENABLE_QUIC_ECHO_SERVER = os.environ.get("ENABLE_QUIC_ECHO_SERVER", "1") == "1"

# Keep a global reference so we can close the server on shutdown.
echo_server: Optional[asyncio.AbstractServer] = None

# --- Certificate paths (ensure these files exist) --- # Moved to quic_tunnel.py
# For testing, generate self-signed certs:
# openssl req -x509 -newkey rsa:2048 -nodes -days 365 -keyout key.pem -out cert.pem -subj "/CN=localhost"
# CERTIFICATE_FILE = Path(__file__).parent / "cert.pem" # Moved to quic_tunnel.py
# PRIVATE_KEY_FILE = Path(__file__).parent / "key.pem" # Moved to quic_tunnel.py

# --- AppContext Class for sharing state with other modules ---
class AppContext:
    def __init__(self):
        self.worker_id = worker_id
        self.INTERNAL_UDP_PORT = INTERNAL_UDP_PORT
        self.DEFAULT_STUN_HOST = DEFAULT_STUN_HOST
        self.DEFAULT_STUN_PORT = DEFAULT_STUN_PORT
        
        # Direct module/class references
        self.discover_stun_endpoint = discover_stun_endpoint
        self.QuicTunnel = QuicTunnel # Pass the class itself
        self.broadcast_to_all_ui_clients = broadcast_to_all_ui_clients
        self.P2PUDPProtocol = P2PUDPProtocol # Pass the class itself
        self.BENCHMARK_CHUNK_SIZE = int(os.environ.get("BENCHMARK_CHUNK_SIZE", "1024")) # Read here
        self.BENCHMARK_FILE_URL = os.environ.get("BENCHMARK_GCS_URL") # Read here
        self.LOCAL_QUIC_RELAY_PORT = int(os.environ.get("LOCAL_QUIC_RELAY_PORT", "9000")) # Added
        self.LOCAL_QUIC_RELAY_HOST = "127.0.0.1" # Added

        # Use the benchmark sender from network_utils (5-argument signature)
        import network_utils as _nu
        self.benchmark_send_udp_data = (
            lambda ip, port, size_kb, ui_ws: _nu.benchmark_send_udp_data(self, ip, port, size_kb, ui_ws)
        )
        # Provide a getter for the current QUIC engine instance for UI server
        self.get_main_quic_engine = self.get_quic_engine # Expose the existing getter

        # Callbacks for P2PProtocol factory
        self.p2p_protocol_factory = lambda: self.P2PUDPProtocol(
            worker_id_val=self.worker_id,
            internal_udp_port_val=self.INTERNAL_UDP_PORT,
            get_current_p2p_peer_id_func=self.get_current_p2p_peer_id,
            get_current_p2p_peer_addr_func=self.get_current_p2p_peer_addr,
            get_quic_engine_func=self.get_quic_engine,
            set_quic_engine_func=self.set_quic_engine,
            set_main_p2p_udp_transport_func=self.set_p2p_transport
        )

    # Getters for global state
    def get_stop_signal(self) -> bool: return stop_signal_received
    def get_p2p_transport(self) -> Optional[asyncio.DatagramTransport]: return p2p_udp_transport
    def get_stun_discovered_ip(self) -> Optional[str]: return our_stun_discovered_udp_ip
    def get_stun_discovered_port(self) -> Optional[int]: return our_stun_discovered_udp_port
    def get_active_rendezvous_websocket(self) -> Optional[websockets.WebSocketClientProtocol]: return active_rendezvous_websocket
    def get_quic_engine(self) -> Optional[QuicTunnel]: return quic_engine
    def get_current_p2p_peer_id(self) -> Optional[str]: return current_p2p_peer_id
    def get_current_p2p_peer_addr(self) -> Optional[Tuple[str, int]]: return current_p2p_peer_addr

    # Setters for global state (use with caution, ensure they are called appropriately)
    def set_p2p_transport(self, transport: Optional[asyncio.DatagramTransport]):
        global p2p_udp_transport
        if transport is None: # Transport is being reported as lost
            if p2p_udp_transport is not None: 
                 print(f"Worker '{self.worker_id}': P2PUDPProtocol reported its transport lost. Clearing global p2p_udp_transport.")
            p2p_udp_transport = None
        else: # Transport is being established
            p2p_udp_transport = transport

    def set_stun_discovered_ip(self, ip: Optional[str]): 
        global our_stun_discovered_udp_ip
        our_stun_discovered_udp_ip = ip
    def set_stun_discovered_port(self, port: Optional[int]): 
        global our_stun_discovered_udp_port
        our_stun_discovered_udp_port = port
    def set_active_rendezvous_websocket(self, ws: Optional[websockets.WebSocketClientProtocol]):
        global active_rendezvous_websocket
        active_rendezvous_websocket = ws
    def set_quic_engine(self, engine: Optional[QuicTunnel]):
        global quic_engine
        quic_engine = engine
    def set_current_p2p_peer_id(self, peer_id: Optional[str]):
        global current_p2p_peer_id
        current_p2p_peer_id = peer_id
    def set_current_p2p_peer_addr(self, addr: Optional[Tuple[str, int]]):
        global current_p2p_peer_addr
        current_p2p_peer_addr = addr
# --- End AppContext ---

# --- Callbacks for P2PUDPProtocol (no longer needed here, AppContext provides them) ---
# def get_current_p2p_peer_id_cb() -> Optional[str]: return current_p2p_peer_id
# ... and other _cb functions ...
# def set_main_p2p_udp_transport_cb(new_transport: Optional[asyncio.DatagramTransport]):
# ...
# --- End Callbacks for P2PUDPProtocol ---

def update_main_stun_globals(ip: Optional[str], port: Optional[int]):
    # This function is now effectively replaced by app_context.set_stun_discovered_ip/port
    # It was used by ui_websocket_handler, which will now use context methods if needed or have STUN info passed directly.
    global our_stun_discovered_udp_ip, our_stun_discovered_udp_port
    our_stun_discovered_udp_ip = ip
    our_stun_discovered_udp_port = port
    print(f"Worker '{worker_id}': Updated STUN globals via update_main_stun_globals: IP={ip}, Port={port}")

def get_p2p_info_for_ui() -> Tuple[Optional[str], Optional[asyncio.DatagramTransport], Optional[Tuple[str, int]]]:
    return current_p2p_peer_id, p2p_udp_transport, current_p2p_peer_addr

def get_rendezvous_ws_for_ui() -> Optional[websockets.WebSocketClientProtocol]:
    return active_rendezvous_websocket

def handle_shutdown_signal(signum, frame):
    global stop_signal_received, p2p_udp_transport, quic_engine, worker_id # Added worker_id
    print(f"Shutdown signal ({signum}) received. Worker '{worker_id}' attempting graceful shutdown.")
    stop_signal_received = True
    if p2p_udp_transport:
        try: p2p_udp_transport.close(); print(f"Worker '{worker_id}': P2P UDP transport closed.")
        except Exception as e: print(f"Worker '{worker_id}': Error closing P2P UDP transport: {e}")
        p2p_udp_transport = None 
    if quic_engine:
        engine_to_close = quic_engine
        quic_engine = None
        if p2p_protocol_instance and p2p_protocol_instance.associated_quic_tunnel is engine_to_close:
            p2p_protocol_instance.associated_quic_tunnel = None
        print(f"Worker '{worker_id}': Cleared global QUIC engine reference, scheduling close for {engine_to_close.peer_addr}.")
        asyncio.create_task(engine_to_close.close())
    # UI clients are managed in ui_server.py; their individual closing is handled there or by server shutdown.
    # The old loop: `for ws_client in list(ui_websocket_clients): asyncio.create_task(ws_client.close(reason="Server shutting down"))`
    # is implicitly handled by the server closing. ui_server.ui_websocket_handler also removes clients on disconnect.

async def process_http_request(path: str, request_headers: Headers) -> Optional[Tuple[int, Headers, bytes]]:
    if path == "/ui_ws": return None  
    if path == "/":
        try:
            html_path = Path(__file__).parent / "index.html"
            with open(html_path, "rb") as f: content = f.read()
            headers = Headers([("Content-Type", "text/html"), ("Content-Length", str(len(content)))])
            return (200, headers, content)
        except FileNotFoundError: return (404, [("Content-Type", "text/plain")], b"index.html not found")
        except Exception as e_file: print(f"Error serving index.html: {e_file}"); return (500, [("Content-Type", "text/plain")], b"Internal Server Error")
    elif path == "/health": return (200, [("Content-Type", "text/plain")], b"OK")
    else: return (404, [("Content-Type", "text/plain")], b"Not Found")


async def benchmark_send_udp_data(target_ip: str, target_port: int, _size_kb: int, ui_ws: websockets.WebSocketServerProtocol):
    """Download a file from GCS and send it to the peer over UDP."""
    global worker_id, p2p_udp_transport, BENCHMARK_FILE_URL, current_p2p_peer_addr # Added current_p2p_peer_addr

    if not (p2p_udp_transport and current_p2p_peer_addr):
        err_msg = "P2P UDP transport or peer address not available for benchmark."
        print(f"Worker '{worker_id}': {err_msg}")
        await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Error: {err_msg}"}))
        return

    if not BENCHMARK_FILE_URL:
        err_msg = "BENCHMARK_GCS_URL not set."
        print(f"Worker '{worker_id}': {err_msg}")
        await ui_ws.send(json.dumps({"type": "benchmark_status", "message": err_msg}))
        return

    print(f"Worker '{worker_id}': Starting benchmark download from {BENCHMARK_FILE_URL}")
    await ui_ws.send(json.dumps({"type": "benchmark_status", "message": "Downloading benchmark file..."}))

    start_download = time.monotonic()
    try:
        file_bytes = await download_benchmark_file(BENCHMARK_FILE_URL) 
    except Exception as e:
        err_msg = f"Download failed: {type(e).__name__} - {e}"
        print(f"Worker '{worker_id}': {err_msg}")
        await ui_ws.send(json.dumps({"type": "benchmark_status", "message": err_msg}))
        return
    download_time = time.monotonic() - start_download
    file_size_kb = len(file_bytes) / 1024
    await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Downloaded {file_size_kb/1024:.2f} MB in {download_time:.2f}s"}))

    num_chunks = (len(file_bytes) + BENCHMARK_CHUNK_SIZE - 1) // BENCHMARK_CHUNK_SIZE

    print(f"Worker '{worker_id}': Sending {len(file_bytes)} bytes in {num_chunks} chunks to {target_ip}:{target_port}")
    await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Starting UDP send of {file_size_kb/1024:.2f} MB..."}))

    start_time = time.monotonic()
    bytes_sent = 0

    try:
        for i in range(num_chunks):
            if stop_signal_received or ui_ws.closed:
                print(f"Worker '{worker_id}': Benchmark send cancelled (stop_signal or UI disconnected).")
                await ui_ws.send(json.dumps({"type": "benchmark_status", "message": "Benchmark send cancelled."}))
                break

            chunk = file_bytes[i * BENCHMARK_CHUNK_SIZE : (i + 1) * BENCHMARK_CHUNK_SIZE]
            seq_header = i.to_bytes(4, "big")
            packet = seq_header + chunk
            if p2p_udp_transport: # Check if transport still exists
                p2p_udp_transport.sendto(packet, (target_ip, target_port))
                bytes_sent += len(packet)
            else:
                print(f"Worker '{worker_id}': Benchmark send error: P2P UDP transport is not available.")
                await ui_ws.send(json.dumps({"type": "benchmark_status", "message": "Error: P2P UDP transport lost."}))
                break    
            if (i + 1) % (num_chunks // 10 if num_chunks >= 10 else 1) == 0:
                progress_msg = f"Benchmark Send: Sent {i+1}/{num_chunks} chunks ({bytes_sent / 1024:.2f} KB)..."
                print(f"Worker '{worker_id}': {progress_msg}")
                await ui_ws.send(json.dumps({"type": "benchmark_status", "message": progress_msg}))
        else:
            if p2p_udp_transport: # Check again before sending end marker
                end_marker = (0xFFFFFFFF).to_bytes(4, "big")
                p2p_udp_transport.sendto(end_marker, (target_ip, target_port))
                print(f"Worker '{worker_id}': Sent benchmark_end marker to {target_ip}:{target_port}")

                end_time = time.monotonic()
                duration = end_time - start_time
                throughput_kbps = (bytes_sent / 1024) / duration if duration > 0 else 0
                final_msg = (
                    f"Benchmark Send Complete: {file_size_kb/1024:.2f} MB sent in {duration:.2f}s (download {download_time:.2f}s)." \
                    f" Throughput: {throughput_kbps:.2f} KB/s"
                )
                print(f"Worker '{worker_id}': {final_msg}")
                await ui_ws.send(json.dumps({"type": "benchmark_status", "message": final_msg}))

    except Exception as e:
        error_msg = f"Benchmark Send Error: {type(e).__name__} - {e}"
        print(f"Worker '{worker_id}': {error_msg}")
        if not ui_ws.closed:
            await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Error: {error_msg}"}))

# async def ui_websocket_handler(websocket: websockets.WebSocketServerProtocol, path: str): # REMOVED - moved to ui_server.py
# ... (entire function body removed)

async def delayed_exit(delay_seconds: float):
    await asyncio.sleep(delay_seconds)
    print(f"Worker '{worker_id}': Executing delayed exit (forcing process termination).")
    os._exit(0)


async def start_udp_hole_punch(peer_udp_ip: str, peer_udp_port: int, peer_worker_id: str):
    global worker_id, stop_signal_received, p2p_udp_transport, current_p2p_peer_addr, current_p2p_peer_id, quic_engine

    if not p2p_udp_transport: 
        print(f"Worker '{worker_id}': UDP transport not ready for hole punch to '{peer_worker_id}'."); 
        return

    print(f"Worker '{worker_id}': Initiating P2P connection. Previous peer ID: '{current_p2p_peer_id}', Previous peer addr: {current_p2p_peer_addr}.")
    
    if quic_engine and (quic_engine.peer_addr != (peer_udp_ip, peer_udp_port) or current_p2p_peer_id != peer_worker_id):
        print(f"Worker '{worker_id}': Peer changed or re-initiating. Closing existing QUIC tunnel with {quic_engine.peer_addr}.")
        old_engine = quic_engine
        quic_engine = None 
        asyncio.create_task(old_engine.close())

    current_p2p_peer_id = peer_worker_id
    current_p2p_peer_addr = (peer_udp_ip, peer_udp_port)
    print(f"Worker '{worker_id}': Set new P2P target. Current peer ID: '{current_p2p_peer_id}', Current peer addr: {current_p2p_peer_addr}.")

    local_bind_addr_tuple = p2p_udp_transport.get_extra_info('sockname') if p2p_udp_transport else ("unknown", "unknown")
    print(f"Worker '{worker_id}': Starting UDP hole punch PINGs towards '{peer_worker_id}' at {current_p2p_peer_addr}")
    for i in range(1, 4):
        if stop_signal_received: break
        try:
            message_content = f"P2P_HOLE_PUNCH_PING_FROM_{worker_id}_NUM_{i}"
            if p2p_udp_transport: p2p_udp_transport.sendto(message_content.encode(), current_p2p_peer_addr)
            print(f"Worker '{worker_id}': Sent UDP Hole Punch PING {i} to {current_p2p_peer_addr}")
        except Exception as e: print(f"Worker '{worker_id}': Error sending UDP Hole Punch PING {i}: {e}")
        await asyncio.sleep(0.5)
    print(f"Worker '{worker_id}': Finished UDP Hole Punch PING burst to '{peer_worker_id}'.")

    if not quic_engine:
        is_quic_client_role = worker_id > current_p2p_peer_id 

        if is_quic_client_role:
            print(f"Worker '{worker_id}': Initializing QUIC tunnel (client) with peer {current_p2p_peer_addr}")

            def udp_sender_for_quic(data_to_send: bytes, destination_addr: Tuple[str, int]):
                if p2p_udp_transport and not p2p_udp_transport.is_closing():
                    p2p_udp_transport.sendto(data_to_send, destination_addr)
                else:
                    print(f"Worker '{worker_id}': QUIC UDP sender: P2P transport not available or closing. Cannot send.")

            def client_tunnel_on_close():
                global quic_engine, p2p_protocol_instance
                if p2p_protocol_instance and p2p_protocol_instance.associated_quic_tunnel is quic_engine:
                    p2p_protocol_instance.associated_quic_tunnel = None
                quic_engine = None

            quic_engine = QuicTunnel(
                worker_id_val=worker_id,
                peer_addr_val=current_p2p_peer_addr,
                udp_sender_func=udp_sender_for_quic,
                is_client_role=True,
                on_close_callback=client_tunnel_on_close,
            )
            if p2p_protocol_instance:
                p2p_protocol_instance.associated_quic_tunnel = quic_engine
            await asyncio.sleep(0.5)
            await quic_engine.connect_if_client()
            print(f"Worker '{worker_id}': QUIC engine setup initiated for peer {current_p2p_peer_addr}. Role: Client")
        else:
            print(f"Worker '{worker_id}': Acting as QUIC Server – will create tunnel on first inbound Initial packet.")

    # for ui_client_ws in list(ui_websocket_clients): # OLD BROADCAST
    #     asyncio.create_task(ui_client_ws.send(json.dumps({"type": "p2p_status_update", "message": f"P2P link attempt initiated with {peer_worker_id[:8]}...", "peer_id": peer_worker_id}))) # OLD BROADCAST
    asyncio.create_task(broadcast_to_all_ui_clients({"type": "p2p_status_update", "message": f"P2P link attempt initiated with {peer_worker_id[:8]}...", "peer_id": peer_worker_id})) # NEW BROADCAST

async def attempt_hole_punch_when_ready(peer_udp_ip: str, peer_udp_port: int, peer_worker_id: str, max_wait_sec: float = 10.0, check_interval: float = 0.5):
    global p2p_udp_transport, stop_signal_received, worker_id
    waited = 0.0
    while not stop_signal_received and waited < max_wait_sec:
        if p2p_udp_transport:
            await start_udp_hole_punch(peer_udp_ip, peer_udp_port, peer_worker_id)
            return
        await asyncio.sleep(check_interval)
        waited += check_interval
    print(f"Worker '{worker_id}': Gave up waiting ({waited:.1f}s) for UDP listener to become active before initiating hole-punch to '{peer_worker_id}'.")

async def connect_to_rendezvous(rendezvous_ws_url: str):
    global stop_signal_received, p2p_udp_transport, INTERNAL_UDP_PORT, our_stun_discovered_udp_ip, our_stun_discovered_udp_port, active_rendezvous_websocket, quic_engine
    global worker_id, DEFAULT_STUN_HOST, DEFAULT_STUN_PORT 
    ip_echo_service_url = "https://api.ipify.org"
    ping_interval = float(os.environ.get("PING_INTERVAL_SEC", "25"))
    ping_timeout = float(os.environ.get("PING_TIMEOUT_SEC", "25"))
    STUN_RECHECK_INTERVAL = float(os.environ.get("STUN_RECHECK_INTERVAL_SEC", "300"))
    udp_listener_active = False
    loop = asyncio.get_running_loop()
    last_stun_recheck_time = 0.0

    protocol_factory = lambda: P2PUDPProtocol(
        worker_id_val=worker_id,
        internal_udp_port_val=INTERNAL_UDP_PORT,
        get_current_p2p_peer_id_func=get_current_p2p_peer_id_cb,
        get_current_p2p_peer_addr_func=get_current_p2p_peer_addr_cb,
        get_quic_engine_func=get_quic_engine_cb,
        set_quic_engine_func=set_quic_engine_cb,
        set_main_p2p_udp_transport_func=set_main_p2p_udp_transport_cb
    )

    while not stop_signal_received:
        p2p_listener_transport_local_ref = None
        active_rendezvous_websocket = None 
        try:
            async with websockets.connect(rendezvous_ws_url, 
                                        ping_interval=ping_interval, 
                                        ping_timeout=ping_timeout,
                                        proxy=None) as ws_to_rendezvous:
                active_rendezvous_websocket = ws_to_rendezvous 
                print(f"Worker '{worker_id}' connected to Rendezvous Service.")
                try:
                    response = requests.get(ip_echo_service_url, timeout=10)
                    response.raise_for_status()
                    http_public_ip = response.text.strip()
                    await ws_to_rendezvous.send(json.dumps({"type": "register_public_ip", "ip": http_public_ip}))
                    print(f"Worker '{worker_id}' sent HTTP-based IP ({http_public_ip}) to Rendezvous.")
                except Exception as e_http_ip: print(f"Worker '{worker_id}': Error sending HTTP IP: {e_http_ip}")
                
                stun_result_initial = await discover_stun_endpoint(worker_id, INTERNAL_UDP_PORT, DEFAULT_STUN_HOST, DEFAULT_STUN_PORT)
                stun_success_initial = False
                if stun_result_initial:
                    our_stun_discovered_udp_ip, our_stun_discovered_udp_port = stun_result_initial
                    try:
                        await ws_to_rendezvous.send(
                            json.dumps({
                                "type": "update_udp_endpoint",
                                "udp_ip": our_stun_discovered_udp_ip,
                                "udp_port": our_stun_discovered_udp_port,
                            })
                        )
                        print(f"Worker '{worker_id}': Sent STUN UDP endpoint ({our_stun_discovered_udp_ip}:{our_stun_discovered_udp_port}) to Rendezvous.")
                        # The original code sent a "system_message" to the UI websocket here. 
                        # That websocket might not exist yet or be the right one for this context.
                        # Removing this specific UI broadcast from here, as it was conditional on discover_and_report_stun_udp_endpoint's old structure.
                        # The UI handler itself now messages the specific UI client on STUN redial.
                        stun_success_initial = True
                    except Exception as send_e:
                        print(f"Worker '{worker_id}': Error sending STUN endpoint to Rendezvous: {type(send_e).__name__} - {send_e}")
                        stun_success_initial = True 
                else:
                    print(f"Worker '{worker_id}': Initial STUN discovery failed to get an endpoint.")

                last_stun_recheck_time = time.monotonic()

                if stun_success_initial and not udp_listener_active:
                    try:
                        _transport, _protocol = await loop.create_datagram_endpoint(
                            protocol_factory,
                            local_addr=('0.0.0.0', INTERNAL_UDP_PORT)
                        )
                        p2p_listener_transport_local_ref = _transport
                        p2p_protocol_instance = _protocol  # store protocol for future association
                        # p2p_udp_transport is now set by the callback
                        await asyncio.sleep(0.1)
                        if p2p_udp_transport: print(f"Worker '{worker_id}': Asyncio P2P UDP listener appears active on 0.0.0.0:{INTERNAL_UDP_PORT}.")
                        else: print(f"Worker '{worker_id}': P2P UDP listener transport not set globally after create_datagram_endpoint on 0.0.0.0:{INTERNAL_UDP_PORT}.")
                        udp_listener_active = True
                    except Exception as e_udp_listen:
                        print(f"Worker '{worker_id}': Failed to create P2P UDP datagram endpoint on 0.0.0.0:{INTERNAL_UDP_PORT}: {e_udp_listen}")
                
                while not stop_signal_received:
                    try:
                        message_raw = await asyncio.wait_for(ws_to_rendezvous.recv(), timeout=ping_interval) 
                        print(f"Worker '{worker_id}': Message from Rendezvous: {message_raw}")
                        message_data = json.loads(message_raw)
                        msg_type = message_data.get("type")
                        if msg_type == "p2p_connection_offer":
                            peer_id = message_data.get("peer_worker_id")
                            peer_ip = message_data.get("peer_udp_ip")
                            peer_port = message_data.get("peer_udp_port")
                            if peer_id and peer_ip and peer_port:
                                print(f"Worker '{worker_id}': Received P2P offer for peer '{peer_id}' at {peer_ip}:{peer_port}")
                                asyncio.create_task(attempt_hole_punch_when_ready(peer_ip, int(peer_port), peer_id))
                        elif msg_type == "udp_endpoint_ack": print(f"Worker '{worker_id}': UDP Endpoint Ack: {message_data.get('status')}")
                        elif msg_type == "echo_response": print(f"Worker '{worker_id}': Echo Response: {message_data.get('processed_by_rendezvous')}")
                        else: print(f"Worker '{worker_id}': Unhandled message from Rendezvous: {msg_type}")
                    except asyncio.TimeoutError: pass
                    except websockets.exceptions.ConnectionClosed as e_conn_closed: 
                        print(f"Worker '{worker_id}': Rendezvous WS closed by server during recv: {e_conn_closed}")
                        break 
                    except Exception as e_recv: 
                        print(f"Worker '{worker_id}': Error in WS recv loop: {e_recv}") 
                        break 
                    
                    current_time = time.monotonic()
                    if current_time - last_stun_recheck_time > STUN_RECHECK_INTERVAL:
                        print(f"Worker '{worker_id}': Performing periodic STUN UDP endpoint check (interval: {STUN_RECHECK_INTERVAL}s)...")
                        try:
                            old_ip, old_port = our_stun_discovered_udp_ip, our_stun_discovered_udp_port
                            stun_recheck_result = await discover_stun_endpoint(worker_id, INTERNAL_UDP_PORT, DEFAULT_STUN_HOST, DEFAULT_STUN_PORT)
                            if stun_recheck_result:
                                new_ip, new_port = stun_recheck_result
                                if new_ip != old_ip or new_port != old_port or old_ip is None:
                                    our_stun_discovered_udp_ip, our_stun_discovered_udp_port = new_ip, new_port
                                    print(f"Worker '{worker_id}': Periodic STUN: UDP endpoint changed from {old_ip}:{old_port} to {new_ip}:{new_port}. Update will be sent.")
                                else:
                                    print(f"Worker '{worker_id}': Periodic STUN: UDP endpoint re-verified and unchanged: {new_ip}:{new_port}. Update will be (re)sent.")
                                
                                try:
                                    await ws_to_rendezvous.send(
                                        json.dumps({
                                            "type": "update_udp_endpoint",
                                            "udp_ip": new_ip, 
                                            "udp_port": new_port,
                                        })
                                    )
                                    print(f"Worker '{worker_id}': Sent updated STUN UDP endpoint ({new_ip}:{new_port}) to Rendezvous.")
                                except Exception as send_e:
                                    print(f"Worker '{worker_id}': Error sending updated STUN endpoint to Rendezvous: {type(send_e).__name__} - {send_e}")
                            else:
                                print(f"Worker '{worker_id}': Periodic STUN UDP endpoint discovery failed logically. Will retry after interval.")
                            last_stun_recheck_time = current_time
                        except websockets.exceptions.ConnectionClosed as e_stun_conn_closed:
                            print(f"Worker '{worker_id}': Connection closed during periodic STUN update: {e_stun_conn_closed}. Will attempt to reconnect.")
                            break 
                        except Exception as e_stun_general:
                            print(f"Worker '{worker_id}': Error during periodic STUN UDP endpoint check: {type(e_stun_general).__name__} - {e_stun_general}. Will retry STUN after interval.")
                            last_stun_recheck_time = current_time 
                if stop_signal_received: break 
        except websockets.exceptions.ConnectionClosed as e_outer_closed:
            print(f"Worker '{worker_id}': Rendezvous WS connection closed before or during connect: {e_outer_closed}")
        except Exception as e_ws_connect: 
            print(f"Worker '{worker_id}': Error in WS connection loop: {type(e_ws_connect).__name__} - {e_ws_connect}. Retrying...")
        finally: 
            active_rendezvous_websocket = None
            if p2p_listener_transport_local_ref:
                print(f"Worker '{worker_id}': Closing local P2P UDP transport (asyncio wrapper) from this WS session.")
                # p2p_listener_transport_local_ref.close() # This is tricky if it's also the global one managed by P2PUDPProtocol
                # The callback set_main_p2p_udp_transport_cb(None) should be called by P2PUDPProtocol.connection_lost
                # if this transport instance is indeed lost.
                # If this specific p2p_listener_transport_local_ref needs explicit closing here,
                # ensure it doesn't conflict with P2PUDPProtocol's management.
                # For now, relying on P2PUDPProtocol.connection_lost to clear the global p2p_udp_transport via callback.
                pass # Let P2PUDPProtocol's connection_lost handle its own transport closure and callback.
                udp_listener_active = False
                p2p_protocol_instance = None
        if not stop_signal_received: await asyncio.sleep(10)
        else: break

async def main_async_orchestrator():
    global p2p_udp_transport
    loop = asyncio.get_running_loop()
    app_context = AppContext() # Create the application context

    # Prepare the UI websocket handler with necessary context from main.py
    # The ui_websocket_handler in ui_server.py now takes specific args, not AppContext directly for this part.
    # We need to make sure its arguments are correctly sourced from AppContext or globals for clarity.
    
    # The ui_websocket_handler was defined to take:
    # worker_id_val, get_p2p_info, get_rendezvous_ws, config_*, benchmark_sender_func, delayed_exit_caller, discover_stun_caller, update_main_stun_globals_func
    # ADDING: get_main_quic_engine_func

    partial_ui_ws_handler = functools.partial(
        ui_websocket_handler,
        worker_id_val=app_context.worker_id,
        get_p2p_info=get_p2p_info_for_ui, # Uses globals, could be app_context methods
        get_rendezvous_ws=get_rendezvous_ws_for_ui, # Uses globals, could be app_context methods
        config_internal_udp_port=app_context.INTERNAL_UDP_PORT,
        config_default_stun_host=app_context.DEFAULT_STUN_HOST,
        config_default_stun_port=app_context.DEFAULT_STUN_PORT,
        benchmark_sender_func=app_context.benchmark_send_udp_data, # Use the pre-partialed one from context
        delayed_exit_caller=delayed_exit, # Global function
        discover_stun_caller=app_context.discover_stun_endpoint, # From context
        update_main_stun_globals_func=update_main_stun_globals, # Global function (could be context method)
        get_quic_engine_global_func=app_context.get_main_quic_engine # NEW: Pass the getter for QUIC engine
    )

    main_server = await websockets_serve(
        partial_ui_ws_handler, "0.0.0.0", HTTP_PORT_FOR_UI,
        process_request=process_http_request, # process_http_request is simple, doesn't need context
        ping_interval=20, ping_timeout=20
    )
    print(f"Worker '{worker_id}': HTTP & UI WebSocket server listening on 0.0.0.0:{HTTP_PORT_FOR_UI}")
    print(f"  - Serving index.html at '/'")
    print(f"  - UI WebSocket at '/ui_ws'")
    print(f"  - Health check at '/health'")
    rendezvous_base_url_env = os.environ.get("RENDEZVOUS_SERVICE_URL")
    if not rendezvous_base_url_env: print("CRITICAL: RENDEZVOUS_SERVICE_URL missing."); sys.exit(1)
    full_rendezvous_ws_url = ""
    if rendezvous_base_url_env: 
        ws_scheme = "wss" if rendezvous_base_url_env.startswith("https://") else "ws"
        base_url_no_scheme = rendezvous_base_url_env.replace("https://", "").replace("http://", "")
        full_rendezvous_ws_url = f"{ws_scheme}://{base_url_no_scheme}/ws/register/{worker_id}"
    
    # Start the rendezvous client task, passing the app_context
    rendezvous_client_task = asyncio.create_task(rendezvous_client_module.connect_to_rendezvous(app_context, full_rendezvous_ws_url))

    global echo_server # echo_server is a global in main.py
    if ENABLE_QUIC_ECHO_SERVER:
        async def _echo_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            addr = writer.get_extra_info("peername")
            try:
                while True:
                    data = await reader.read(4096)
                    if not data: break
                    writer.write(data)
                    await writer.drain()
            except Exception: pass
            finally:
                if not writer.is_closing(): writer.close()
                try: await writer.wait_closed()
                except Exception: pass
        try:
            echo_server = await asyncio.start_server(_echo_handler, app_context.LOCAL_QUIC_RELAY_HOST, app_context.LOCAL_QUIC_RELAY_PORT)
            srv_host, srv_port = echo_server.sockets[0].getsockname()
            print(f"[EchoServer] Listening on {srv_host}:{srv_port} (env ENABLE_QUIC_ECHO_SERVER=1)")
        except Exception as e_echo:
            print(f"WARNING: Failed to start built-in echo server on {app_context.LOCAL_QUIC_RELAY_HOST}:{app_context.LOCAL_QUIC_RELAY_PORT}: {e_echo}")
    try:
        await rendezvous_client_task
    except asyncio.CancelledError:
        print(f"Worker '{worker_id}': Rendezvous client task was cancelled.")
    finally:
        main_server.close()
        await main_server.wait_closed()
        print(f"Worker '{worker_id}': Main HTTP/UI WebSocket server stopped.")
        if p2p_udp_transport:
            try:
                if not p2p_udp_transport.is_closing(): 
                    p2p_udp_transport.close()
                print(f"Worker '{worker_id}': P2P UDP transport closed from main_async_orchestrator finally.")
            except Exception as e_close_transport:
                print(f"Worker '{worker_id}': Error closing P2P UDP transport in main_async_orchestrator: {e_close_transport}")
            p2p_udp_transport = None
        if echo_server:
            echo_server.close()
            try: await echo_server.wait_closed()
            except Exception: pass
            echo_server = None

if __name__ == "__main__":
    print(f"WORKER SCRIPT (ID: {worker_id}): Initializing...")
    rendezvous_base_url_env = os.environ.get("RENDEZVOUS_SERVICE_URL")
    if not rendezvous_base_url_env: print("CRITICAL ERROR: RENDEZVOUS_SERVICE_URL environment variable not set. Exiting worker."); sys.exit(1) 
    signal.signal(signal.SIGTERM, handle_shutdown_signal); signal.signal(signal.SIGINT, handle_shutdown_signal)
    try: asyncio.run(main_async_orchestrator())
    except KeyboardInterrupt: print(f"Worker '{worker_id}' interrupted by user."); stop_signal_received = True 
    except Exception as e_main_run: print(f"Worker '{worker_id}' CRITICAL ERROR in __main__: {type(e_main_run).__name__} - {e_main_run}")
    finally: print(f"Worker '{worker_id}' main EXIT.") 