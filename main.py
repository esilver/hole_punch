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
import struct
import itertools

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

# Trino configuration
TRINO_MODE = os.environ.get("TRINO_MODE", "").lower() in ["true", "1", "yes", "on"]
TRINO_LOCAL_PORT = int(os.environ.get("TRINO_LOCAL_PORT", "8081"))  # Local Trino worker port
TRINO_PROXY_PORT = int(os.environ.get("TRINO_PROXY_PORT", "18080"))  # HTTP proxy port for Trino
TRINO_COORDINATOR_ID = os.environ.get("TRINO_COORDINATOR_ID", "")  # Worker ID of the coordinator

ui_websocket_clients: Set[websockets.WebSocketServerProtocol] = set()

# Benchmark related globals
benchmark_sessions: Dict[str, Dict] = {} # Key: peer_addr_str, Value: {received_bytes, received_chunks, start_time, total_chunks (from sender)}
BENCHMARK_CHUNK_SIZE = 1024 # 1KB

# Add these constants near the top with other environment variables
STUN_MAX_RETRIES = int(os.environ.get("STUN_MAX_RETRIES", "3"))
STUN_RETRY_DELAY_SEC = float(os.environ.get("STUN_RETRY_DELAY_SEC", "2.0"))

# HTTP-over-UDP framing constants
HTTP_UDP_MAGIC = 0xAB  # Magic byte to identify HTTP-over-UDP frames
HTTP_UDP_FMT = "!B B I"  # magic (1 byte), flags (1 byte), msg-id (4 bytes)
HTTP_UDP_ACK = 0x80
HTTP_UDP_CHUNK = 0x40  # Flag indicating this is a chunk
HTTP_UDP_FINAL = 0x20  # Flag indicating final chunk
HTTP_UDP_MAX = 1200  # fits under common MTU
HTTP_UDP_HEADER_SIZE = 6  # Size of our header (increased by 1 for magic)
HTTP_UDP_CHUNK_FMT = "!B B I H H"  # magic, flags, msg-id, chunk_num, total_chunks
HTTP_UDP_CHUNK_HEADER_SIZE = 10  # Size of chunk header (increased by 1 for magic)
HTTP_UDP_MAX_PAYLOAD = HTTP_UDP_MAX - HTTP_UDP_CHUNK_HEADER_SIZE  # Max chunk payload
CHUNK_TIMEOUT_SEC = 30.0  # Timeout for incomplete chunk reassembly
PROXY_REQUEST_TIMEOUT_SEC = 30.0 # Timeout for HTTP proxy client requests

def handle_shutdown_signal(signum, frame):
    global stop_signal_received, p2p_udp_transport
    print(f"Shutdown signal ({signum}) received. Worker '{worker_id}' attempting graceful shutdown.")
    stop_signal_received = True
    if p2p_udp_transport:
        try: p2p_udp_transport.close(); print(f"Worker '{worker_id}': P2P UDP transport closed.")
        except Exception as e: print(f"Worker '{worker_id}': Error closing P2P UDP transport: {e}")
    for ws_client in list(ui_websocket_clients):
        async def safe_close(ws):
            try:
                await ws.close(reason="Server shutting down")
            except Exception:
                pass  # Ignore errors during shutdown
        asyncio.create_task(safe_close(ws_client))

async def process_http_request(path: str, request_headers: Headers) -> Optional[Tuple[int, Headers, bytes]]:
    global worker_id, current_p2p_peer_id, TRINO_MODE
    
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
    elif path == "/trino-proxy-status":
        # Provide status about Trino proxy access
        is_coordinator = os.environ.get("IS_COORDINATOR", "").lower() in ["true", "1", "yes", "on"]
        content = f"""
Trino Proxy Status:
- Node Type: {'Coordinator' if is_coordinator else 'Worker'}
- Proxy Port: {TRINO_PROXY_PORT}
- Local Trino Port: {TRINO_LOCAL_PORT}

To access Trino UI:
1. The UI runs on the coordinator at port 8081
2. Use the coordinator's public URL with port 18080 for API access
3. Example query: curl -X POST {'{coordinator-url}'}/v1/statement -d 'SELECT 1'

Note: Full UI access requires direct connection to port 8081.
""".encode()
        return (200, [("Content-Type", "text/plain"), ("Content-Length", str(len(content)))], content)
    elif path == "/v1/info" and TRINO_MODE:
        # Trino worker info endpoint for discovery
        info = {
            "nodeId": worker_id,
            "environment": "holepunch-p2p",
            "external": f"http://localhost:{TRINO_PROXY_PORT}",
            "internal": f"http://localhost:{TRINO_PROXY_PORT}",
            "internalHttps": None,
            "nodeVersion": "1.0.0",
            "coordinator": os.environ.get("IS_COORDINATOR", "").lower() in ["true", "1", "yes", "on"],
            "startTime": time.time()
        }
        content = json.dumps(info).encode()
        headers = Headers([("Content-Type", "application/json"), ("Content-Length", str(len(content)))])
        return (200, headers, content)
    elif path == "/v1/announcement" and TRINO_MODE:
        # Trino discovery endpoint - list all known workers
        workers = []
        if worker_id:
            workers.append({
                "id": worker_id,
                "internalUri": f"http://localhost:{TRINO_PROXY_PORT}",
                "nodeVersion": "1.0.0",
                "coordinator": worker_id == TRINO_COORDINATOR_ID
            })
        if current_p2p_peer_id:
            workers.append({
                "id": current_p2p_peer_id,
                "internalUri": f"http://localhost:{TRINO_PROXY_PORT}",
                "nodeVersion": "1.0.0",
                "coordinator": current_p2p_peer_id == TRINO_COORDINATOR_ID
            })
        content = json.dumps(workers).encode()
        headers = Headers([("Content-Type", "application/json"), ("Content-Length", str(len(content)))])
        return (200, headers, content)
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

