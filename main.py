import asyncio
import os
import uuid
import websockets # For Rendezvous client AND UI server
import signal
import requests 
import json
import socket
import stun # pystun3
from typing import Optional, Tuple, Set, Dict
from pathlib import Path
from websockets.server import serve as websockets_serve
from websockets.http import Headers
import time # For benchmark timing
import base64 # For encoding benchmark payload

# --- Global Variables ---
worker_id = str(uuid.uuid4())
stop_signal_received = False
p2p_udp_transport: Optional[asyncio.DatagramTransport] = None
our_stun_discovered_udp_ip: Optional[str] = None
our_stun_discovered_udp_port: Optional[int] = None
current_p2p_peer_id: Optional[str] = None
current_p2p_peer_addr: Optional[Tuple[str, int]] = None

DEFAULT_STUN_HOST = os.environ.get("STUN_HOST", "stun.l.google.com")
DEFAULT_STUN_PORT = int(os.environ.get("STUN_PORT", "19302"))
INTERNAL_UDP_PORT = int(os.environ.get("INTERNAL_UDP_PORT", "8081"))
HTTP_PORT_FOR_UI = int(os.environ.get("PORT", 8080))

P2P_KEEP_ALIVE_INTERVAL_SEC = 15 # Interval in seconds to send P2P keep-alives

ui_websocket_clients: Set[websockets.WebSocketServerProtocol] = set()

# Benchmark related globals
benchmark_sessions: Dict[str, Dict] = {} # Key: peer_addr_str, Value: {received_bytes, received_chunks, start_time, total_chunks (from sender)}
BENCHMARK_CHUNK_SIZE = 1024 # 1KB

# Add these constants near the top with other environment variables
STUN_MAX_RETRIES = int(os.environ.get("STUN_MAX_RETRIES", "3"))
STUN_RETRY_DELAY_SEC = float(os.environ.get("STUN_RETRY_DELAY_SEC", "2.0"))

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

async def benchmark_send_udp_data(target_ip: str, target_port: int, size_kb: int, ui_ws: websockets.WebSocketServerProtocol):
    global worker_id, p2p_udp_transport
    if not (p2p_udp_transport and current_p2p_peer_addr):
        err_msg = "P2P UDP transport or peer address not available for benchmark."
        print(f"Worker '{worker_id}': {err_msg}")
        await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Error: {err_msg}"}))
        return

    print(f"Worker '{worker_id}': Starting P2P UDP Benchmark: Sending {size_kb}KB to {target_ip}:{target_port}")
    await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Benchmark Send: Starting to send {size_kb}KB..."}))

    num_chunks = size_kb
    dummy_chunk_content = b'B' * (BENCHMARK_CHUNK_SIZE - 50) # Approx to leave room for JSON overhead
    dummy_chunk_b64 = base64.b64encode(dummy_chunk_content).decode('ascii')
    
    start_time = time.monotonic()
    bytes_sent = 0
    update_interval = num_chunks // 10 if num_chunks >= 10 else 1

    try:
        for i in range(num_chunks):
            if stop_signal_received or ui_ws.closed:
                print(f"Worker '{worker_id}': Benchmark send cancelled (stop_signal or UI disconnected).")
                await ui_ws.send(json.dumps({"type": "benchmark_status", "message": "Benchmark send cancelled."}))
                break
            
            payload = {"type": "benchmark_chunk", "seq": i, "payload": dummy_chunk_b64, "from_worker_id": worker_id}
            data_to_send = json.dumps(payload).encode()
            p2p_udp_transport.sendto(data_to_send, (target_ip, target_port))
            bytes_sent += len(data_to_send)
            if (i + 1) % update_interval == 0: # Update UI every 10% or each chunk
                progress_msg = f"Benchmark Send: Sent {i+1}/{num_chunks} chunks ({bytes_sent / 1024:.2f} KB)..."
                print(f"Worker '{worker_id}': {progress_msg}")
                await ui_ws.send(json.dumps({"type": "benchmark_status", "message": progress_msg}))
        else: # If loop completed without break
            # Send benchmark end marker
            end_payload = {"type": "benchmark_end", "total_chunks": num_chunks, "from_worker_id": worker_id}
            p2p_udp_transport.sendto(json.dumps(end_payload).encode(), (target_ip, target_port))
            print(f"Worker '{worker_id}': Sent benchmark_end marker to {target_ip}:{target_port}")

            end_time = time.monotonic()
            duration = end_time - start_time
            throughput_kbps = (bytes_sent / 1024) / duration if duration > 0 else 0
            final_msg = f"Benchmark Send Complete: Sent {bytes_sent / 1024:.2f} KB in {duration:.2f}s. Throughput: {throughput_kbps:.2f} KB/s"
            print(f"Worker '{worker_id}': {final_msg}")
            await ui_ws.send(json.dumps({"type": "benchmark_status", "message": final_msg}))

    except Exception as e:
        error_msg = f"Benchmark Send Error: {type(e).__name__} - {e}"
        print(f"Worker '{worker_id}': {error_msg}")
        if not ui_ws.closed:
            await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Error: {error_msg}"}))

