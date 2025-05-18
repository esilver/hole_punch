import asyncio
import os
import uuid
import websockets # For Rendezvous client AND UI server
import aiohttp
import signal
import threading
import http.server 
import socketserver 
import requests 
import json
import socket
import stun # pystun3
import sys # Added for sys.exit()
from typing import Optional, Tuple, Set, Dict
from pathlib import Path
from websockets.server import serve as websockets_serve
from websockets.http import Headers
import time # For benchmark timing
import base64 # For encoding benchmark payload

# --- Global Variables ---
worker_id = str(uuid.uuid4())
stop_signal_received = False
active_rendezvous_websocket: Optional[websockets.WebSocketClientProtocol] = None # Added for explicit deregister
p2p_udp_transport: Optional[asyncio.DatagramTransport] = None
our_stun_discovered_udp_ip: Optional[str] = None
our_stun_discovered_udp_port: Optional[int] = None
current_p2p_peer_id: Optional[str] = None
current_p2p_peer_addr: Optional[Tuple[str, int]] = None

DEFAULT_STUN_HOST = os.environ.get("STUN_HOST", "stun.l.google.com")
DEFAULT_STUN_PORT = int(os.environ.get("STUN_PORT", "19302"))
INTERNAL_UDP_PORT = int(os.environ.get("INTERNAL_UDP_PORT", "8081"))
HTTP_PORT_FOR_UI = int(os.environ.get("PORT", 8080))

ui_websocket_clients: Set[websockets.WebSocketServerProtocol] = set()

# Benchmark related globals
benchmark_sessions: Dict[str, Dict] = {} # Key: peer_addr_str, Value: {received_bytes, received_chunks, start_time, total_chunks (from sender)}
BENCHMARK_CHUNK_SIZE = int(os.environ.get("BENCHMARK_CHUNK_SIZE", "1024")) # Read from env, default 1KB
BENCHMARK_FILE_URL = os.environ.get("BENCHMARK_GCS_URL")

async def download_benchmark_file() -> bytes:
    """Download the benchmark file from GCS asynchronously."""
    if not BENCHMARK_FILE_URL:
        raise RuntimeError("BENCHMARK_GCS_URL environment variable not set")

    async with aiohttp.ClientSession() as session:
        async with session.get(BENCHMARK_FILE_URL) as resp:
            resp.raise_for_status()
            return await resp.read()

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