async def test_http_proxy_internal(test_message: str, ui_ws: websockets.WebSocketServerProtocol):
    """Test the HTTP proxy by making an internal request"""
    global worker_id, current_p2p_peer_addr
    
    if not current_p2p_peer_addr:
        await ui_ws.send(json.dumps({"type": "http_proxy_error", "error": "No P2P peer connected"}))
        return
    
    try:
        print(f"Worker '{worker_id}': Testing HTTP proxy with message: {test_message}")
        reader, writer = await asyncio.open_connection('127.0.0.1', 8080)
        
        # Send HTTP request
        request = f"POST /echo HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(test_message)}\r\n\r\n{test_message}"
        print(f"Worker '{worker_id}': Sending HTTP request to proxy: {request[:50]}...")
        writer.write(request.encode())
        await writer.drain()
        
        # Read response with timeout
        try:
            response = await asyncio.wait_for(reader.read(1024), timeout=5.0)
            response_str = response.decode('utf-8', errors='ignore')
            print(f"Worker '{worker_id}': Received response from proxy: {response_str[:100]}...")
            
            # Parse response
            if b"HTTP/1.1 200 OK" in response:
                # Extract body
                parts = response_str.split("\r\n\r\n", 1)
                body = parts[1] if len(parts) > 1 else "No body"
                await ui_ws.send(json.dumps({"type": "http_proxy_response", "response": body}))
            elif b"HTTP/1.1 503" in response:
                await ui_ws.send(json.dumps({"type": "http_proxy_error", "error": "No peer connected (503)"}))
            elif b"HTTP/1.1 504" in response:
                await ui_ws.send(json.dumps({"type": "http_proxy_error", "error": "Request timeout (504)"}))
            else:
                await ui_ws.send(json.dumps({"type": "http_proxy_error", "error": f"Unexpected response: {response_str[:100]}"}))
        except asyncio.TimeoutError:
            await ui_ws.send(json.dumps({"type": "http_proxy_error", "error": "Timeout waiting for proxy response"}))
        
        writer.close()
        await writer.wait_closed()
        
    except ConnectionRefusedError:
        await ui_ws.send(json.dumps({"type": "http_proxy_error", "error": "HTTP proxy not running on port 8080"}))
    except Exception as e:
        error_msg = f"Failed to test HTTP proxy: {type(e).__name__}: {e}"
        print(f"Worker '{worker_id}': {error_msg}")
        await ui_ws.send(json.dumps({"type": "http_proxy_error", "error": error_msg}))

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
                elif msg_type == "test_http_proxy":
                    test_message = message.get("message", "")
                    print(f"Worker '{worker_id}': UI requested HTTP proxy test with message: {test_message}")
                    # Test the HTTP proxy internally
                    asyncio.create_task(test_http_proxy_internal(test_message, websocket))
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
        self.http_proxy_server = None  # Will be set after connection
        # Start msg_id counter from a random value to avoid collisions between workers
        import random
        start_id = random.randint(1000, 1000000)
        self.http_msg_counter = itertools.count(start_id).__next__
        self.http_pending_requests: Dict[int, asyncio.Future] = {}  # msg_id -> asyncio.Future
        self.incoming_chunks = {}  # msg_id -> {chunks: {chunk_num: data}, total: int, received: set, start_time: float}
        self.outgoing_chunk_tracking = {}  # msg_id -> {acked_chunks: set, total: int}
        print(f"Worker '{self.worker_id}': P2PUDPProtocol instance created.")

    async def _execute_handler_safely(self, coro, handler_name: str):
        try:
            await coro
        except asyncio.CancelledError:
            print(f"Worker '{self.worker_id}': {handler_name} task was cancelled.")
            # Re-raise if cancellation needs to propagate, but often for created tasks,
            # just logging is fine if they are meant to be independent.
        except Exception as e:
            print(f"Worker '{self.worker_id}': Unhandled exception in {handler_name}: {type(e).__name__} - {e}")
            # Only print traceback if we have a valid event loop
            try:
                loop = asyncio.get_running_loop()
                if loop and not loop.is_closed():
                    import traceback
                    traceback.print_exc()
            except RuntimeError:
                # No running event loop
                pass
            except Exception:
                pass  # Ignore errors during traceback printing

    def connection_made(self, transport: asyncio.DatagramTransport):
        global p2p_udp_transport 
        self.transport = transport
        p2p_udp_transport = transport 
        local_addr = transport.get_extra_info('sockname')
        print(f"Worker '{self.worker_id}': P2P UDP listener active on {local_addr} (Internal Port: {INTERNAL_UDP_PORT}).")
        # Start HTTP proxy server when UDP is ready
        asyncio.create_task(self.start_http_proxy())
        # Start chunk cleanup task
        asyncio.create_task(self._chunk_cleanup_task())
    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        global current_p2p_peer_addr, current_p2p_peer_id, benchmark_sessions
        
        # Check if this is an HTTP-over-UDP frame
        if len(data) >= HTTP_UDP_HEADER_SIZE:  # Minimum frame size
            try:
                magic, flags, msg_id = struct.unpack_from(HTTP_UDP_FMT, data)
                
                # Check magic byte first
                if magic != HTTP_UDP_MAGIC:
                    raise ValueError(f"Invalid magic byte: {magic:#x}")
                
                # Validate flags - must be a known combination
                valid_flags = flags == 0 or flags & HTTP_UDP_ACK or flags & HTTP_UDP_CHUNK
                if not valid_flags:
                    raise ValueError(f"Invalid flags: {flags}")
                
                # Handle ACK
                if flags & HTTP_UDP_ACK:
                    print(f"Worker '{self.worker_id}': Received HTTP ACK for msg_id={msg_id} from {addr}")
                    # Track chunk ACKs if this is for a chunked message
                    if flags & HTTP_UDP_CHUNK and len(data) >= HTTP_UDP_CHUNK_HEADER_SIZE:
                        _, _, _, chunk_num, _ = struct.unpack_from(HTTP_UDP_CHUNK_FMT, data)
                        if msg_id in self.outgoing_chunk_tracking:
                            self.outgoing_chunk_tracking[msg_id]['acked_chunks'].add(chunk_num)
                            print(f"Worker '{self.worker_id}': Chunk {chunk_num} ACKed for msg_id={msg_id}")
                    return
                
                # Handle chunked frames
                if flags & HTTP_UDP_CHUNK:
                    if len(data) < HTTP_UDP_CHUNK_HEADER_SIZE:
                        print(f"Worker '{self.worker_id}': Invalid chunk header size")
                        return
                    
                    _, _, _, chunk_num, total_chunks = struct.unpack_from(HTTP_UDP_CHUNK_FMT, data)
                    chunk_payload = data[HTTP_UDP_CHUNK_HEADER_SIZE:]
                    
                    # Validate chunk numbers
                    if chunk_num >= total_chunks:
                        print(f"Worker '{self.worker_id}': Invalid chunk {chunk_num}/{total_chunks} - chunk_num must be < total_chunks")
                        return
                    
                    # Send ACK for this chunk
                    ack_frame = struct.pack(HTTP_UDP_CHUNK_FMT, HTTP_UDP_MAGIC, HTTP_UDP_ACK | HTTP_UDP_CHUNK, msg_id, chunk_num, total_chunks)
                    self.transport.sendto(ack_frame, addr)
                    
                    # Handle chunk reassembly
                    asyncio.create_task(self._handle_incoming_chunk(msg_id, chunk_num, total_chunks, chunk_payload, flags & HTTP_UDP_FINAL, addr))
                    return
                
                # Regular (non-chunked) frame
                elif flags == 0:
                    payload = data[HTTP_UDP_HEADER_SIZE:]
                    print(f"Worker '{self.worker_id}': Received HTTP frame, msg_id={msg_id}, from {addr}, size={len(payload)}, payload_start={payload[:50]}...")
                    # Send ACK
                    ack_frame = struct.pack(HTTP_UDP_FMT, HTTP_UDP_MAGIC, HTTP_UDP_ACK, msg_id)
                    self.transport.sendto(ack_frame, addr)
                    
                    # Check if it's an HTTP request
                    if TRINO_MODE:
                        # In Trino mode, forward to local Trino worker
                        if payload.startswith(b"GET ") or payload.startswith(b"POST ") or payload.startswith(b"PUT ") or payload.startswith(b"DELETE "):
                            print(f"Worker '{self.worker_id}': Processing Trino request from {addr}")
                            # Process immediately without wrapper for better performance
                            asyncio.create_task(self._handle_remote_trino_request(payload, msg_id, addr))
                            return
                    else:
                        # Echo mode
                        if payload.startswith(b"GET /echo") or payload.startswith(b"POST /echo"):
                            # Handle echo request
                            print(f"Worker '{self.worker_id}': Processing echo request from {addr}")
                            parts = payload.split(b"\r\n\r\n", 1)
                            body = parts[1] if len(parts) > 1 else b"Echo from remote peer"
                            response = f"HTTP/1.1 200 OK\r\nContent-Length:{len(body)}\r\n\r\n".encode() + body
                            response_frame = struct.pack(HTTP_UDP_FMT, HTTP_UDP_MAGIC, 0, msg_id) + response
                            self.transport.sendto(response_frame, addr)
                            print(f"Worker '{self.worker_id}': Sent echo response to {addr}, body={body[:30]}...")
                            return
                    
                    if payload.startswith(b"HTTP/1.1"):
                        # This is an HTTP response for a pending request
                        print(f"Worker '{self.worker_id}': Received non-chunked HTTP response for msg_id={msg_id}")
                        fut = self.http_pending_requests.pop(msg_id, None)
                        if fut:
                            if not fut.done():
                                fut.set_result(payload)
                                print(f"Worker '{self.worker_id}': Set result for non-chunked msg_id={msg_id}. Remaining pending_requests: {list(self.http_pending_requests.keys())}")
                            else:
                                print(f"Worker '{self.worker_id}': Future for non-chunked msg_id={msg_id} was already done. Exception: {fut.exception()}. Remaining pending_requests: {list(self.http_pending_requests.keys())}")
                        else:
                            # This case implies the future was already removed (e.g. timed out by handle_http_client)
                            print(f"Worker '{self.worker_id}': Late non-chunked HTTP response for msg_id={msg_id}. No pending future. Data: {payload[:60]}")
                        return
            except (struct.error, ValueError) as e:
                # Not an HTTP frame, continue with JSON processing
                pass
        
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
    
    async def start_http_proxy(self):
        """Start the HTTP proxy server on localhost:8080"""
        try:
            proxy_port = TRINO_PROXY_PORT if TRINO_MODE else 8080
            self.http_proxy_server = await asyncio.start_server(
                self.handle_http_client, '127.0.0.1', proxy_port)
            addr = self.http_proxy_server.sockets[0].getsockname()
            mode = "Trino mode" if TRINO_MODE else "echo mode"
            print(f"Worker '{self.worker_id}': HTTP-over-UDP proxy started on {addr} in {mode}")
        except Exception as e:
            print(f"Worker '{self.worker_id}': Failed to start HTTP proxy: {e}")
    
    async def handle_http_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle incoming HTTP connections from local clients"""
        global current_p2p_peer_addr, TRINO_MODE
        client_addr = writer.get_extra_info('peername')
        print(f"Worker '{self.worker_id}': HTTP proxy received connection from {client_addr}")
        request_line_strip = b"" # For logging
        msg_id = -1 # For logging in finally
        request_summary_for_logs = ""

        try:
            # Read the HTTP request headers first to determine routing
            request_line = await reader.readline()
            if not request_line: # Connection closed prematurely
                print(f"Worker '{self.worker_id}': HTTP proxy connection from {client_addr} closed before request line.")
                return
            request_line_strip = request_line.strip()
            request_summary_for_logs = request_line_strip[:80].decode('utf-8', 'ignore') # For log messages

            headers = []
            headers.append(request_line)
            
            # Read headers
            content_length = 0
            while True:
                line = await reader.readline()
                if not line: # Connection closed prematurely
                    print(f"Worker '{self.worker_id}': HTTP proxy connection from {client_addr} closed while reading headers for req: {request_summary_for_logs}")
                    return
                headers.append(line)
                if line == b'\r\n':
                    break
                if line.lower().startswith(b'content-length:'):
                    content_length = int(line.split(b':', 1)[1].strip())
            
            # Read body if present
            body = b''
            if content_length > 0:
                body = await reader.read(content_length)
                if len(body) < content_length:
                    print(f"Worker '{self.worker_id}': HTTP proxy connection from {client_addr} closed while reading body for req: {request_summary_for_logs}. Expected {content_length}, got {len(body)}")
                    return

            # Reconstruct full request
            request = b''.join(headers) + body
            print(f"Worker '{self.worker_id}': HTTP proxy received full request: {request_summary_for_logs}")
            
            if TRINO_MODE:
                # In Trino mode, determine routing based on path
                path = request_line.split(b' ')[1].decode('utf-8')
                if self._is_local_trino_path(path):
                    # Forward to local Trino worker
                    print(f"Worker '{self.worker_id}': HTTP proxy forwarding req {request_summary_for_logs} to LOCAL Trino.")
                    await self._forward_to_local_trino(request, writer)
                    return # Local handling complete
                else:
                    # Forward to peer via UDP tunnel
                    # Note: The peer will forward this to their local Trino - no further routing logic applied
                    is_coordinator = os.environ.get("IS_COORDINATOR", "").lower() in ["true", "1", "yes", "on"]
                    print(f"Worker '{self.worker_id}': Path {path} not local. I am {'COORDINATOR' if is_coordinator else 'WORKER'}. Forwarding to peer.")
                    if not current_p2p_peer_addr:
                        print(f"Worker '{self.worker_id}': HTTP proxy - no peer for Trino req {request_summary_for_logs}")
                        error_response = b"HTTP/1.1 503 Service Unavailable\\r\\nContent-Length: 17\\r\\n\\r\\nNo peer connected"
                        writer.write(error_response)
                        await writer.drain()
                        return
            else: # Echo mode
                if not current_p2p_peer_addr:
                    print(f"Worker '{self.worker_id}': HTTP proxy - no peer for echo req {request_summary_for_logs}")
                    error_response = b"HTTP/1.1 503 Service Unavailable\\r\\nContent-Length: 17\\r\\n\\r\\nNo peer connected"
                    writer.write(error_response)
                    await writer.drain()
                    return
            
            # Send request to peer via UDP
            msg_id = self.http_msg_counter() # Assign here for logging
            frame = struct.pack(HTTP_UDP_FMT, HTTP_UDP_MAGIC, 0, msg_id) + request
            
            fut = asyncio.get_event_loop().create_future()
            self.http_pending_requests[msg_id] = fut
            print(f"Worker '{self.worker_id}': HTTP proxy CREATED future for msg_id={msg_id} (req: {request_summary_for_logs}). Pending: {list(self.http_pending_requests.keys())}")
            
            if not self.transport:
                 print(f"Worker '{self.worker_id}': HTTP proxy NO TRANSPORT to send msg_id={msg_id} (req: {request_summary_for_logs}) to peer.")
                 self.http_pending_requests.pop(msg_id, None) # Clean up future
                 # fut.cancel() # Or fut.set_exception(...)
                 error_response = b"HTTP/1.1 500 Internal Server Error\\r\\nContent-Length: 28\\r\\n\\r\\nUDP transport not available"
                 writer.write(error_response)
                 await writer.drain()
                 return

            self.transport.sendto(frame, current_p2p_peer_addr)
            print(f"Worker '{self.worker_id}': HTTP proxy SENT frame to {current_p2p_peer_addr}, msg_id={msg_id} (req: {request_summary_for_logs})")
            
            try:
                print(f"Worker '{self.worker_id}': HTTP proxy WAITING for response, msg_id={msg_id} (req: {request_summary_for_logs}), timeout={PROXY_REQUEST_TIMEOUT_SEC}s. Pending: {list(self.http_pending_requests.keys())}")
                response = await asyncio.wait_for(fut, timeout=PROXY_REQUEST_TIMEOUT_SEC)
                print(f"Worker '{self.worker_id}': HTTP proxy GOT response for msg_id={msg_id} (req: {request_summary_for_logs}). Resp: {response[:60].decode('utf-8','ignore')}... Pending after success: {list(self.http_pending_requests.keys())}")
                writer.write(response)
            except asyncio.TimeoutError: # This is specifically for wait_for timing out
                log_prefix_immediate = f"Worker '{self.worker_id}': HTTP proxy TIMEOUT (by asyncio.wait_for) for msg_id={msg_id} (req: {request_summary_for_logs})."
                print(f"{log_prefix_immediate} Attempting to process future state.")

                popped_fut_on_timeout = self.http_pending_requests.pop(msg_id, None)
                future_was_done = False
                future_was_cancelled_by_us = False
                future_exception_info = "NoExceptionRetrieved"

                try:
                    if fut.done():
                        future_was_done = True
                        try:
                            actual_exception = fut.exception()
                            future_exception_info = f"{type(actual_exception).__name__} - {actual_exception}"
                            print(f"{log_prefix_immediate} Future was ALREADY DONE. Actual exception on future: {future_exception_info}. Popped now: {popped_fut_on_timeout is not None}.")
                        except Exception as e_fut_exc:
                            future_exception_info = f"Error getting future exception: {e_fut_exc}"
                            print(f"{log_prefix_immediate} Future was ALREADY DONE but errored on .exception(): {e_fut_exc}. Popped now: {popped_fut_on_timeout is not None}.")
                    else: 
                        print(f"{log_prefix_immediate} Future was PENDING and wait_for timed it out. Attempting to cancel. Popped now: {popped_fut_on_timeout is not None}.")
                        try:
                            if not fut.cancelled(): 
                                fut.cancel()
                                future_was_cancelled_by_us = True
                                print(f"{log_prefix_immediate} Future cancellation initiated.")
                        except Exception as e_fut_cancel:
                            print(f"{log_prefix_immediate} Error during future cancellation: {e_fut_cancel}.")
                except Exception as e_outer_fut_check:
                     print(f"{log_prefix_immediate} Error checking/processing future state: {e_outer_fut_check}.")

                print(f"{log_prefix_immediate} Summary: Done={future_was_done}, CancelledByUs={future_was_cancelled_by_us}, ExceptionInfo='{future_exception_info}'. Remaining pending_requests: {list(self.http_pending_requests.keys())}")
                timeout_response = b"HTTP/1.1 504 Gateway Timeout\\r\\nContent-Length: 15\\r\\n\\r\\nRequest timeout"
                writer.write(timeout_response)
            except Exception as e_wait: # Handles exceptions if 'fut' itself completes with an error
                # This means 'fut' completed with an exception (e.g., set by _chunk_cleanup_task or other logic)
                # and that exception was re-raised by 'await fut' (implicitly by wait_for).
                self.http_pending_requests.pop(msg_id, None) # Ensure cleanup
                print(f"Worker '{self.worker_id}': HTTP proxy request for msg_id={msg_id} (req: {request_summary_for_logs}) future completed with EXCEPTION: {type(e_wait).__name__} - {e_wait}. Pending: {list(self.http_pending_requests.keys())}")
                error_response_body = f"Proxied request failed: {type(e_wait).__name__}".encode('utf-8')
                error_response = f"HTTP/1.1 502 Bad Gateway\\r\\nContent-Length: {len(error_response_body)}\\r\\n\\r\\n".encode('utf-8') + error_response_body
                writer.write(error_response)
            
            await writer.drain()
        except ConnectionRefusedError:
             print(f"Worker '{self.worker_id}': HTTP proxy ConnectionRefusedError for req {request_summary_for_logs}, msg_id={msg_id}")
             if not writer.is_closing():
                error_response = b"HTTP/1.1 503 Service Unavailable\\r\\nContent-Length: 20\\r\\n\\r\\nConnection refused" # Should be rare here
                writer.write(error_response)
                await writer.drain()
        except Exception as e_http_client:
            print(f"Worker '{self.worker_id}': HTTP proxy EXCEPTION in handle_http_client for msg_id={msg_id} (req: {request_summary_for_logs}): {type(e_http_client).__name__} - {e_http_client}")
            if not writer.is_closing():
                try:
                    err_resp = b"HTTP/1.1 500 Internal Server Error\\r\\nContent-Length: 22\\r\\n\\r\\nProxy internal error"
                    writer.write(err_resp)
                    await writer.drain()
                except Exception as e_final_err:
                    print(f"Worker '{self.worker_id}': HTTP proxy failed to send 500 error for req {request_summary_for_logs}: {e_final_err}")
        finally:
            # Ensure writer is closed.
            if not writer.is_closing():
                writer.close()
                # await writer.wait_closed() # wait_closed() can hang if the other side RSTs
            print(f"Worker '{self.worker_id}': HTTP proxy connection from {client_addr} CLEANUP. (Processed msg_id={msg_id}, req: {request_summary_for_logs})")
    
    def _is_local_trino_path(self, path: str) -> bool:
        """Determine if a path should be handled by the local Trino worker"""
        # Check if this is the coordinator
        is_coordinator = os.environ.get("IS_COORDINATOR", "").lower() in ["true", "1", "yes", "on"]
        
        # These paths are handled by all nodes (coordinator and workers)
        common_paths = [
            '/v1/task',  # Task management
            '/v1/info',  # Worker info
            '/v1/status',  # Worker status
            '/v1/thread',  # Thread dumps
            '/v1/announcement',  # Service announcements - each node handles its own
            '/v1/service',  # Service discovery - handled locally by each node
        ]
        
        # These paths are only handled by the coordinator
        coordinator_only_paths = [
            '/v1/statement',  # Query submission (SQL queries)
            '/ui',  # Trino UI
            '/v1/cluster',  # Cluster overview
            '/v1/query',  # Query information
        ]
        
        # Check common paths first
        if any(path.startswith(p) for p in common_paths):
            return True
            
        # Check coordinator-only paths
        if is_coordinator and any(path.startswith(p) for p in coordinator_only_paths):
            return True
            
        return False
    
    async def _forward_to_local_trino(self, request: bytes, writer: asyncio.StreamWriter):
        """Forward request to local Trino worker"""
        try:
            # Connect to local Trino worker
            trino_reader, trino_writer = await asyncio.open_connection('127.0.0.1', TRINO_LOCAL_PORT)
            
            # Forward request
            trino_writer.write(request)
            await trino_writer.drain()
            
            # Read response and forward back
            # For Trino, we need to handle streaming responses
            while True:
                chunk = await trino_reader.read(4096)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
            
            trino_writer.close()
            await trino_writer.wait_closed()
            
        except Exception as e:
            print(f"Worker '{self.worker_id}': Error forwarding to local Trino: {e}")
            error_response = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 21\r\n\r\nLocal Trino error"
            writer.write(error_response)
            await writer.drain()
    
    async def _handle_remote_trino_request(self, request: bytes, msg_id: int, addr: Tuple[str, int]):
        """Handle Trino request from remote peer by forwarding to local Trino"""
        start_time = time.time()
        print(f"Worker '{self.worker_id}': _handle_remote_trino_request START for msg_id={msg_id} at {time.strftime('%H:%M:%S', time.localtime(start_time))}")
        print(f"Worker '{self.worker_id}': Request preview: {request[:100]}")
        
        # Important: ANY request that comes via UDP tunnel should be forwarded to local Trino
        # We should NOT apply routing logic here - that was already done by the sender
        try:
            # Connect to local Trino worker with timeout
            print(f"Worker '{self.worker_id}': Attempting to connect to local Trino on 127.0.0.1:{TRINO_LOCAL_PORT}")
            try:
                trino_reader, trino_writer = await asyncio.wait_for(
                    asyncio.open_connection('127.0.0.1', TRINO_LOCAL_PORT),
                    timeout=2.0  # 2 second timeout for connection
                )
                print(f"Worker '{self.worker_id}': Successfully connected to local Trino")
            except asyncio.TimeoutError:
                raise ConnectionError(f"Timeout connecting to Trino on port {TRINO_LOCAL_PORT}")
            
            # Forward request
            trino_writer.write(request)
            await trino_writer.drain()
            print(f"Worker '{self.worker_id}': Forwarded request to local Trino")
            
            # Read response with timeout to avoid hanging
            response_parts = []
            total_read = 0
            read_timeout = 2.0  # Reduced initial timeout
            
            try:
                # Read response in chunks with timeout
                while True:
                    try:
                        chunk = await asyncio.wait_for(trino_reader.read(8192), timeout=read_timeout)
                        if not chunk:
                            print(f"Worker '{self.worker_id}': End of response stream")
                            break
                        response_parts.append(chunk)
                        total_read += len(chunk)
                        print(f"Worker '{self.worker_id}': Read chunk from Trino, size={len(chunk)}, total={total_read}")
                        # For subsequent chunks, use very short timeout
                        read_timeout = 0.5
                    except asyncio.TimeoutError:
                        print(f"Worker '{self.worker_id}': Read timeout after {total_read} bytes - assuming response complete")
                        break
            except Exception as e:
                print(f"Worker '{self.worker_id}': Error reading response: {type(e).__name__}: {e}")
                # Continue with what we have
            
            response = b''.join(response_parts)
            print(f"Worker '{self.worker_id}': Got full response from Trino, size={len(response)}")
            
            # Send response back via UDP using chunking if needed
            print(f"Worker '{self.worker_id}': Sending response back via UDP for msg_id={msg_id}")
            await self._send_chunked_response(response, msg_id, addr)
            print(f"Worker '{self.worker_id}': Response sent successfully for msg_id={msg_id}")
            
            trino_writer.close()
            await trino_writer.wait_closed()
            
        except ConnectionRefusedError as e:
            print(f"Worker '{self.worker_id}': ConnectionRefusedError - Trino not running on port {TRINO_LOCAL_PORT}? (msg_id={msg_id})")
            error_response_body = b"Trino service not available on port " + str(TRINO_LOCAL_PORT).encode()
            error_response = f"HTTP/1.1 502 Bad Gateway\r\nContent-Length: {len(error_response_body)}\r\n\r\n".encode() + error_response_body
            
            if self.transport:
                try:
                    print(f"Worker '{self.worker_id}': Sending 502 error response for msg_id={msg_id}")
                    error_frame = struct.pack(HTTP_UDP_FMT, HTTP_UDP_MAGIC, 0, msg_id) + error_response
                    self.transport.sendto(error_frame, addr)
                    print(f"Worker '{self.worker_id}': 502 error response SENT for msg_id={msg_id}")
                except Exception as send_exc:
                    print(f"Worker '{self.worker_id}': Failed to send error response for msg_id={msg_id}: {send_exc}")
            else:
                print(f"Worker '{self.worker_id}': Transport not available to send error response for msg_id={msg_id}")
                
        except Exception as e:
            print(f"Worker '{self.worker_id}': Error handling remote Trino request: {type(e).__name__}: {e} (msg_id={msg_id})")
            error_response_body = f"Local Trino processing error: {type(e).__name__}".encode()
            error_response = f"HTTP/1.1 502 Bad Gateway\r\nContent-Length: {len(error_response_body)}\r\n\r\n".encode() + error_response_body
            
            if self.transport:
                try:
                    print(f"Worker '{self.worker_id}': Sending 502 error response for msg_id={msg_id}")
                    error_frame = struct.pack(HTTP_UDP_FMT, HTTP_UDP_MAGIC, 0, msg_id) + error_response
                    self.transport.sendto(error_frame, addr)
                    print(f"Worker '{self.worker_id}': 502 error response SENT for msg_id={msg_id}")
                except Exception as send_exc:
                    print(f"Worker '{self.worker_id}': Failed to send error response for msg_id={msg_id}: {send_exc}")
            else:
                print(f"Worker '{self.worker_id}': Transport not available to send error response for msg_id={msg_id}")

    async def _handle_incoming_chunk(self, msg_id: int, chunk_num: int, total_chunks: int, payload: bytes, is_final: bool, addr: Tuple[str, int]):
        """Handle incoming chunk and reassemble when complete"""
        current_time = time.time()
        
        # Initialize tracking for this message if needed
        if msg_id not in self.incoming_chunks:
            self.incoming_chunks[msg_id] = {
                'chunks': {},
                'total': total_chunks,
                'received': set(),
                'start_time': current_time
            }
            print(f"Worker '{self.worker_id}': Starting chunk reassembly for msg_id={msg_id}, expecting {total_chunks} chunks")
        
        chunk_info = self.incoming_chunks[msg_id]
        
        # Store this chunk
        chunk_info['chunks'][chunk_num] = payload
        chunk_info['received'].add(chunk_num)
        
        print(f"Worker '{self.worker_id}': Received chunk {chunk_num}/{total_chunks} for msg_id={msg_id}, size={len(payload)}")
        
        # Check if we have all chunks
        if len(chunk_info['received']) == chunk_info['total']:
            # Reassemble the complete message
            complete_data = b''
            for i in range(chunk_info['total']):
                complete_data += chunk_info['chunks'][i]
            
            print(f"Worker '{self.worker_id}': Reassembled complete message for msg_id={msg_id}, total size={len(complete_data)}")
            
            # Clean up tracking
            del self.incoming_chunks[msg_id]
            
            # Process the complete message
            fut_for_response = self.http_pending_requests.pop(msg_id, None) # Try to pop it
            if fut_for_response:
                # This is a response to our request
                if not fut_for_response.done():
                    fut_for_response.set_result(complete_data)
                    print(f"Worker '{self.worker_id}': Set reassembled result for msg_id={msg_id}. Remaining pending_requests: {list(self.http_pending_requests.keys())}")
                else:
                    print(f"Worker '{self.worker_id}': Future for reassembled msg_id={msg_id} was already done when trying to set result. Exception: {fut_for_response.exception()}. Remaining pending_requests: {list(self.http_pending_requests.keys())}")
            else:
                # This means msg_id was not in http_pending_requests (e.g. timed out by handle_http_client), 
                # so it's either a new incoming request from peer, or a late response to a timed-out one.
                print(f"Worker '{self.worker_id}': Reassembled data for msg_id={msg_id} (not in pending_requests). Processing as potential new req/late resp. Data: {complete_data[:100]}")
                # The complete_data should be an HTTP request if it's a new one, or an HTTP response if it's late.
                if TRINO_MODE and (complete_data.startswith(b"GET ") or complete_data.startswith(b"POST ") or complete_data.startswith(b"PUT ") or complete_data.startswith(b"DELETE ")):
                    handler_coro = self._handle_remote_trino_request(complete_data, msg_id, addr)
                    task_name = f"ReassembledTrinoRequest(msg_id={msg_id}, peer={addr})"
                    asyncio.create_task(self._execute_handler_safely(handler_coro, task_name))
                elif not TRINO_MODE and (complete_data.startswith(b"GET /echo") or complete_data.startswith(b"POST /echo")):
                    parts = complete_data.split(b"\r\n\r\n", 1)
                    body = parts[1] if len(parts) > 1 else b"Echo from remote peer"
                    response = f"HTTP/1.1 200 OK\\r\\nContent-Length:{len(body)}\\r\\n\\r\\n".encode() + body
                    try:
                        await self._send_chunked_response(response, msg_id, addr)
                    except Exception as e_echo_send:
                        print(f"Worker '{self.worker_id}': Error sending chunked echo response for reassembled msg_id={msg_id}: {e_echo_send}")
                # else: it's neither a Trino request nor an Echo request, and no pending future. Could be a late HTTP response.
                # If it was a late HTTP response, it will simply be ignored here if no future. No specific handling needed for that.

    async def _send_chunked_response(self, data: bytes, msg_id: int, addr: Tuple[str, int]):
        """Send a response in chunks if it's too large"""
        print(f"Worker '{self.worker_id}': _send_chunked_response for msg_id={msg_id} to {addr}. Data prefix: {data[:100]}")

        if not self.transport or self.transport.is_closing():
            print(f"Worker '{self.worker_id}': Transport unavailable or closing in _send_chunked_response for msg_id={msg_id}. Cannot send response.")
            return  # Silently return instead of raising to avoid cascading errors

        if len(data) + HTTP_UDP_HEADER_SIZE <= HTTP_UDP_MAX:
            # Small enough to send as regular frame
            frame = struct.pack(HTTP_UDP_FMT, HTTP_UDP_MAGIC, 0, msg_id) + data
            self.transport.sendto(frame, addr)
            print(f"Worker '{self.worker_id}': Sent non-chunked response for msg_id={msg_id}, size={len(data)}")
            return
        
        # Need to chunk the response
        total_chunks = (len(data) + HTTP_UDP_MAX_PAYLOAD - 1) // HTTP_UDP_MAX_PAYLOAD
        self.outgoing_chunk_tracking[msg_id] = {'acked_chunks': set(), 'total': total_chunks}
        
        print(f"Worker '{self.worker_id}': Sending chunked response for msg_id={msg_id}, size={len(data)}, chunks={total_chunks}")
        
        for chunk_num in range(total_chunks):
            start = chunk_num * HTTP_UDP_MAX_PAYLOAD
            end = min(start + HTTP_UDP_MAX_PAYLOAD, len(data))
            chunk_data = data[start:end]
            
            # Set final flag on last chunk
            flags = HTTP_UDP_CHUNK
            if chunk_num == total_chunks - 1:
                flags |= HTTP_UDP_FINAL
            
            # Send chunk
            chunk_frame = struct.pack(HTTP_UDP_CHUNK_FMT, HTTP_UDP_MAGIC, flags, msg_id, chunk_num, total_chunks) + chunk_data
            self.transport.sendto(chunk_frame, addr)
            
            # Only add delay every 100 chunks to avoid overwhelming
            if chunk_num % 100 == 99:
                await asyncio.sleep(0.001)
        
        # TODO: Implement retransmission for un-ACKed chunks
        # For now, just clean up after a delay
        await asyncio.sleep(1.0)
        if msg_id in self.outgoing_chunk_tracking:
            del self.outgoing_chunk_tracking[msg_id]
    
    async def _chunk_cleanup_task(self):
        """Periodically clean up incomplete chunk reassembly"""
        while True:
            await asyncio.sleep(10.0)  # Check every 10 seconds
            current_time = time.time()
            expired_msgs = []
            
            for msg_id, chunk_info in self.incoming_chunks.items():
                if current_time - chunk_info['start_time'] > CHUNK_TIMEOUT_SEC:
                    expired_msgs.append(msg_id)
            
            for msg_id in expired_msgs:
                chunk_info = self.incoming_chunks[msg_id]
                print(f"Worker '{self.worker_id}': Timing out incomplete chunk reassembly for msg_id={msg_id}, "
                      f"received {len(chunk_info['received'])}/{chunk_info['total']} chunks")
                del self.incoming_chunks[msg_id]
                
                # If this was a pending request, fail it
                # Check before pop, then pop it.
                fut_to_timeout = self.http_pending_requests.pop(msg_id, None)
                if fut_to_timeout: # If it was indeed in pending_requests
                    if not fut_to_timeout.done():
                        print(f"Worker '{self.worker_id}': Chunk reassembly timeout for msg_id={msg_id}. Setting TimeoutError on its future. Remaining pending_requests: {list(self.http_pending_requests.keys())}")
                        fut_to_timeout.set_exception(asyncio.TimeoutError(f"Chunk reassembly timeout for msg_id={msg_id}"))
                    else: # Future was already done
                         print(f"Worker '{self.worker_id}': Chunk reassembly timeout for msg_id={msg_id}, but its future was already done. Exception: {fut_to_timeout.exception()}. Remaining pending_requests: {list(self.http_pending_requests.keys())}")
                # else: msg_id was not in http_pending_requests, so no corresponding client future to fail.
    
    def error_received(self, exc: Exception): print(f"Worker '{self.worker_id}': P2P UDP listener error: {exc}")
    def connection_lost(self, exc: Optional[Exception]): 
        global p2p_udp_transport
        print(f"Worker '{self.worker_id}': P2P UDP listener connection lost: {exc if exc else 'Closed normally'}")
        if self.transport == p2p_udp_transport: p2p_udp_transport = None
        if self.http_proxy_server:
            self.http_proxy_server.close()