async def ui_websocket_handler(websocket: websockets.WebSocketServerProtocol, path: str):
    global ui_websocket_clients, worker_id, current_p2p_peer_id, p2p_udp_transport, current_p2p_peer_addr
    ui_websocket_clients.add(websocket)
    print(f"Worker '{worker_id}': UI WebSocket client connected from {websocket.remote_address}")
    try:
        await websocket.send(json.dumps({"type": "init_info", "worker_id": worker_id, "p2p_peer_id": current_p2p_peer_id}))
        async for message_raw in websocket:
            print(f"Worker '{worker_id}': Message from UI WebSocket: {message_raw}")
            try:
                message = json.loads(message_raw)
                msg_type = message.get("type")
                if msg_type == "send_p2p_message":
                    content = message.get("content")
                    if not current_p2p_peer_addr:
                        await websocket.send(json.dumps({"type": "error", "message": "Not connected to a P2P peer."}))
                    elif not content:
                        await websocket.send(json.dumps({"type": "error", "message": "Cannot send empty message."}))
                    elif p2p_udp_transport: # Ensure transport is also available
                        print(f"Worker '{worker_id}': Sending P2P UDP message '{content}' to peer {current_p2p_peer_id} at {current_p2p_peer_addr}")
                        p2p_message = {"type": "chat_message", "from_worker_id": worker_id, "content": content}
                        p2p_udp_transport.sendto(json.dumps(p2p_message).encode(), current_p2p_peer_addr)
                    else: # Should ideally not happen if current_p2p_peer_addr is set
                        await websocket.send(json.dumps({"type": "error", "message": "P2P transport not available."}))
                elif msg_type == "ui_client_hello":
                    print(f"Worker '{worker_id}': UI Client says hello.")
                    if current_p2p_peer_id:
                         await websocket.send(json.dumps({"type": "p2p_status_update", "message": f"P2P link active with {current_p2p_peer_id[:8]}...", "peer_id": current_p2p_peer_id}))
                elif msg_type == "start_benchmark_send":
                    size_kb = message.get("size_kb", 1024) # Default to 1MB if not specified
                    if current_p2p_peer_addr:
                        print(f"Worker '{worker_id}': UI requested benchmark send of {size_kb}KB to {current_p2p_peer_id}")
                        asyncio.create_task(benchmark_send_udp_data(current_p2p_peer_addr[0], current_p2p_peer_addr[1], size_kb, websocket))
                    else:
                        await websocket.send(json.dumps({"type": "benchmark_status", "message": "Error: No P2P peer to start benchmark with."}))
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
        global current_p2p_peer_addr, current_p2p_peer_id, benchmark_sessions
        message_str = data.decode(errors='ignore')
        try:
            p2p_message = json.loads(message_str)
            msg_type = p2p_message.get("type")
            from_id = p2p_message.get("from_worker_id")

            if from_id and current_p2p_peer_id and from_id != current_p2p_peer_id:
                print(f"Worker '{self.worker_id}': WARNING - Received P2P message from '{from_id}' but current peer is '{current_p2p_peer_id}'. Addr: {addr}")
            elif not from_id and msg_type not in ["benchmark_chunk", "benchmark_end"]:
                print(f"Worker '{self.worker_id}': WARNING - Received P2P message of type '{msg_type}' without 'from_worker_id'. Addr: {addr}")

            if msg_type == "chat_message":
                content = p2p_message.get("content")
                print(f"Worker '{self.worker_id}': Received P2P chat from '{from_id}' (expected: '{current_p2p_peer_id}'): '{content}'")
                for ui_client_ws in list(ui_websocket_clients): 
                    asyncio.create_task(ui_client_ws.send(json.dumps({"type": "p2p_message_received", "from_peer_id": from_id, "content": content}))) 
            elif msg_type == "p2p_test_data": 
                test_data_content = p2p_message.get("data")
                print(f"Worker '{self.worker_id}': +++ P2P_TEST_DATA RECEIVED from '{from_id}': '{test_data_content}' +++")
            elif msg_type == "benchmark_chunk": 
                peer_addr_str = str(addr)
                
                session = benchmark_sessions.setdefault(peer_addr_str, {
                    "received_bytes": 0, 
                    "received_chunks": 0, 
                    "start_time": time.monotonic(), 
                    "total_chunks": -1, 
                    "from_worker_id": from_id 
                })
                if from_id and session.get("from_worker_id") != from_id : 
                    if not session.get("from_worker_id"): 
                         session["from_worker_id"] = from_id
                    else: 
                        print(f"Worker '{self.worker_id}': WARNING - Benchmark session for {peer_addr_str} saw from_worker_id change from '{session.get('from_worker_id')}' to '{from_id}'.")

                session["received_bytes"] += len(data) 
                session["received_chunks"] += 1
                if session["received_chunks"] % 100 == 0: 
                    log_from_id = session.get('from_worker_id', 'unknown_peer')
                    print(f"Worker '{self.worker_id}': Benchmark data received from {log_from_id}@{peer_addr_str}: {session['received_chunks']} chunks, {session['received_bytes']/1024:.2f} KB")
            elif msg_type == "benchmark_end": 
                total_chunks_sent = p2p_message.get("total_chunks", 0)
                peer_addr_str = str(addr)
                if peer_addr_str in benchmark_sessions:
                    session = benchmark_sessions[peer_addr_str]
                    session["total_chunks"] = total_chunks_sent
                    duration = time.monotonic() - session["start_time"]
                    throughput_kbps = (session["received_bytes"] / 1024) / duration if duration > 0 else 0
                    log_from_id = session.get('from_worker_id', 'unknown_peer') # Use a safe default for from_id
                    status_msg = f"Benchmark Receive from {log_from_id} Complete: Received {session['received_chunks']}/{total_chunks_sent} chunks ({session['received_bytes']/1024:.2f} KB) in {duration:.2f}s. Throughput: {throughput_kbps:.2f} KB/s"
                    print(f"Worker '{self.worker_id}': {status_msg}")
                    for ui_client_ws in list(ui_websocket_clients):
                        asyncio.create_task(ui_client_ws.send(json.dumps({"type": "benchmark_status", "message": status_msg})))
                    del benchmark_sessions[peer_addr_str] 
                else:
                    print(f"Worker '{self.worker_id}': Received benchmark_end from unknown session/peer {addr}")     
            elif msg_type == "p2p_keep_alive": 
                print(f"Worker '{self.worker_id}': Received P2P keep-alive from '{from_id}' at {addr}")
            elif msg_type == "p2p_pairing_test":
                timestamp = p2p_message.get("timestamp")
                print(f"Worker '{self.worker_id}': Received p2p_pairing_test from '{from_id}' (timestamp: {timestamp}). Sending echo.")
                echo_message = {
                    "type": "p2p_pairing_echo",
                    "from_worker_id": self.worker_id, # Current worker's ID
                    "original_timestamp": timestamp
                }
                if self.transport: # Ensure transport is available
                    self.transport.sendto(json.dumps(echo_message).encode(), addr)
                    print(f"Worker '{self.worker_id}': Sent p2p_pairing_echo to '{from_id}' at {addr}")
            elif msg_type == "p2p_pairing_echo":
                original_timestamp = p2p_message.get("original_timestamp")
                rtt = (time.time() - original_timestamp) * 1000 if original_timestamp else -1
                print(f"Worker '{self.worker_id}': Received p2p_pairing_echo from '{from_id}'. RTT: {rtt:.2f} ms (if timestamp valid).")
                # Optionally send this to UI
                for ui_client_ws in list(ui_websocket_clients):
                    asyncio.create_task(ui_client_ws.send(json.dumps({"type": "p2p_status_update", "message": f"Pairing test with {from_id[:8]} successful! RTT: {rtt:.2f}ms"})))
            elif "P2P_PING_FROM_" in message_str: print(f"Worker '{self.worker_id}': !!! P2P UDP Ping (legacy) received from {addr} !!!")
        except json.JSONDecodeError: print(f"Worker '{self.worker_id}': Received non-JSON UDP packet from {addr}: {message_str}")
    def error_received(self, exc: Exception): print(f"Worker '{self.worker_id}': P2P UDP listener error: {exc}")
    def connection_lost(self, exc: Optional[Exception]): 
        global p2p_udp_transport
        print(f"Worker '{self.worker_id}': P2P UDP listener connection lost: {exc if exc else 'Closed normally'}")
        if self.transport == p2p_udp_transport: p2p_udp_transport = None

