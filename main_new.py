import asyncio
import os
import uuid
import websockets
import signal
import requests
import json
import socket
import stun
import ipaddress
from typing import Optional, Tuple, Set, Dict, List
from pathlib import Path
from websockets.server import serve as websockets_serve
from websockets.http import Headers
import time
import base64
import ssl
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from datetime import datetime, timedelta, timezone

# QUIC imports
from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import QuicEvent, StreamDataReceived, ConnectionIdIssued, DatagramFrameReceived, ConnectionTerminated
from aioquic.tls import SessionTicket

# --- Global Variables ---
worker_id = str(uuid.uuid4())
stop_signal_received = False
quic_dispatcher: Optional['QuicServerDispatcher'] = None
our_stun_discovered_udp_ip: Optional[str] = None
our_stun_discovered_udp_port: Optional[int] = None
current_p2p_peer_id: Optional[str] = None
current_p2p_peer_addr: Optional[Tuple[str, int]] = None

DEFAULT_STUN_HOST = os.environ.get("STUN_HOST", "stun.l.google.com")
DEFAULT_STUN_PORT = int(os.environ.get("STUN_PORT", "19302"))
INTERNAL_UDP_PORT = int(os.environ.get("INTERNAL_UDP_PORT", "8081"))
HTTP_PORT_FOR_UI = int(os.environ.get("PORT", 8080))

P2P_KEEP_ALIVE_INTERVAL_SEC = 15
ui_websocket_clients: Set[websockets.WebSocketServerProtocol] = set()

# Benchmark related globals
benchmark_sessions: Dict[str, Dict] = {}
BENCHMARK_CHUNK_SIZE = 1024

# STUN retry configuration
STUN_MAX_RETRIES = int(os.environ.get("STUN_MAX_RETRIES", "3"))
STUN_RETRY_DELAY_SEC = float(os.environ.get("STUN_RETRY_DELAY_SEC", "2.0"))

# Certificate storage
CERT_DIR = Path("certs")
CERT_FILE = CERT_DIR / "cert.pem"
KEY_FILE = CERT_DIR / "key.pem"