async def discover_and_report_stun_udp_endpoint(websocket_conn_to_rendezvous):
    global our_stun_discovered_udp_ip, our_stun_discovered_udp_port, worker_id, INTERNAL_UDP_PORT
    
    stun_host = os.environ.get("STUN_HOST", DEFAULT_STUN_HOST)
    stun_port = int(os.environ.get("STUN_PORT", DEFAULT_STUN_PORT))
    
    for attempt in range(1, STUN_MAX_RETRIES + 1):
        print(f"Worker '{worker_id}': STUN discovery attempt {attempt}/{STUN_MAX_RETRIES} via {stun_host}:{stun_port} for local port {INTERNAL_UDP_PORT}.")
        try:
            # The stun.get_ip_info function can raise socket.gaierror, stun.StunException, OSError, etc.
            # Run in executor to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            nat_type, external_ip, external_port = await loop.run_in_executor(
                None,
                stun.get_ip_info,
                "0.0.0.0",
                INTERNAL_UDP_PORT,
                stun_host,
                stun_port
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
        except AttributeError as e_attr:
            # Handle case where StunException doesn't exist
            if "StunException" in str(e_attr):
                print(f"Worker '{worker_id}': STUN library doesn't have StunException attribute")
            else:
                raise  # Re-raise if it's a different AttributeError
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
    global stop_signal_received, p2p_udp_transport, INTERNAL_UDP_PORT, ui_websocket_clients, our_stun_discovered_udp_ip, our_stun_discovered_udp_port, worker_id
    ip_echo_service_url = "https://api.ipify.org"
    ping_interval = float(os.environ.get("PING_INTERVAL_SEC", "25"))
    ping_timeout = float(os.environ.get("PING_TIMEOUT_SEC", "25"))
    udp_listener_active = False
    loop = asyncio.get_running_loop()

    reconnect_failures = 0
    while not stop_signal_received:
        p2p_listener_transport_local_ref = None
        try:
            # After multiple failures, consider regenerating worker ID
            if reconnect_failures >= 3:
                old_worker_id = worker_id
                worker_id = str(uuid.uuid4())
                print(f"Worker '{old_worker_id}': After {reconnect_failures} reconnect failures, generating new worker ID: {worker_id}")
                # Update the WebSocket URL with new worker ID
                ws_scheme = "wss" if rendezvous_ws_url.startswith("wss://") else "ws"
                base_url = rendezvous_ws_url.rsplit('/', 1)[0]  # Remove old worker ID
                rendezvous_ws_url = f"{base_url}/{worker_id}"
                reconnect_failures = 0
            
            print(f"Worker '{worker_id}': Attempting to connect to rendezvous at {rendezvous_ws_url}")
            # Ensure explicit proxy=None to avoid automatic system proxy usage (websockets v15.0+ behavior)
            async with websockets.connect(rendezvous_ws_url, 
                                        ping_interval=ping_interval, 
                                        ping_timeout=ping_timeout,
                                        open_timeout=30,  # 30 second timeout for connection
                                        proxy=None) as ws_to_rendezvous:
                connection_start_time = time.time()
                print(f"Worker '{worker_id}' connected to Rendezvous Service at {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(connection_start_time))}")
                reconnect_failures = 0  # Reset counter on successful connection
                
                # Send immediate keepalive to test connection
                try:
                    await ws_to_rendezvous.send(json.dumps({"type": "echo_request", "payload": "initial_connection_test"}))
                    print(f"Worker '{worker_id}': Sent initial connection test to rendezvous")
                except Exception as e:
                    print(f"Worker '{worker_id}': Failed to send initial connection test: {e}")
                
                try:
                    # Run blocking HTTP request in executor to avoid blocking the event loop
                    loop = asyncio.get_event_loop()
                    response = await loop.run_in_executor(None, lambda: requests.get(ip_echo_service_url, timeout=10))
                    response.raise_for_status()
                    http_public_ip = response.text.strip()
                    await ws_to_rendezvous.send(json.dumps({"type": "register_public_ip", "ip": http_public_ip}))
                    print(f"Worker '{worker_id}' sent HTTP-based IP ({http_public_ip}) to Rendezvous.")
                except Exception as e_http_ip: print(f"Worker '{worker_id}': Error sending HTTP IP: {e_http_ip}")
                
                # Initial STUN discovery
                stun_success_initial = await discover_and_report_stun_udp_endpoint(ws_to_rendezvous)

                # Create a keep-alive task for the WebSocket
                async def send_ws_keepalive():
                    """Send periodic keep-alive messages to prevent WebSocket timeout"""
                    print(f"Worker '{worker_id}': Starting WebSocket keepalive task")
                    keepalive_count = 0
                    
                    # Send initial keepalive immediately
                    try:
                        keepalive_count += 1
                        await ws_to_rendezvous.send(json.dumps({"type": "echo_request", "payload": "keepalive"}))
                        print(f"Worker '{worker_id}': Sent initial echo keepalive #{keepalive_count} to rendezvous")
                    except Exception as e:
                        print(f"Worker '{worker_id}': Error sending initial WebSocket keep-alive: {e}")
                        return
                    
                    while not stop_signal_received:
                        try:
                            await asyncio.sleep(20)  # Send every 20 seconds
                            keepalive_count += 1
                            # Just try to send - if the connection is closed, it will raise an exception
                            await ws_to_rendezvous.send(json.dumps({"type": "echo_request", "payload": "keepalive"}))
                            print(f"Worker '{worker_id}': Sent echo keepalive #{keepalive_count} to rendezvous")
                        except websockets.exceptions.ConnectionClosed:
                            print(f"Worker '{worker_id}': WebSocket closed, stopping keep-alive")
                            break
                        except Exception as e:
                            print(f"Worker '{worker_id}': Error sending WebSocket keep-alive: {e}")
                            break
                    print(f"Worker '{worker_id}': Exiting keepalive task (stop_signal_received={stop_signal_received})")
                
                keepalive_task = asyncio.create_task(send_ws_keepalive())

                if stun_success_initial and not udp_listener_active:
                    # Check if we already have a global UDP transport running
                    if p2p_udp_transport and not p2p_udp_transport.is_closing():
                        print(f"Worker '{worker_id}': P2P UDP transport already active, skipping creation.")
                        udp_listener_active = True
                        p2p_listener_transport_local_ref = p2p_udp_transport
                    else:
                        try:
                            # Create new UDP listener
                            _transport, _protocol = await loop.create_datagram_endpoint(
                                lambda: P2PUDPProtocol(worker_id),
                                local_addr=('0.0.0.0', INTERNAL_UDP_PORT)
                            )
                            p2p_listener_transport_local_ref = _transport
                            # Check immediately without sleep
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
                        elif msg_type == "echo_response": 
                            original_payload = message_data.get('original_payload', '')
                            if original_payload == 'keepalive':
                                print(f"Worker '{worker_id}': Received keepalive echo response")
                            else:
                                print(f"Worker '{worker_id}': Echo Response: {message_data.get('processed_by_rendezvous')}")
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
                        connection_duration = time.time() - connection_start_time
                        print(f"Worker '{worker_id}': Rendezvous WS closed by server during recv: {e_conn_closed}. Connection lasted {connection_duration:.1f} seconds ({connection_duration/60:.1f} minutes)") 
                        break # Break inner message loop
                    except Exception as e_recv: 
                        print(f"Worker '{worker_id}': Error in WS recv loop: {e_recv}") 
                        break # Break inner message loop
                    
                # End of inner message/task loop
                if stop_signal_received: break 

        except websockets.exceptions.ConnectionClosed as e_outer_closed:
            print(f"Worker '{worker_id}': Rendezvous WS connection closed before or during connect: {e_outer_closed}")
            reconnect_failures += 1
        except Exception as e_ws_connect: 
            print(f"Worker '{worker_id}': Error in WS connection loop: {type(e_ws_connect).__name__} - {e_ws_connect}. Retrying...")
            reconnect_failures += 1
        finally: 
            # Cancel the WebSocket keepalive task if it exists
            if 'keepalive_task' in locals() and not keepalive_task.done():
                keepalive_task.cancel()
                try:
                    await keepalive_task
                except asyncio.CancelledError:
                    pass
                print(f"Worker '{worker_id}': Cancelled WebSocket keepalive task")
            
            # Don't close the UDP transport when WebSocket reconnects - they're independent!
            # The UDP transport should remain open for P2P communication
            if stop_signal_received and p2p_listener_transport_local_ref:
                # Only close on shutdown
                print(f"Worker '{worker_id}': Closing local P2P UDP transport due to shutdown.")
                p2p_listener_transport_local_ref.close()
                if p2p_udp_transport == p2p_listener_transport_local_ref:
                    p2p_udp_transport = None
            
            # Always reset this flag so we can properly check UDP state on reconnect
            udp_listener_active = False
            
        if not stop_signal_received: 
            print(f"Worker '{worker_id}': Waiting 10 seconds before reconnection attempt...")
            await asyncio.sleep(10)
        else: 
            break

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