async def discover_and_report_stun_udp_endpoint(websocket_conn_to_rendezvous):
    global our_stun_discovered_udp_ip, our_stun_discovered_udp_port, worker_id, INTERNAL_UDP_PORT
    
    stun_host = os.environ.get("STUN_HOST", DEFAULT_STUN_HOST)
    stun_port = int(os.environ.get("STUN_PORT", DEFAULT_STUN_PORT))
    
    for attempt in range(1, STUN_MAX_RETRIES + 1):
        print(f"Worker '{worker_id}': STUN discovery attempt {attempt}/{STUN_MAX_RETRIES} via {stun_host}:{stun_port} for local port {INTERNAL_UDP_PORT}.")
        try:
            # The stun.get_ip_info function can raise socket.gaierror, stun.StunException, OSError, etc.
            nat_type, external_ip, external_port = stun.get_ip_info(
                source_ip="0.0.0.0",
                source_port=INTERNAL_UDP_PORT,
                stun_host=stun_host,
                stun_port=stun_port
            )
            
            if external_ip and external_port:
                print(f"Worker '{worker_id}': STUN: NAT='{nat_type}', External IP='{external_ip}', Port={external_port}")
                our_stun_discovered_udp_ip = external_ip
                our_stun_discovered_udp_port = external_port
                await websocket_conn_to_rendezvous.send(json.dumps({"type": "update_udp_endpoint", "udp_ip": external_ip, "udp_port": external_port}))
                print(f"Worker '{worker_id}': Sent STUN UDP endpoint ({external_ip}:{external_port}) to Rendezvous.")
                return True # Success, exit function
            else:
                # stun.get_ip_info succeeded but didn't return usable IP/port
                print(f"Worker '{worker_id}': STUN attempt {attempt} succeeded but returned no valid external IP/Port.")
                # Fall through to retry logic if not last attempt

        except socket.gaierror as e_gaierror: # Specific error for DNS issues
            print(f"Worker '{worker_id}': STUN attempt {attempt} failed: DNS resolution error for '{stun_host}': {e_gaierror}")
        except stun.StunException as e_stun: # Catch specific STUN protocol errors from pystun3
            print(f"Worker '{worker_id}': STUN attempt {attempt} failed: STUN protocol error: {type(e_stun).__name__} - {e_stun}")
        except OSError as e_os: # Catch socket-related errors like "Address already in use" or network issues
            print(f"Worker '{worker_id}': STUN attempt {attempt} failed: OS error (e.g., socket issue): {type(e_os).__name__} - {e_os}")
        except Exception as e_general: # Catch-all for any other unexpected errors from stun.get_ip_info
            print(f"Worker '{worker_id}': STUN attempt {attempt} failed: An unexpected error: {type(e_general).__name__} - {e_general}")

        # If we've reached here, the current attempt failed (either an exception or no IP/port returned)
        if attempt < STUN_MAX_RETRIES:
            delay = STUN_RETRY_DELAY_SEC * (2 ** (attempt - 1)) # Exponential backoff
            print(f"Worker '{worker_id}': Retrying STUN in {delay:.1f} seconds...")
            await asyncio.sleep(delay)
            # Loop will continue to the next attempt
        else: # This was the last attempt and it failed
            print(f"Worker '{worker_id}': STUN discovery failed after {STUN_MAX_RETRIES} attempts.")
            return False # All retries exhausted

    # Fallback, theoretically unreachable if STUN_MAX_RETRIES >= 1
    print(f"Worker '{worker_id}': STUN discovery function unexpectedly completed loop without success or explicit failure.")
    return False