def generate_self_signed_cert():
    """Generate a self-signed certificate for QUIC"""
    CERT_DIR.mkdir(exist_ok=True)
    
    # Generate private key
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )
    
    # Generate certificate
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "State"),
        x509.NameAttribute(NameOID.LOCALITY_NAME, "City"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "P2P Worker"),
        x509.NameAttribute(NameOID.COMMON_NAME, f"worker-{worker_id[:8]}"),
    ])
    
    cert = x509.CertificateBuilder().subject_name(
        subject
    ).issuer_name(
        issuer
    ).public_key(
        private_key.public_key()
    ).serial_number(
        x509.random_serial_number()
    ).not_valid_before(
        datetime.now(timezone.utc)
    ).not_valid_after(
        datetime.now(timezone.utc) + timedelta(days=365)
    ).add_extension(
        x509.SubjectAlternativeName([
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            x509.IPAddress(ipaddress.ip_address("0.0.0.0")),
        ]),
        critical=False,
    ).sign(private_key, hashes.SHA256())
    
    # Write private key
    with open(KEY_FILE, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ))
    
    # Write certificate
    with open(CERT_FILE, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    
    print(f"Worker '{worker_id}': Generated self-signed certificate")

def create_quic_configuration(is_client=True):
    """Create QUIC configuration for client or server"""
    configuration = QuicConfiguration(
        is_client=is_client,
    )
    
    if not is_client:
        # Server configuration
        if not CERT_FILE.exists() or not KEY_FILE.exists():
            generate_self_signed_cert()
        configuration.load_cert_chain(str(CERT_FILE), str(KEY_FILE))
    else:
        # Client configuration - accept self-signed certificates
        configuration.verify_mode = ssl.CERT_NONE
    
    # Enable datagram support for unreliable messages
    configuration.max_datagram_frame_size = 65536
    
    # Set idle timeout
    configuration.idle_timeout = 60.0
    
    return configuration

class P2PQuicHandler:
    """Handler for P2P QUIC connection events"""
    
    def __init__(self, connection: QuicConnection, peer_addr: Tuple[str, int], 
                 peer_id: Optional[str], is_client: bool):
        self.connection = connection
        self.peer_addr = peer_addr
        self.peer_id = peer_id
        self.is_client = is_client
        self.worker_id = worker_id
        self.streams = {}
        print(f"Worker '{self.worker_id}': P2PQuicHandler created for {peer_addr} (is_client={is_client})")
        
    def handle_event(self, event: QuicEvent):
        """Handle QUIC events"""
        if isinstance(event, StreamDataReceived):
            self.handle_stream_data(event)
        elif isinstance(event, DatagramFrameReceived):
            self.handle_datagram(event.data)
        elif isinstance(event, ConnectionTerminated):
            print(f"Worker '{self.worker_id}': QUIC connection to {self.peer_addr} terminated")
            
    def handle_stream_data(self, event: StreamDataReceived):
        """Handle data received on a QUIC stream (reliable)"""
        stream_id = event.stream_id
        data = event.data
        
        print(f"Worker '{self.worker_id}': Received stream data on stream {stream_id}, length={len(data)}, end_stream={event.end_stream}")
        
        # Accumulate data for the stream
        if stream_id not in self.streams:
            self.streams[stream_id] = bytearray()
        self.streams[stream_id].extend(data)
        
        # If stream is complete, process the message
        if event.end_stream:
            complete_data = bytes(self.streams[stream_id])
            del self.streams[stream_id]
            self.process_p2p_message(complete_data, reliable=True)
            
    def handle_datagram(self, data: bytes):
        """Handle QUIC datagram (unreliable but fast)"""
        self.process_p2p_message(data, reliable=False)
        
    def process_p2p_message(self, data: bytes, reliable: bool):
        """Process P2P messages"""
        global benchmark_sessions, ui_websocket_clients
        
        message_str = data.decode(errors='ignore')
        try:
            p2p_message = json.loads(message_str)
            msg_type = p2p_message.get("type")
            from_id = p2p_message.get("from_worker_id")
            
            if msg_type == "chat_message":
                content = p2p_message.get("content")
                print(f"Worker '{self.worker_id}': Received P2P chat from '{from_id}': '{content}' (reliable={reliable})")
                for ui_client_ws in list(ui_websocket_clients):
                    asyncio.create_task(ui_client_ws.send(json.dumps({
                        "type": "p2p_message_received",
                        "from_peer_id": from_id,
                        "content": content,
                        "reliable": reliable
                    })))
                    
            elif msg_type == "p2p_test_data":
                test_data_content = p2p_message.get("data")
                print(f"Worker '{self.worker_id}': +++ P2P_TEST_DATA RECEIVED from '{from_id}': '{test_data_content}' +++")
                
            elif msg_type == "benchmark_chunk":
                peer_addr_str = f"{from_id}"
                session = benchmark_sessions.setdefault(peer_addr_str, {
                    "received_bytes": 0,
                    "received_chunks": 0,
                    "start_time": time.monotonic(),
                    "total_chunks": -1,
                    "from_worker_id": from_id
                })
                
                session["received_bytes"] += len(data)
                session["received_chunks"] += 1
                if session["received_chunks"] % 100 == 0:
                    log_from_id = session.get('from_worker_id', 'unknown_peer')
                    print(f"Worker '{self.worker_id}': Benchmark data from {log_from_id}: {session['received_chunks']} chunks, {session['received_bytes']/1024:.2f} KB")
                    
            elif msg_type == "benchmark_end":
                total_chunks_sent = p2p_message.get("total_chunks", 0)
                peer_addr_str = f"{from_id}"
                if peer_addr_str in benchmark_sessions:
                    session = benchmark_sessions[peer_addr_str]
                    session["total_chunks"] = total_chunks_sent
                    duration = time.monotonic() - session["start_time"]
                    throughput_kbps = (session["received_bytes"] / 1024) / duration if duration > 0 else 0
                    log_from_id = session.get('from_worker_id', 'unknown_peer')
                    status_msg = f"Benchmark Receive from {log_from_id} Complete: Received {session['received_chunks']}/{total_chunks_sent} chunks ({session['received_bytes']/1024:.2f} KB) in {duration:.2f}s. Throughput: {throughput_kbps:.2f} KB/s"
                    print(f"Worker '{self.worker_id}': {status_msg}")
                    for ui_client_ws in list(ui_websocket_clients):
                        asyncio.create_task(ui_client_ws.send(json.dumps({"type": "benchmark_status", "message": status_msg})))
                    del benchmark_sessions[peer_addr_str]
                    
            elif msg_type == "p2p_keep_alive":
                print(f"Worker '{self.worker_id}': Received P2P keep-alive from '{from_id}'")
                
            elif msg_type == "p2p_pairing_test":
                timestamp = p2p_message.get("timestamp")
                print(f"Worker '{self.worker_id}': Received p2p_pairing_test from '{from_id}' (timestamp: {timestamp}). Sending echo.")
                echo_message = {
                    "type": "p2p_pairing_echo",
                    "from_worker_id": self.worker_id,
                    "original_timestamp": timestamp
                }
                asyncio.create_task(self.send_message(echo_message, reliable=False))
                
            elif msg_type == "p2p_pairing_echo":
                original_timestamp = p2p_message.get("original_timestamp")
                rtt = (time.time() - original_timestamp) * 1000 if original_timestamp else -1
                print(f"Worker '{self.worker_id}': Received p2p_pairing_echo from '{from_id}'. RTT: {rtt:.2f} ms")
                for ui_client_ws in list(ui_websocket_clients):
                    asyncio.create_task(ui_client_ws.send(json.dumps({
                        "type": "p2p_status_update",
                        "message": f"Pairing test with {from_id[:8]} successful! RTT: {rtt:.2f}ms"
                    })))
                    
        except json.JSONDecodeError:
            print(f"Worker '{self.worker_id}': Received non-JSON QUIC packet: {message_str[:100]}")
            
    async def send_message(self, message_dict: dict, reliable: bool = True):
        """Send a P2P message either reliably (stream) or unreliably (datagram)"""
        data = json.dumps(message_dict).encode()
        
        if reliable:
            # Use QUIC stream for reliable delivery
            stream_id = self.connection.get_next_available_stream_id()
            self.connection.send_stream_data(stream_id, data, end_stream=True)
            print(f"Worker '{self.worker_id}': Sent reliable message on stream {stream_id}")
        else:
            # Use QUIC datagram for fast unreliable delivery
            self.connection.send_datagram_frame(data)
            print(f"Worker '{self.worker_id}': Sent unreliable datagram")

class QuicServerDispatcher(asyncio.DatagramProtocol):
    """Dispatcher that handles both UDP hole-punching and QUIC connections"""
    
    def __init__(self, worker_id_val: str):
        self.worker_id = worker_id_val
        self.transport: Optional[asyncio.DatagramTransport] = None
        self.quic_connections: Dict[Tuple[str, int], QuicConnection] = {}
        self.handlers: Dict[Tuple[str, int], P2PQuicHandler] = {}
        self.timers: Dict[Tuple[str, int], asyncio.TimerHandle] = {}
        print(f"Worker '{self.worker_id}': QuicServerDispatcher created")
        
    def connection_made(self, transport: asyncio.DatagramTransport):
        global quic_dispatcher
        self.transport = transport
        quic_dispatcher = self
        local_addr = transport.get_extra_info('sockname')
        print(f"Worker '{self.worker_id}': UDP listener active on {local_addr} (Internal Port: {INTERNAL_UDP_PORT})")
        
    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        """Handle incoming UDP datagrams"""
        global current_p2p_peer_addr, current_p2p_peer_id
        
        # Check if it's a hole-punch ping (non-QUIC)
        message_str = data.decode(errors='ignore')
        if "P2P_PING_FROM_" in message_str or "P2P_HOLE_PUNCH_PING_FROM_" in message_str:
            print(f"Worker '{self.worker_id}': !!! P2P UDP Ping received from {addr}: {message_str} !!!")
            return
            
        # Try to handle as QUIC packet
        now = time.time()
        
        if addr not in self.quic_connections:
            # This might be a new incoming QUIC connection
            print(f"Worker '{self.worker_id}': New QUIC packet from {addr}, creating server connection")
            config = create_quic_configuration(is_client=False)
            # Parse the first byte to get the destination connection ID length
            # For initial packets, we need to extract the DCID from the packet
            dcid_length = 8  # Default QUIC connection ID length
            if len(data) > dcid_length + 5:
                # Extract destination connection ID from the packet (simplified)
                dcid = data[6:6+dcid_length]  # Skip header flags and version
            else:
                # Fallback: generate a random connection ID
                dcid = os.urandom(dcid_length)
            
            connection = QuicConnection(
                configuration=config,
                original_destination_connection_id=dcid
            )
            self.quic_connections[addr] = connection
            self.handlers[addr] = P2PQuicHandler(connection, addr, None, is_client=False)
            
        connection = self.quic_connections[addr]
        connection.receive_datagram(data, addr, now)
        
        # Process QUIC events
        self._process_quic_events(connection, addr)
        
        # Send any pending datagrams
        self._send_pending_datagrams(connection, addr, now)
        
        # Update timer
        self._update_timer(connection, addr)
        
    def initiate_quic_connection(self, peer_addr: Tuple[str, int], peer_id: str):
        """Initiate a QUIC connection to a peer"""
        global current_p2p_peer_id, current_p2p_peer_addr
        
        print(f"Worker '{self.worker_id}': Initiating QUIC connection to {peer_id} at {peer_addr}")
        
        # Update globals
        current_p2p_peer_id = peer_id
        current_p2p_peer_addr = peer_addr
        
        # Create client QUIC connection
        config = create_quic_configuration(is_client=True)
        connection = QuicConnection(configuration=config)
        self.quic_connections[peer_addr] = connection
        self.handlers[peer_addr] = P2PQuicHandler(connection, peer_addr, peer_id, is_client=True)
        
        # Start handshake
        now = time.time()
        connection.connect(peer_addr, now)
        
        # Send initial packets
        self._send_pending_datagrams(connection, peer_addr, now)
        
        # Set up timer
        self._update_timer(connection, peer_addr)
        
    def _process_quic_events(self, connection: QuicConnection, addr: Tuple[str, int]):
        """Process QUIC events"""
        handler = self.handlers.get(addr)
        if not handler:
            return
            
        while True:
            event = connection.next_event()
            if event is None:
                break
            # Debug logging
            print(f"Worker '{self.worker_id}': Processing QUIC event {type(event).__name__} from {addr}")
            handler.handle_event(event)
            
    def _send_pending_datagrams(self, connection: QuicConnection, addr: Tuple[str, int], now: float):
        """Send any pending QUIC datagrams"""
        if not self.transport:
            return
            
        for data, _ in connection.datagrams_to_send(now):
            self.transport.sendto(data, addr)
            
    def _update_timer(self, connection: QuicConnection, addr: Tuple[str, int]):
        """Update QUIC timer for this connection"""
        # Cancel existing timer
        if addr in self.timers:
            self.timers[addr].cancel()
            
        # Get next timer
        timer = connection.get_timer()
        if timer is not None:
            loop = asyncio.get_event_loop()
            handle = loop.call_at(timer, self._handle_timer, connection, addr)
            self.timers[addr] = handle
            
    def _handle_timer(self, connection: QuicConnection, addr: Tuple[str, int]):
        """Handle QUIC timer expiry"""
        now = time.time()
        connection.handle_timer(now)
        
        # Send any resulting datagrams
        self._send_pending_datagrams(connection, addr, now)
        
        # Update timer
        self._update_timer(connection, addr)
        
    def send_hole_punch_ping(self, peer_addr: Tuple[str, int], ping_num: int):
        """Send UDP hole punch ping"""
        if self.transport:
            message = f"P2P_HOLE_PUNCH_PING_FROM_{self.worker_id}_NUM_{ping_num}"
            self.transport.sendto(message.encode(), peer_addr)
            print(f"Worker '{self.worker_id}': Sent UDP Hole Punch PING {ping_num} to {peer_addr}")
            
    def get_handler(self, addr: Tuple[str, int]) -> Optional[P2PQuicHandler]:
        """Get the handler for a specific peer address"""
        return self.handlers.get(addr)
        
    def error_received(self, exc: Exception):
        print(f"Worker '{self.worker_id}': UDP listener error: {exc}")
        
    def connection_lost(self, exc: Optional[Exception]):
        global quic_dispatcher
        print(f"Worker '{self.worker_id}': UDP listener connection lost: {exc if exc else 'Closed normally'}")
        if self == quic_dispatcher:
            quic_dispatcher = None

def handle_shutdown_signal(signum, frame):
    global stop_signal_received, quic_dispatcher
    print(f"Shutdown signal ({signum}) received. Worker '{worker_id}' attempting graceful shutdown.")
    stop_signal_received = True
    
    # Close QUIC connections
    if quic_dispatcher:
        for addr, connection in quic_dispatcher.quic_connections.items():
            try:
                connection.close()
                print(f"Worker '{worker_id}': Closed QUIC connection to {addr}")
            except Exception as e:
                print(f"Worker '{worker_id}': Error closing QUIC connection: {e}")
                
    # Close UI websocket clients
    for ws_client in list(ui_websocket_clients):
        asyncio.create_task(ws_client.close(reason="Server shutting down"))

async def process_http_request(path: str, request_headers: Headers) -> Optional[Tuple[int, Headers, bytes]]:
    if path == "/ui_ws":
        return None
    if path == "/":
        try:
            html_path = Path(__file__).parent / "index.html"
            with open(html_path, "rb") as f:
                content = f.read()
            headers = Headers([("Content-Type", "text/html"), ("Content-Length", str(len(content)))])
            return (200, headers, content)
        except FileNotFoundError:
            return (404, Headers([("Content-Type", "text/plain")]), b"index.html not found")
        except Exception as e:
            print(f"Error serving index.html: {e}")
            return (500, Headers([("Content-Type", "text/plain")]), b"Internal Server Error")
    elif path == "/health":
        return (200, Headers([("Content-Type", "text/plain")]), b"OK")
    else:
        return (404, Headers([("Content-Type", "text/plain")]), b"Not Found")

async def benchmark_send_quic_data(target_ip: str, target_port: int, size_kb: int, ui_ws: websockets.WebSocketServerProtocol):
    global worker_id, quic_dispatcher, current_p2p_peer_addr
    
    if not (quic_dispatcher and current_p2p_peer_addr):
        err_msg = "No active QUIC connection for benchmark."
        print(f"Worker '{worker_id}': {err_msg}")
        await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Error: {err_msg}"}))
        return
        
    handler = quic_dispatcher.get_handler(current_p2p_peer_addr)
    if not handler:
        err_msg = "No QUIC handler for current peer."
        print(f"Worker '{worker_id}': {err_msg}")
        await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Error: {err_msg}"}))
        return
        
    print(f"Worker '{worker_id}': Starting QUIC Benchmark: Sending {size_kb}KB to {target_ip}:{target_port}")
    await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Benchmark Send: Starting to send {size_kb}KB..."}))
    
    num_chunks = size_kb
    dummy_chunk_content = b'B' * (BENCHMARK_CHUNK_SIZE - 50)
    dummy_chunk_b64 = base64.b64encode(dummy_chunk_content).decode('ascii')
    
    start_time = time.monotonic()
    bytes_sent = 0
    update_interval = max(1, num_chunks // 10)
    
    try:
        for i in range(num_chunks):
            if stop_signal_received or ui_ws.closed:
                print(f"Worker '{worker_id}': Benchmark send cancelled")
                await ui_ws.send(json.dumps({"type": "benchmark_status", "message": "Benchmark send cancelled."}))
                break
                
            payload = {
                "type": "benchmark_chunk",
                "seq": i,
                "payload": dummy_chunk_b64,
                "from_worker_id": worker_id
            }
            
            # Send as reliable stream data for benchmarking
            await handler.send_message(payload, reliable=True)
            
            # Trigger sending of QUIC packets
            now = time.time()
            quic_dispatcher._send_pending_datagrams(handler.connection, current_p2p_peer_addr, now)
            
            bytes_sent += len(json.dumps(payload).encode())
            
            if (i + 1) % update_interval == 0:
                progress_msg = f"Benchmark Send: Sent {i+1}/{num_chunks} chunks ({bytes_sent / 1024:.2f} KB)..."
                print(f"Worker '{worker_id}': {progress_msg}")
                await ui_ws.send(json.dumps({"type": "benchmark_status", "message": progress_msg}))
                
        else:  # Loop completed
            # Send benchmark end marker
            end_payload = {"type": "benchmark_end", "total_chunks": num_chunks, "from_worker_id": worker_id}
            await handler.send_message(end_payload, reliable=True)
            
            # Ensure it's sent
            now = time.time()
            quic_dispatcher._send_pending_datagrams(handler.connection, current_p2p_peer_addr, now)
            
            print(f"Worker '{worker_id}': Sent benchmark_end marker")
            
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
    global ui_websocket_clients, worker_id, current_p2p_peer_id, quic_dispatcher, current_p2p_peer_addr
    ui_websocket_clients.add(websocket)
    print(f"Worker '{worker_id}': UI WebSocket client connected from {websocket.remote_address}")
    
    try:
        await websocket.send(json.dumps({
            "type": "init_info",
            "worker_id": worker_id,
            "p2p_peer_id": current_p2p_peer_id,
            "protocol": "QUIC"
        }))
        
        async for message_raw in websocket:
            print(f"Worker '{worker_id}': Message from UI WebSocket: {message_raw}")
            try:
                message = json.loads(message_raw)
                msg_type = message.get("type")
                
                if msg_type == "send_p2p_message":
                    content = message.get("content")
                    reliable = message.get("reliable", True)  # Default to reliable
                    
                    if not (quic_dispatcher and current_p2p_peer_addr):
                        await websocket.send(json.dumps({"type": "error", "message": "Not connected to a P2P peer."}))
                    elif not content:
                        await websocket.send(json.dumps({"type": "error", "message": "Cannot send empty message."}))
                    else:
                        handler = quic_dispatcher.get_handler(current_p2p_peer_addr)
                        if handler:
                            print(f"Worker '{worker_id}': Sending P2P message '{content}' to peer {current_p2p_peer_id} (reliable={reliable})")
                            p2p_message = {
                                "type": "chat_message",
                                "from_worker_id": worker_id,
                                "content": content
                            }
                            await handler.send_message(p2p_message, reliable=reliable)
                            
                            # Trigger sending
                            now = time.time()
                            quic_dispatcher._send_pending_datagrams(handler.connection, current_p2p_peer_addr, now)
                        else:
                            await websocket.send(json.dumps({"type": "error", "message": "QUIC handler not available."}))
                        
                elif msg_type == "ui_client_hello":
                    print(f"Worker '{worker_id}': UI Client says hello.")
                    if current_p2p_peer_id:
                        await websocket.send(json.dumps({
                            "type": "p2p_status_update",
                            "message": f"QUIC P2P link active with {current_p2p_peer_id[:8]}...",
                            "peer_id": current_p2p_peer_id
                        }))
                        
                elif msg_type == "start_benchmark_send":
                    size_kb = message.get("size_kb", 1024)
                    if current_p2p_peer_addr:
                        print(f"Worker '{worker_id}': UI requested benchmark send of {size_kb}KB to {current_p2p_peer_id}")
                        asyncio.create_task(benchmark_send_quic_data(
                            current_p2p_peer_addr[0], 
                            current_p2p_peer_addr[1], 
                            size_kb, 
                            websocket
                        ))
                    else:
                        await websocket.send(json.dumps({"type": "benchmark_status", "message": "Error: No P2P peer to start benchmark with."}))
                        
            except json.JSONDecodeError:
                print(f"Worker '{worker_id}': UI WebSocket received non-JSON: {message_raw}")
            except Exception as e:
                print(f"Worker '{worker_id}': Error processing UI WebSocket message: {e}")
                
    except websockets.exceptions.ConnectionClosed:
        print(f"Worker '{worker_id}': UI WebSocket client {websocket.remote_address} disconnected.")
    except Exception as e:
        print(f"Worker '{worker_id}': Error with UI WebSocket connection: {e}")
    finally:
        ui_websocket_clients.remove(websocket)
        print(f"Worker '{worker_id}': UI WebSocket client removed.")

async def discover_and_report_stun_udp_endpoint(websocket_conn_to_rendezvous):
    """Discover public UDP endpoint using STUN"""
    global our_stun_discovered_udp_ip, our_stun_discovered_udp_port, worker_id, INTERNAL_UDP_PORT
    
    stun_host = os.environ.get("STUN_HOST", DEFAULT_STUN_HOST)
    stun_port = int(os.environ.get("STUN_PORT", DEFAULT_STUN_PORT))
    
    for attempt in range(1, STUN_MAX_RETRIES + 1):
        print(f"Worker '{worker_id}': STUN discovery attempt {attempt}/{STUN_MAX_RETRIES} via {stun_host}:{stun_port}")
        try:
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
                await websocket_conn_to_rendezvous.send(json.dumps({
                    "type": "update_udp_endpoint",
                    "udp_ip": external_ip,
                    "udp_port": external_port
                }))
                print(f"Worker '{worker_id}': Sent STUN UDP endpoint to Rendezvous.")
                return True
                
        except socket.gaierror as e:
            print(f"Worker '{worker_id}': STUN attempt {attempt} failed: DNS resolution error for '{stun_host}': {e}")
        except stun.StunException as e:
            print(f"Worker '{worker_id}': STUN attempt {attempt} failed: STUN protocol error: {type(e).__name__} - {e}")
        except OSError as e:
            print(f"Worker '{worker_id}': STUN attempt {attempt} failed: OS error: {type(e).__name__} - {e}")
        except Exception as e:
            print(f"Worker '{worker_id}': STUN attempt {attempt} failed: {type(e).__name__} - {e}")
            
        if attempt < STUN_MAX_RETRIES:
            delay = STUN_RETRY_DELAY_SEC * (2 ** (attempt - 1))
            print(f"Worker '{worker_id}': Retrying STUN in {delay:.1f} seconds...")
            await asyncio.sleep(delay)
            
    print(f"Worker '{worker_id}': STUN discovery failed after {STUN_MAX_RETRIES} attempts.")
    return False

async def start_udp_hole_punch(peer_udp_ip: str, peer_udp_port: int, peer_worker_id: str):
    """Start UDP hole punching like in main.py"""
    global worker_id, stop_signal_received, quic_dispatcher, current_p2p_peer_addr, current_p2p_peer_id
    
    if not quic_dispatcher:
        print(f"Worker '{worker_id}': UDP transport not ready for hole punch to '{peer_worker_id}'.")
        return
        
    # Log the (potentially new) P2P target
    print(f"Worker '{worker_id}': Initiating P2P connection. Previous peer ID: '{current_p2p_peer_id}', Previous peer addr: {current_p2p_peer_addr}.")
    current_p2p_peer_id = peer_worker_id
    current_p2p_peer_addr = (peer_udp_ip, peer_udp_port)
    print(f"Worker '{worker_id}': Set new P2P target. Current peer ID: '{current_p2p_peer_id}', Current peer addr: {current_p2p_peer_addr}.")
    
    print(f"Worker '{worker_id}': Starting UDP hole punch PINGs towards '{peer_worker_id}' at {current_p2p_peer_addr}")
    
    # Send hole punch pings exactly like in main.py
    for i in range(1, 4):  # Send a few pings
        if stop_signal_received:
            break
        try:
            quic_dispatcher.send_hole_punch_ping(current_p2p_peer_addr, i)
        except Exception as e:
            print(f"Worker '{worker_id}': Error sending UDP Hole Punch PING {i}: {e}")
        await asyncio.sleep(0.5)
        
    print(f"Worker '{worker_id}': Finished UDP Hole Punch PING burst to '{peer_worker_id}'.")
    
    # Notify UI
    for ui_client_ws in list(ui_websocket_clients):
        asyncio.create_task(ui_client_ws.send(json.dumps({
            "type": "p2p_status_update",
            "message": f"P2P link attempt initiated with {peer_worker_id[:8]}...",
            "peer_id": peer_worker_id
        })))
    
    # After hole punching, initiate QUIC connection
    await asyncio.sleep(0.5)  # Small delay to ensure hole punch completes
    
    if quic_dispatcher:
        quic_dispatcher.initiate_quic_connection(current_p2p_peer_addr, peer_worker_id)
        
        # Send pairing test if we're the initiator (like in main.py)
        if worker_id < peer_worker_id:
            print(f"Worker '{worker_id}': Designated as initiator for pairing test with '{peer_worker_id}'. Sending test message.")
            handler = quic_dispatcher.get_handler(current_p2p_peer_addr)
            if handler:
                pairing_test_message = {
                    "type": "p2p_pairing_test",
                    "from_worker_id": worker_id,
                    "timestamp": time.time()
                }
                await handler.send_message(pairing_test_message, reliable=False)
                
                # Trigger sending
                now = time.time()
                quic_dispatcher._send_pending_datagrams(handler.connection, current_p2p_peer_addr, now)
        else:
            print(f"Worker '{worker_id}': Designated as responder for pairing test with '{peer_worker_id}'. Awaiting test message.")

async def attempt_hole_punch_when_ready(peer_udp_ip: str, peer_udp_port: int, peer_worker_id: str, 
                                       max_wait_sec: float = 10.0, check_interval: float = 0.5):
    """Wait for UDP transport to be ready then start hole punch"""
    global quic_dispatcher, stop_signal_received, worker_id
    
    waited = 0.0
    while not stop_signal_received and waited < max_wait_sec:
        if quic_dispatcher:  # Listener is finally ready
            await start_udp_hole_punch(peer_udp_ip, peer_udp_port, peer_worker_id)
            return
        await asyncio.sleep(check_interval)
        waited += check_interval
        
    print(f"Worker '{worker_id}': Gave up waiting ({waited:.1f}s) for UDP listener before hole-punch to '{peer_worker_id}'.")

async def send_periodic_p2p_keep_alives():
    """Send periodic keep-alive messages"""
    global worker_id, stop_signal_received, quic_dispatcher, current_p2p_peer_addr
    
    print(f"Worker '{worker_id}': P2P Keep-Alive sender task started.")
    
    while not stop_signal_received:
        await asyncio.sleep(P2P_KEEP_ALIVE_INTERVAL_SEC)
        
        if quic_dispatcher and current_p2p_peer_addr:
            handler = quic_dispatcher.get_handler(current_p2p_peer_addr)
            if handler:
                try:
                    keep_alive_message = {
                        "type": "p2p_keep_alive",
                        "from_worker_id": worker_id
                    }
                    await handler.send_message(keep_alive_message, reliable=False)
                    
                    # Trigger sending
                    now = time.time()
                    quic_dispatcher._send_pending_datagrams(handler.connection, current_p2p_peer_addr, now)
                except Exception as e:
                    print(f"Worker '{worker_id}': Error sending P2P keep-alive: {e}")
                    
    print(f"Worker '{worker_id}': P2P Keep-Alive sender task stopped.")

async def connect_to_rendezvous(rendezvous_ws_url: str):
    """Connect to rendezvous service and handle P2P coordination"""
    global stop_signal_received, worker_id, ui_websocket_clients, quic_dispatcher, INTERNAL_UDP_PORT
    
    ip_echo_service_url = "https://api.ipify.org"
    ping_interval = float(os.environ.get("PING_INTERVAL_SEC", "25"))
    ping_timeout = float(os.environ.get("PING_TIMEOUT_SEC", "25"))
    udp_listener_active = False
    loop = asyncio.get_running_loop()
    
    while not stop_signal_received:
        try:
            async with websockets.connect(
                rendezvous_ws_url,
                ping_interval=ping_interval,
                ping_timeout=ping_timeout,
                proxy=None
            ) as ws_to_rendezvous:
                print(f"Worker '{worker_id}' connected to Rendezvous Service.")
                
                # Send HTTP-based public IP
                try:
                    response = requests.get(ip_echo_service_url, timeout=10)
                    response.raise_for_status()
                    http_public_ip = response.text.strip()
                    await ws_to_rendezvous.send(json.dumps({
                        "type": "register_public_ip",
                        "ip": http_public_ip
                    }))
                    print(f"Worker '{worker_id}' sent HTTP-based IP ({http_public_ip}) to Rendezvous.")
                except Exception as e:
                    print(f"Worker '{worker_id}': Error sending HTTP IP: {e}")
                
                # STUN discovery
                stun_success = await discover_and_report_stun_udp_endpoint(ws_to_rendezvous)
                
                # Start UDP listener with QUIC dispatcher
                if stun_success and not udp_listener_active:
                    try:
                        _transport, _protocol = await loop.create_datagram_endpoint(
                            lambda: QuicServerDispatcher(worker_id),
                            local_addr=('0.0.0.0', INTERNAL_UDP_PORT)
                        )
                        await asyncio.sleep(0.1)
                        if quic_dispatcher:
                            print(f"Worker '{worker_id}': QUIC/UDP dispatcher active on 0.0.0.0:{INTERNAL_UDP_PORT}")
                        else:
                            print(f"Worker '{worker_id}': Failed to create QUIC dispatcher")
                        udp_listener_active = True
                    except Exception as e:
                        print(f"Worker '{worker_id}': Failed to create UDP endpoint: {e}")
                
                # Message handling loop
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
                                # Schedule hole punch attempt
                                asyncio.create_task(attempt_hole_punch_when_ready(peer_ip, int(peer_port), peer_id))
                                
                        elif msg_type == "udp_endpoint_ack":
                            print(f"Worker '{worker_id}': UDP Endpoint Ack: {message_data.get('status')}")
                            
                        elif msg_type == "echo_response":
                            print(f"Worker '{worker_id}': Echo Response: {message_data.get('processed_by_rendezvous')}")
                            
                        elif msg_type == "admin_chat_message":
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
                                
                                # Auto-reply
                                await ws_to_rendezvous.send(json.dumps({
                                    "type": "chat_response",
                                    "admin_session_id": admin_session_id,
                                    "content": f"Worker {worker_id[:8]} received: {content}"
                                }))
                        else:
                            print(f"Worker '{worker_id}': Unhandled message from Rendezvous: {msg_type}")
                            
                    except asyncio.TimeoutError:
                        pass  # Expected timeout for periodic checks
                    except websockets.exceptions.ConnectionClosed as e:
                        print(f"Worker '{worker_id}': Rendezvous WS closed: {e}")
                        break
                    except Exception as e:
                        print(f"Worker '{worker_id}': Error in WS recv loop: {e}")
                        break
                        
        except Exception as e:
            print(f"Worker '{worker_id}': Error in WS connection: {type(e).__name__} - {e}")
            
        finally:
            # Clean up UDP listener if needed
            if quic_dispatcher and quic_dispatcher.transport:
                quic_dispatcher.transport.close()
                udp_listener_active = False
                
        if not stop_signal_received:
            await asyncio.sleep(10)

async def main_async_orchestrator():
    """Main orchestrator for all async tasks"""
    global worker_id, HTTP_PORT_FOR_UI
    
    # Start HTTP/WebSocket server for UI
    main_server = await websockets_serve(
        ui_websocket_handler, "0.0.0.0", HTTP_PORT_FOR_UI,
        process_request=process_http_request,
        ping_interval=20, ping_timeout=20
    )
    print(f"Worker '{worker_id}': HTTP & UI WebSocket server listening on 0.0.0.0:{HTTP_PORT_FOR_UI}")
    print(f"  - Serving index.html at '/'")
    print(f"  - UI WebSocket at '/ui_ws'")
    print(f"  - Health check at '/health'")
    print(f"  - Using QUIC protocol over manual UDP hole-punching")
    
    # Get rendezvous URL
    rendezvous_base_url_env = os.environ.get("RENDEZVOUS_SERVICE_URL")
    if not rendezvous_base_url_env:
        print("CRITICAL: RENDEZVOUS_SERVICE_URL missing")
        return
        
    ws_scheme = "wss" if rendezvous_base_url_env.startswith("https://") else "ws"
    base_url_no_scheme = rendezvous_base_url_env.replace("https://", "").replace("http://", "")
    full_rendezvous_ws_url = f"{ws_scheme}://{base_url_no_scheme}/ws/register/{worker_id}"
    
    # Start tasks
    rendezvous_client_task = asyncio.create_task(connect_to_rendezvous(full_rendezvous_ws_url))
    p2p_keep_alive_task = asyncio.create_task(send_periodic_p2p_keep_alives())
    
    try:
        await asyncio.gather(rendezvous_client_task, p2p_keep_alive_task)
    except asyncio.CancelledError:
        print(f"Worker '{worker_id}': Main orchestrator tasks were cancelled.")
    finally:
        main_server.close()
        await main_server.wait_closed()
        print(f"Worker '{worker_id}': Main HTTP/UI WebSocket server stopped.")
        
        # Clean up QUIC dispatcher
        if quic_dispatcher and quic_dispatcher.transport:
            try:
                quic_dispatcher.transport.close()
                print(f"Worker '{worker_id}': QUIC/UDP transport closed.")
            except Exception as e:
                print(f"Worker '{worker_id}': Error closing transport: {e}")
                
        # Cancel tasks
        for task in [rendezvous_client_task, p2p_keep_alive_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

if __name__ == "__main__":
    print(f"WORKER SCRIPT (ID: {worker_id}): Initializing with QUIC over manual UDP hole-punching...")
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
    except Exception as e:
        print(f"Worker '{worker_id}' CRITICAL ERROR in __main__: {type(e).__name__} - {e}")
    finally:
        print(f"Worker '{worker_id}' main EXIT.")