async def benchmark_send_udp_data(target_ip: str, target_port: int, _size_kb: int, ui_ws: websockets.WebSocketServerProtocol):
    """Download a file from GCS and send it to the peer over UDP."""
    global worker_id, p2p_udp_transport

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
        file_bytes = await download_benchmark_file()
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
            # Binary packet: 4-byte big-endian sequence number + raw chunk bytes
            seq_header = i.to_bytes(4, "big")
            packet = seq_header + chunk
            p2p_udp_transport.sendto(packet, (target_ip, target_port))
            bytes_sent += len(packet)
            if (i + 1) % (num_chunks // 10 if num_chunks >= 10 else 1) == 0:
                progress_msg = f"Benchmark Send: Sent {i+1}/{num_chunks} chunks ({bytes_sent / 1024:.2f} KB)..."
                print(f"Worker '{worker_id}': {progress_msg}")
                await ui_ws.send(json.dumps({"type": "benchmark_status", "message": progress_msg}))
        else:
            # Send 0xFFFFFFFF as a 4-byte sequence to mark end of transfer
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

async def ui_websocket_handler(websocket: websockets.WebSocketServerProtocol, path: str):
    global ui_websocket_clients, worker_id, current_p2p_peer_id, p2p_udp_transport, current_p2p_peer_addr, active_rendezvous_websocket
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
                elif msg_type == "start_benchmark_send": # NEW
                    size_kb = message.get("size_kb", 1024)
                    if current_p2p_peer_addr:
                        print(f"Worker '{worker_id}': UI requested benchmark send of {size_kb}KB to {current_p2p_peer_id}")
                        asyncio.create_task(benchmark_send_udp_data(current_p2p_peer_addr[0], current_p2p_peer_addr[1], size_kb, websocket))
                    else:
                        await websocket.send(json.dumps({"type": "benchmark_status", "message": "Error: No P2P peer to start benchmark with."}))
                elif msg_type == "restart_worker_request":
                    print(f"Worker '{worker_id}': Received restart_worker_request from UI. Attempting explicit deregistration.")
                    
                    # Attempt to explicitly deregister from Rendezvous
                    if active_rendezvous_websocket:
                        try:
                            deregister_payload = json.dumps({"type": "explicit_deregister", "worker_id": worker_id})
                            await asyncio.wait_for(active_rendezvous_websocket.send(deregister_payload), timeout=2.0)
                            print(f"Worker '{worker_id}': Sent explicit_deregister to Rendezvous.")
                        except asyncio.TimeoutError:
                            print(f"Worker '{worker_id}': Timeout sending explicit_deregister to Rendezvous.")
                        except Exception as e_dereg:
                            print(f"Worker '{worker_id}': Error sending explicit_deregister: {e_dereg}")
                    else:
                        print(f"Worker '{worker_id}': No active Rendezvous WebSocket to send explicit_deregister.")

                    # Proceed with UI notification and shutdown
                    await websocket.send(json.dumps({"type": "system_message", "message": "Worker is restarting..."}))
                    await websocket.close(code=1000, reason="Worker restarting")
                    asyncio.create_task(delayed_exit(1))
                elif msg_type == "redo_stun_request":
                    # UI requests redoing STUN discovery and re-registering with the Rendezvous
                    if active_rendezvous_websocket:
                        # First, close existing UDP listener (releasing port) if present
                        if p2p_udp_transport:
                            try:
                                p2p_udp_transport.close()
                                p2p_udp_transport = None
                                print(f"Worker '{worker_id}': Closed existing UDP listener prior to STUN re-discovery.")
                            except Exception as e_close_udp:
                                print(f"Worker '{worker_id}': Error closing UDP transport before STUN re-discovery: {e_close_udp}")
                                await websocket.send(json.dumps({"type": "error", "message": f"Could not close existing UDP listener: {type(e_close_udp).__name__} - {e_close_udp}"}))
                        try:
                            stun_success = await discover_and_report_stun_udp_endpoint(active_rendezvous_websocket)
                            if stun_success:
                                await websocket.send(json.dumps({"type": "system_message", "message": "STUN re-discovery successful and endpoint re-registered with Rendezvous."}))
                            else:
                                await websocket.send(json.dumps({"type": "error", "message": "STUN re-discovery failed. Check backend logs for details."}))
                        except Exception as e_stun:
                            await websocket.send(json.dumps({"type": "error", "message": f"Error during STUN re-discovery: {type(e_stun).__name__} - {e_stun}"}))
                    else:
                        await websocket.send(json.dumps({"type": "error", "message": "No active connection to Rendezvous service. Cannot re-register."}))

                    # Notify UI client that this connection will close to allow reconnection
                    try:
                        await websocket.send(json.dumps({"type": "system_message", "message": "STUN re-discovery process finished. Closing UI WebSocket to refresh state…"}))
                    except Exception:
                        pass  # Ignore if can't send (e.g., already closing)
                    try:
                        await websocket.close(code=1000, reason="STUN rediscovery complete – please reconnect")
                    except Exception:
                        pass
            except json.JSONDecodeError: print(f"Worker '{worker_id}': UI WebSocket received non-JSON: {message_raw}")
            except Exception as e_ui_msg: print(f"Worker '{worker_id}': Error processing UI WebSocket message: {e_ui_msg}")
    except websockets.exceptions.ConnectionClosed: print(f"Worker '{worker_id}': UI WebSocket client {websocket.remote_address} disconnected.")
    except Exception as e_ui_conn: print(f"Worker '{worker_id}': Error with UI WebSocket connection {websocket.remote_address}: {e_ui_conn}")
    finally:
        ui_websocket_clients.remove(websocket)
        print(f"Worker '{worker_id}': UI WebSocket client {websocket.remote_address} removed.")

async def delayed_exit(delay_seconds: float):
    await asyncio.sleep(delay_seconds)
    print(f"Worker '{worker_id}': Executing delayed exit (forcing process termination).")
    os._exit(0)  # Forcefully terminate the process to let Cloud Run restart the container

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
        # Attempt to treat packet as JSON first; if that fails, fall back to binary benchmark handling
        try:
            message_str = data.decode()
            p2p_message = json.loads(message_str)
            is_json = True
        except (UnicodeDecodeError, json.JSONDecodeError):
            is_json = False

        if not is_json:
            # Binary benchmark path – first 4 bytes = sequence number (big-endian)
            if len(data) < 4:
                return  # Ignore too-short packets
            seq = int.from_bytes(data[:4], "big")
            peer_addr_str = str(addr)
            if seq == 0xFFFFFFFF:  # End marker
                if peer_addr_str in benchmark_sessions:
                    session = benchmark_sessions[peer_addr_str]
                    session["total_chunks"] = session["received_chunks"]
                    duration = time.monotonic() - session["start_time"]
                    throughput_kbps = (session["received_bytes"] / 1024) / duration if duration > 0 else 0
                    status_msg = (
                        f"Benchmark Receive from {session.get('from_worker_id', 'peer')} Complete: "
                        f"Received {session['received_chunks']} chunks ({session['received_bytes']/1024:.2f} KB) in {duration:.2f}s. "
                        f"Throughput: {throughput_kbps:.2f} KB/s"
                    )
                    print(f"Worker '{self.worker_id}': {status_msg}")
                    for ui_client_ws in list(ui_websocket_clients):
                        asyncio.create_task(ui_client_ws.send(json.dumps({"type": "benchmark_status", "message": status_msg})))
                    del benchmark_sessions[peer_addr_str]
                return

            # Regular chunk packet
            if peer_addr_str not in benchmark_sessions:
                benchmark_sessions[peer_addr_str] = {
                    "received_bytes": 0,
                    "received_chunks": 0,
                    "start_time": time.monotonic(),
                    "from_worker_id": current_p2p_peer_id,
                }
            session = benchmark_sessions[peer_addr_str]
            session["received_bytes"] += len(data)
            session["received_chunks"] += 1
            if session["received_chunks"] % 100 == 0:
                print(
                    f"Worker '{self.worker_id}': Benchmark data received from peer@{peer_addr_str}: "
                    f"{session['received_chunks']} chunks, {session['received_bytes']/1024:.2f} KB"
                )
            return  # Binary packet handled – stop here

        # ==== Existing JSON handling path below (runs only if is_json is True) ====
        msg_type = p2p_message.get("type")
        from_id = p2p_message.get("from_worker_id")

        if from_id and current_p2p_peer_id and from_id != current_p2p_peer_id:
            print(f"Worker '{self.worker_id}': WARNING - Received P2P message from '{from_id}' but current peer is '{current_p2p_peer_id}'. Addr: {addr}")
        elif not from_id and msg_type not in ["benchmark_chunk", "benchmark_end"]: # Benchmark chunks might not have from_id if an old client is used.
            print(f"Worker '{self.worker_id}': WARNING - Received P2P message of type '{msg_type}' without 'from_worker_id'. Addr: {addr}")

        if msg_type == "chat_message":
            content = p2p_message.get("content")
            print(f"Worker '{self.worker_id}': Received P2P chat from '{from_id}' (expected: '{current_p2p_peer_id}'): '{content}'")
            for ui_client_ws in list(ui_websocket_clients): 
                # print(f"WORKER '{self.worker_id}': Attempting to send to UI client: {ui_client_ws.remote_address}") # Too verbose
                asyncio.create_task(ui_client_ws.send(json.dumps({"type": "p2p_message_received", "from_peer_id": from_id, "content": content}))) 
        elif msg_type == "p2p_test_data": # Legacy, can remove if benchmark uses benchmark_chunk
            test_data_content = p2p_message.get("data")
            print(f"Worker '{self.worker_id}': +++ P2P_TEST_DATA RECEIVED from '{from_id}': '{test_data_content}' +++")
        elif msg_type == "benchmark_chunk": # NEW
            seq = p2p_message.get("seq", -1)
            payload_b64 = p2p_message.get("payload", "")
            # payload_bytes = base64.b64decode(payload_b64) # Not strictly needed to decode on receiver for throughput test
            peer_addr_str = str(addr)
            if peer_addr_str not in benchmark_sessions:
                benchmark_sessions[peer_addr_str] = {"received_bytes": 0, "received_chunks": 0, "start_time": time.monotonic(), "total_chunks": -1, "from_worker_id": from_id}
            session = benchmark_sessions[peer_addr_str]
            session["received_bytes"] += len(message_str) # Approx size of JSON string
            session["received_chunks"] += 1
            if session["received_chunks"] % 100 == 0: # Log progress periodically
                print(f"Worker '{self.worker_id}': Benchmark data received from {from_id}@{peer_addr_str}: {session['received_chunks']} chunks, {session['received_bytes']/1024:.2f} KB")
        elif msg_type == "benchmark_end": # NEW
            total_chunks_sent = p2p_message.get("total_chunks", 0)
            peer_addr_str = str(addr)
            if peer_addr_str in benchmark_sessions:
                session = benchmark_sessions[peer_addr_str]
                session["total_chunks"] = total_chunks_sent
                duration = time.monotonic() - session["start_time"]
                throughput_kbps = (session["received_bytes"] / 1024) / duration if duration > 0 else 0
                status_msg = f"Benchmark Receive from {session['from_worker_id']} Complete: Received {session['received_chunks']}/{total_chunks_sent} chunks ({session['received_bytes']/1024:.2f} KB) in {duration:.2f}s. Throughput: {throughput_kbps:.2f} KB/s"
                print(f"Worker '{self.worker_id}': {status_msg}")
                for ui_client_ws in list(ui_websocket_clients):
                    asyncio.create_task(ui_client_ws.send(json.dumps({"type": "benchmark_status", "message": status_msg})))
                del benchmark_sessions[peer_addr_str] # Clear session
            else:
                print(f"Worker '{self.worker_id}': Received benchmark_end from unknown session/peer {addr}")     
        elif "P2P_PING_FROM_" in message_str: print(f"Worker '{self.worker_id}': !!! P2P UDP Ping (legacy) received from {addr} !!!")
    def error_received(self, exc: Exception): print(f"Worker '{self.worker_id}': P2P UDP listener error: {exc}")
    def connection_lost(self, exc: Optional[Exception]): 
        global p2p_udp_transport
        print(f"Worker '{self.worker_id}': P2P UDP listener connection lost: {exc if exc else 'Closed normally'}")
        if self.transport == p2p_udp_transport: p2p_udp_transport = None

async def discover_and_report_stun_udp_endpoint(websocket_conn_to_rendezvous):
    global our_stun_discovered_udp_ip, our_stun_discovered_udp_port, worker_id, INTERNAL_UDP_PORT
    try:
        stun_host = os.environ.get("STUN_HOST", DEFAULT_STUN_HOST)
        stun_port = int(os.environ.get("STUN_PORT", DEFAULT_STUN_PORT))
        print(f"Worker '{worker_id}': Attempting STUN discovery via {stun_host}:{stun_port} for local port {INTERNAL_UDP_PORT}.")

        # Broader exception catch specifically for get_ip_info, as this was proven to be more robust
        try:
            nat_type, external_ip, external_port = stun.get_ip_info(
                source_ip="0.0.0.0",
                source_port=INTERNAL_UDP_PORT,
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
        pass # Ensure finally is not empty if all lines removed

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

    local_bind_addr_tuple = p2p_udp_transport.get_extra_info('sockname') if p2p_udp_transport else ("unknown", "unknown")
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
    # Test data packet sending removed from here, will be initiated by UI action.
    for ui_client_ws in list(ui_websocket_clients):
        asyncio.create_task(ui_client_ws.send(json.dumps({"type": "p2p_status_update", "message": f"P2P link attempt initiated with {peer_worker_id[:8]}...", "peer_id": peer_worker_id})))

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
    global stop_signal_received, p2p_udp_transport, INTERNAL_UDP_PORT, ui_websocket_clients, our_stun_discovered_udp_ip, our_stun_discovered_udp_port, active_rendezvous_websocket
    ip_echo_service_url = "https://api.ipify.org"
    ping_interval = float(os.environ.get("PING_INTERVAL_SEC", "25"))
    ping_timeout = float(os.environ.get("PING_TIMEOUT_SEC", "25"))
    STUN_RECHECK_INTERVAL = float(os.environ.get("STUN_RECHECK_INTERVAL_SEC", "300")) # Default 5 minutes
    udp_listener_active = False
    loop = asyncio.get_running_loop()
    last_stun_recheck_time = 0.0 # Initialize

    while not stop_signal_received:
        p2p_listener_transport_local_ref = None
        active_rendezvous_websocket = None # Ensure it's None before attempting connection
        try:
            # Ensure explicit proxy=None to avoid automatic system proxy usage (websockets v15.0+ behavior)
            async with websockets.connect(rendezvous_ws_url, 
                                        ping_interval=ping_interval, 
                                        ping_timeout=ping_timeout,
                                        proxy=None) as ws_to_rendezvous:
                active_rendezvous_websocket = ws_to_rendezvous # Set active connection
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
                last_stun_recheck_time = time.monotonic() # Set time after first attempt

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
                        else: print(f"Worker '{worker_id}': Unhandled message from Rendezvous: {msg_type}")
                    except asyncio.TimeoutError: 
                        # This is expected if no messages from rendezvous, allows periodic tasks.
                        pass
                    except websockets.exceptions.ConnectionClosed as e_conn_closed: 
                        print(f"Worker '{worker_id}': Rendezvous WS closed by server during recv: {e_conn_closed}.") 
                        break # Break inner message loop
                    except Exception as e_recv: 
                        print(f"Worker '{worker_id}': Error in WS recv loop: {e_recv}") 
                        break # Break inner message loop
                    
                    # Periodic STUN Re-check Logic
                    current_time = time.monotonic()
                    if current_time - last_stun_recheck_time > STUN_RECHECK_INTERVAL:
                        print(f"Worker '{worker_id}': Performing periodic STUN UDP endpoint check (interval: {STUN_RECHECK_INTERVAL}s)...")
                        try:
                            old_ip, old_port = our_stun_discovered_udp_ip, our_stun_discovered_udp_port
                            # Use main_udp_sock for re-check
                            stun_recheck_success = await discover_and_report_stun_udp_endpoint(ws_to_rendezvous)
                            if stun_recheck_success:
                                if our_stun_discovered_udp_ip != old_ip or our_stun_discovered_udp_port != old_port:
                                    print(f"Worker '{worker_id}': Periodic STUN: UDP endpoint changed from {old_ip}:{old_port} to {our_stun_discovered_udp_ip}:{our_stun_discovered_udp_port}. Update sent.")
                                else:
                                    print(f"Worker '{worker_id}': Periodic STUN: UDP endpoint re-verified and unchanged: {our_stun_discovered_udp_ip}:{our_stun_discovered_udp_port}. Update (re)sent.")
                            else:
                                print(f"Worker '{worker_id}': Periodic STUN UDP endpoint discovery failed logically (e.g., STUN server error). Will retry after interval.")
                            last_stun_recheck_time = current_time # Update time after attempt (logical success/failure)
                        except websockets.exceptions.ConnectionClosed as e_stun_conn_closed:
                            print(f"Worker '{worker_id}': Connection closed during periodic STUN update: {e_stun_conn_closed}. Will attempt to reconnect.")
                            break # Break inner message/task loop, outer loop will retry connection.
                        except Exception as e_stun_general:
                            # Catch other potential errors from STUN discovery anomd log, but don't necessarily break the WS connection
                            # unless it's a ConnectionClosed error (handled above).
                            print(f"Worker '{worker_id}': Error during periodic STUN UDP endpoint check: {type(e_stun_general).__name__} - {e_stun_general}. Will retry STUN after interval.")
                            last_stun_recheck_time = current_time # Update time so we don't immediately retry STUN

                # End of inner message/task loop
                if stop_signal_received: break 

        except websockets.exceptions.ConnectionClosed as e_outer_closed:
            print(f"Worker '{worker_id}': Rendezvous WS connection closed before or during connect: {e_outer_closed}")
        except Exception as e_ws_connect: 
            print(f"Worker '{worker_id}': Error in WS connection loop: {type(e_ws_connect).__name__} - {e_ws_connect}. Retrying...")
        finally: 
            active_rendezvous_websocket = None # Clear active connection on exit/error
            if p2p_listener_transport_local_ref: 
                print(f"Worker '{worker_id}': Closing local P2P UDP transport (asyncio wrapper) from this WS session.")
                p2p_listener_transport_local_ref.close()
                # If this transport was the global one, clear the global reference
                if p2p_udp_transport == p2p_listener_transport_local_ref:
                    p2p_udp_transport = None
                udp_listener_active = False # Allow re-creation of transport on next connection
        if not stop_signal_received: await asyncio.sleep(10)
        else: break

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
    try:
        await rendezvous_client_task
    except asyncio.CancelledError:
        print(f"Worker '{worker_id}': Rendezvous client task was cancelled.")
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

if __name__ == "__main__":
    print(f"WORKER SCRIPT (ID: {worker_id}): Initializing...")
    rendezvous_base_url_env = os.environ.get("RENDEZVOUS_SERVICE_URL")
    if not rendezvous_base_url_env: print("CRITICAL ERROR: RENDEZVOUS_SERVICE_URL environment variable not set. Exiting worker."); exit(1) 
    signal.signal(signal.SIGTERM, handle_shutdown_signal); signal.signal(signal.SIGINT, handle_shutdown_signal)
    try: asyncio.run(main_async_orchestrator())
    except KeyboardInterrupt: print(f"Worker '{worker_id}' interrupted by user."); stop_signal_received = True 
    except Exception as e_main_run: print(f"Worker '{worker_id}' CRITICAL ERROR in __main__: {type(e_main_run).__name__} - {e_main_run}")
    finally: print(f"Worker '{worker_id}' main EXIT.") 