async def start_udp_hole_punch(peer_udp_ip: str, peer_udp_port: int, peer_worker_id: str):
    global worker_id, stop_signal_received, p2p_udp_transport, current_p2p_peer_addr, current_p2p_peer_id
    if not p2p_udp_transport: 
        print(f"Worker '{worker_id}': UDP transport not ready for hole punch to '{peer_worker_id}'."); 
        return

    # Log the (potentially new) P2P target
    print(f"Worker '{worker_id}': Initiating P2P connection. Previous peer ID: '{current_p2p_peer_id}', Previous peer addr: {current_p2p_peer_addr}.")
    current_p2p_peer_id = peer_worker_id
    current_p2p_peer_addr = (peer_udp_ip, peer_udp_port)
    print(f"Worker '{worker_id}': Set new P2P target. Current peer ID: '{current_p2p_peer_id}', Current peer addr: {current_p2p_peer_addr}.")

    print(f"Worker '{worker_id}': Starting UDP hole punch PINGs towards '{peer_worker_id}' at {current_p2p_peer_addr}")
    for i in range(1, 4): # Send a few pings
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

    # NEW: Determine if this worker is the initiator for the pairing test
    if worker_id < peer_worker_id: # Lexicographical comparison
        print(f"Worker '{worker_id}': Designated as initiator for pairing test with '{peer_worker_id}'. Sending test message.")
        pairing_test_message = {
            "type": "p2p_pairing_test",
            "from_worker_id": worker_id,
            "timestamp": time.time()
        }
        try:
            p2p_udp_transport.sendto(json.dumps(pairing_test_message).encode(), current_p2p_peer_addr)
            print(f"Worker '{worker_id}': Sent p2p_pairing_test to '{peer_worker_id}' at {current_p2p_peer_addr}")
        except Exception as e:
            print(f"Worker '{worker_id}': Error sending p2p_pairing_test: {e}")
    else:
        print(f"Worker '{worker_id}': Designated as responder for pairing test with '{peer_worker_id}'. Awaiting test message.")

async def attempt_hole_punch_when_ready(peer_udp_ip: str, peer_udp_port: int, peer_worker_id: str, max_wait_sec: float = 10.0, check_interval: float = 0.5):
    """Safely initiate a UDP hole-punch once the local UDP listener becomes active.

    This helps when a p2p_connection_offer arrives *before* we have finished
    creating the asyncio UDP datagram endpoint (race condition). We poll for
    the global ``p2p_udp_transport`` for up to ``max_wait_sec`` seconds.
    """
    global p2p_udp_transport, stop_signal_received, worker_id

    waited = 0.0
    while not stop_signal_received and waited < max_wait_sec:
        if p2p_udp_transport:  # Listener is finally ready
            await start_udp_hole_punch(peer_udp_ip, peer_udp_port, peer_worker_id)
            return
        await asyncio.sleep(check_interval)
        waited += check_interval

    # If we exit the loop we either exceeded the wait time or shutdown was requested.
    print(
        f"Worker '{worker_id}': Gave up waiting ({waited:.1f}s) for UDP listener to become active "
        f"before initiating hole-punch to '{peer_worker_id}'."
    )

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
            # Ensure explicit proxy=None to avoid automatic system proxy usage (websockets v15.0+ behavior)
            async with websockets.connect(rendezvous_ws_url, 
                                        ping_interval=ping_interval, 
                                        ping_timeout=ping_timeout,
                                        proxy=None) as ws_to_rendezvous:
                print(f"Worker '{worker_id}' connected to Rendezvous Service.")
                try:
                    response = requests.get(ip_echo_service_url, timeout=10)
                    response.raise_for_status()
                    http_public_ip = response.text.strip()
                    await ws_to_rendezvous.send(json.dumps({"type": "register_public_ip", "ip": http_public_ip}))
                    print(f"Worker '{worker_id}' sent HTTP-based IP ({http_public_ip}) to Rendezvous.")
                except Exception as e_http_ip: print(f"Worker '{worker_id}': Error sending HTTP IP: {e_http_ip}")
                
                # Initial STUN discovery
                stun_success_initial = await discover_and_report_stun_udp_endpoint(ws_to_rendezvous)

                if stun_success_initial and not udp_listener_active:
                    try:
                        # The socket main_udp_sock is already bound.
                        # We pass this existing, bound socket to create_datagram_endpoint.
                        _transport, _protocol = await loop.create_datagram_endpoint(
                            lambda: P2PUDPProtocol(worker_id),
                            local_addr=('0.0.0.0', INTERNAL_UDP_PORT)
                        )
                        p2p_listener_transport_local_ref = _transport
                        await asyncio.sleep(0.1)
                        if p2p_udp_transport: print(f"Worker '{worker_id}': Asyncio P2P UDP listener appears active on 0.0.0.0:{INTERNAL_UDP_PORT}.")
                        else: print(f"Worker '{worker_id}': P2P UDP listener transport not set globally after create_datagram_endpoint on 0.0.0.0:{INTERNAL_UDP_PORT}.")
                        udp_listener_active = True
                    except Exception as e_udp_listen:
                        print(f"Worker '{worker_id}': Failed to create P2P UDP datagram endpoint on 0.0.0.0:{INTERNAL_UDP_PORT}: {e_udp_listen}")
                
                while not stop_signal_received:
                    try:
                        message_raw = await asyncio.wait_for(ws_to_rendezvous.recv(), timeout=ping_interval) # Use ping_interval for recv timeout
                        print(f"Worker '{worker_id}': Message from Rendezvous: {message_raw}")
                        message_data = json.loads(message_raw)
                        msg_type = message_data.get("type")
                        if msg_type == "p2p_connection_offer":
                            peer_id = message_data.get("peer_worker_id")
                            peer_ip = message_data.get("peer_udp_ip")
                            peer_port = message_data.get("peer_udp_port")
                            if peer_id and peer_ip and peer_port:
                                print(f"Worker '{worker_id}': Received P2P offer for peer '{peer_id}' at {peer_ip}:{peer_port}")
                                # Always schedule an attempt, letting the helper wait until the listener is ready.
                                asyncio.create_task(attempt_hole_punch_when_ready(peer_ip, int(peer_port), peer_id))
                        elif msg_type == "udp_endpoint_ack": print(f"Worker '{worker_id}': UDP Endpoint Ack: {message_data.get('status')}")
                        elif msg_type == "echo_response": print(f"Worker '{worker_id}': Echo Response: {message_data.get('processed_by_rendezvous')}")
                        elif msg_type == "admin_chat_message":
                            # Admin is sending a chat message to this worker
                            admin_session_id = message_data.get("admin_session_id")
                            content = message_data.get("content")
                            if admin_session_id and content:
                                print(f"Worker '{worker_id}': Received admin chat: '{content}'")
                                # Forward to UI clients
                                for ui_client_ws in list(ui_websocket_clients):
                                    try:
                                        await ui_client_ws.send(json.dumps({
                                            "type": "admin_chat_received",
                                            "content": content,
                                            "admin_session_id": admin_session_id
                                        }))
                                    except Exception as e:
                                        print(f"Worker '{worker_id}': Error forwarding admin chat to UI: {e}")
                                
                                # Auto-reply for demo purposes (workers can implement their own logic)
                                await ws_to_rendezvous.send(json.dumps({
                                    "type": "chat_response",
                                    "admin_session_id": admin_session_id,
                                    "content": f"Worker {worker_id[:8]} received: {content}"
                                }))
                        else: print(f"Worker '{worker_id}': Unhandled message from Rendezvous: {msg_type}")
                    except asyncio.TimeoutError: 
                        # This is expected if no messages from rendezvous, allows periodic tasks.
                        # WebSocket ping/pong should keep the connection alive.
                        pass
                    except websockets.exceptions.ConnectionClosed as e_conn_closed: 
                        print(f"Worker '{worker_id}': Rendezvous WS closed by server during recv: {e_conn_closed}.") 
                        break # Break inner message loop
                    except Exception as e_recv: 
                        print(f"Worker '{worker_id}': Error in WS recv loop: {e_recv}") 
                        break # Break inner message loop
                    
                # End of inner message/task loop
                if stop_signal_received: break 

        except websockets.exceptions.ConnectionClosed as e_outer_closed:
            print(f"Worker '{worker_id}': Rendezvous WS connection closed before or during connect: {e_outer_closed}")
        except Exception as e_ws_connect: 
            print(f"Worker '{worker_id}': Error in WS connection loop: {type(e_ws_connect).__name__} - {e_ws_connect}. Retrying...")
        finally: 
            if p2p_listener_transport_local_ref: 
                print(f"Worker '{worker_id}': Closing local P2P UDP transport (asyncio wrapper) from this WS session.")
                p2p_listener_transport_local_ref.close()
                # If this transport was the global one, clear the global reference
                if p2p_udp_transport == p2p_listener_transport_local_ref:
                    p2p_udp_transport = None
                udp_listener_active = False # Allow re-creation of transport on next connection
        if not stop_signal_received: await asyncio.sleep(10)
        else: break

async def send_periodic_p2p_keep_alives():
    global worker_id, stop_signal_received, p2p_udp_transport, current_p2p_peer_addr
    print(f"Worker '{worker_id}': P2P Keep-Alive sender task started.")
    while not stop_signal_received:
        await asyncio.sleep(P2P_KEEP_ALIVE_INTERVAL_SEC)
        if p2p_udp_transport and current_p2p_peer_addr:
            try:
                keep_alive_message = {"type": "p2p_keep_alive", "from_worker_id": worker_id}
                encoded_message = json.dumps(keep_alive_message).encode()
                p2p_udp_transport.sendto(encoded_message, current_p2p_peer_addr)
            except Exception as e:
                print(f"Worker '{worker_id}': Error sending P2P keep-alive: {e}")
        # No explicit print for transport not ready or no peer, to reduce verbosity.
        # These conditions are normal states.
    print(f"Worker '{worker_id}': P2P Keep-Alive sender task stopped.")

async def main_async_orchestrator():
    loop = asyncio.get_running_loop()

    main_server = await websockets_serve(
        ui_websocket_handler, "0.0.0.0", HTTP_PORT_FOR_UI,
        process_request=process_http_request,
        ping_interval=20, ping_timeout=20
    )
    print(f"Worker '{worker_id}': HTTP & UI WebSocket server listening on 0.0.0.0:{HTTP_PORT_FOR_UI}")
    print(f"  - Serving index.html at '/'")
    print(f"  - UI WebSocket at '/ui_ws'")
    print(f"  - Health check at '/health'")
    rendezvous_base_url_env = os.environ.get("RENDEZVOUS_SERVICE_URL")
    if not rendezvous_base_url_env: print("CRITICAL: RENDEZVOUS_SERVICE_URL missing in main_async_runner.")
    full_rendezvous_ws_url = ""
    if rendezvous_base_url_env: 
        ws_scheme = "wss" if rendezvous_base_url_env.startswith("https://") else "ws"
        base_url_no_scheme = rendezvous_base_url_env.replace("https://", "").replace("http://", "")
        full_rendezvous_ws_url = f"{ws_scheme}://{base_url_no_scheme}/ws/register/{worker_id}"
    # Pass main_udp_sock to connect_to_rendezvous
    rendezvous_client_task = asyncio.create_task(connect_to_rendezvous(full_rendezvous_ws_url))
    p2p_keep_alive_task = asyncio.create_task(send_periodic_p2p_keep_alives()) # NEW: Start P2P keep-alive task

    try:
        # await rendezvous_client_task # Original
        await asyncio.gather(rendezvous_client_task, p2p_keep_alive_task) # Wait for both tasks
    except asyncio.CancelledError:
        print(f"Worker '{worker_id}': Main orchestrator tasks were cancelled.")
    finally:
        main_server.close()
        await main_server.wait_closed()
        print(f"Worker '{worker_id}': Main HTTP/UI WebSocket server stopped.")
        
        # Close the asyncio P2P UDP transport if it's active
        if p2p_udp_transport:
            try:
                p2p_udp_transport.close()
                print(f"Worker '{worker_id}': P2P UDP transport closed from main_async_orchestrator finally.")
            except Exception as e_close_transport:
                print(f"Worker '{worker_id}': Error closing P2P UDP transport in main_async_orchestrator: {e_close_transport}")

        # Ensure cancellation of tasks if they are still running
        if rendezvous_client_task and not rendezvous_client_task.done():
            rendezvous_client_task.cancel()
            print(f"Worker '{worker_id}': Cancelled rendezvous_client_task.")
        if p2p_keep_alive_task and not p2p_keep_alive_task.done():
            p2p_keep_alive_task.cancel()
            print(f"Worker '{worker_id}': Cancelled p2p_keep_alive_task.")
        # Optionally await their cancellation
        try:
            await asyncio.gather(rendezvous_client_task, p2p_keep_alive_task, return_exceptions=True)
        except asyncio.CancelledError:
            print(f"Worker '{worker_id}': Tasks fully cancelled during cleanup.")

if __name__ == "__main__":
    print(f"WORKER SCRIPT (ID: {worker_id}): Initializing...")
    rendezvous_base_url_env = os.environ.get("RENDEZVOUS_SERVICE_URL")
    if not rendezvous_base_url_env: print("CRITICAL ERROR: RENDEZVOUS_SERVICE_URL environment variable not set. Exiting worker."); exit(1) 
    signal.signal(signal.SIGTERM, handle_shutdown_signal); signal.signal(signal.SIGINT, handle_shutdown_signal)
    try: asyncio.run(main_async_orchestrator())
    except KeyboardInterrupt: print(f"Worker '{worker_id}' interrupted by user."); stop_signal_received = True 
    except Exception as e_main_run: print(f"Worker '{worker_id}' CRITICAL ERROR in __main__: {type(e_main_run).__name__} - {e_main_run}")
    finally: print(f"Worker '{worker_id}' main EXIT.") 