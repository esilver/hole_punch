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
from aioquic.quic.connection import QuicConnection, QuicConnectionState
from aioquic.quic.events import QuicEvent, StreamDataReceived, ConnectionIdIssued, DatagramFrameReceived, ConnectionTerminated
from aioquic.tls import SessionTicket
from aioquic.asyncio import QuicConnectionProtocol

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
# (Removed benchmark_sessions - no longer needed with streaming approach)

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
    
    # Increase flow control windows for better performance with many small messages or large transfers
    configuration.initial_max_stream_data_bidi_local = 8 * 1024 * 1024  # 8 MB per stream (local initiator)
    configuration.initial_max_stream_data_bidi_remote = 8 * 1024 * 1024 # 8 MB per stream (remote initiator)
    configuration.initial_max_stream_data_uni = 8 * 1024 * 1024       # 8 MB for unidirectional streams
    configuration.initial_max_data = 32 * 1024 * 1024                 # 32 MB for the entire connection
    
    return configuration

# P2P Application Stream Handler (Async)
async def _async_handle_app_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    source_peer_id: Optional[str], # Peer ID if known
    source_peer_addr: Tuple[str, int],
    is_client_role: bool  # Passed from the callback
):
    """Handle incoming P2P streams from peers (async)"""
    global ui_websocket_clients, worker_id
    local_worker_id = worker_id # Capture from global scope
    
    # Get stream ID and check if it's client-initiated
    stream_id = writer.get_extra_info('stream_id')
    
    # In QUIC, stream IDs indicate who initiated:
    # Client-initiated: 0, 4, 8, 12... (bidirectional) or 2, 6, 10... (unidirectional)
    # Server-initiated: 1, 5, 9, 13... (bidirectional) or 3, 7, 11... (unidirectional)
    
    is_client_initiated = (stream_id % 4) in [0, 2]
    
    # Use the passed client role status
    we_are_client = is_client_role
    
    # Debug logging
    print(f"Worker '{local_worker_id}': Stream handler called - Stream ID: {stream_id}, Client-initiated: {is_client_initiated}, We are client: {we_are_client}")
    
    # If we're the client and this is a client-initiated stream, it's our own stream
    # This happens when the server sends data back on a stream we initiated
    if we_are_client and is_client_initiated:
        print(f"Worker '{local_worker_id}': Data received on our client-initiated stream {stream_id} from {source_peer_addr}")
        # We might receive data back on our own stream (e.g., server echo)
        # But for now, our protocol doesn't expect responses on chat/benchmark streams
        # So we'll just return without processing
        return
    
    # If we're the server and this is a server-initiated stream, it's our own stream
    if not we_are_client and not is_client_initiated:
        print(f"Worker '{local_worker_id}': Data received on our server-initiated stream {stream_id} from {source_peer_addr}")
        # Similar to above - we don't expect responses on server-initiated streams
        return

    print(f"Worker '{local_worker_id}': New P2P stream accepted from {source_peer_addr} (Peer ID: {source_peer_id}). Stream ID: {stream_id}")

    # Buffer for initial JSON message
    file_info_for_stream = None
    is_benchmark_stream = False

    try:
        # 1. Read the initial JSON header line
        header_line_bytes = await reader.readline()
        if not header_line_bytes:
            print(f"Worker '{local_worker_id}': Stream from {source_peer_addr} closed before header.")
            return

        # Attempt to parse header
        try:
            message = json.loads(header_line_bytes.decode().strip())
            msg_type = message.get("type")
            from_id = message.get("from_worker_id")

            # print(f"Worker '{local_worker_id}': Stream header from {from_id or source_peer_addr}: Type '{msg_type}'")

            if msg_type == "benchmark_start":
                is_benchmark_stream = True
                file_info_for_stream = {
                    "filename": message.get("filename", "unknown_file"),
                    "expected_total_size": message.get("total_size", 0),
                    "received_bytes": 0,
                    "start_time": time.monotonic(),
                    "from_worker_id": from_id
                }
                print(f"Worker '{local_worker_id}': BENCHMARK_START (receive) on stream {writer.get_extra_info('stream_id')} for '{file_info_for_stream['filename']}', expecting {file_info_for_stream['expected_total_size']} bytes.")

            elif msg_type == "chat_message":
                content = message.get("content")
                print(f"Worker '{local_worker_id}': Received P2P chat via stream from '{from_id}': '{content}'")
                for ui_client_ws in list(ui_websocket_clients):
                    asyncio.create_task(ui_client_ws.send(json.dumps({
                        "type": "p2p_message_received", 
                        "from_peer_id": from_id,
                        "content": content, 
                        "reliable": True # Streams are reliable
                    })))

            elif msg_type == "p2p_test_data":
                test_data_content = message.get("data")
                print(f"Worker '{local_worker_id}': +++ P2P_TEST_DATA RECEIVED from '{from_id}': '{test_data_content}' +++")

            elif msg_type == "p2p_pairing_test":
                original_timestamp = message.get("original_timestamp")
                rtt = (time.time() - original_timestamp) * 1000 if original_timestamp else -1
                print(f"Worker '{local_worker_id}': Received p2p_pairing_test from '{from_id}'. RTT: {rtt:.2f} ms")
                for ui_client_ws in list(ui_websocket_clients):
                    asyncio.create_task(ui_client_ws.send(json.dumps({
                        "type": "p2p_status_update",
                        "message": f"Pairing test with {from_id[:8]} successful! RTT: {rtt:.2f}ms"
                    })))

            elif msg_type == "p2p_pairing_echo":
                original_timestamp = message.get("original_timestamp")
                rtt = (time.time() - original_timestamp) * 1000 if original_timestamp else -1
                print(f"Worker '{local_worker_id}': Received p2p_pairing_echo from '{from_id}'. RTT: {rtt:.2f} ms")
                for ui_client_ws in list(ui_websocket_clients):
                    asyncio.create_task(ui_client_ws.send(json.dumps({
                        "type": "p2p_status_update",
                        "message": f"Pairing test with {from_id[:8]} successful! RTT: {rtt:.2f}ms"
                    })))

            elif msg_type == "p2p_keep_alive":
                # print(f"Worker '{local_worker_id}': Received P2P keep-alive from '{from_id}'")
                pass

            else:
                print(f"Worker '{local_worker_id}': Unknown P2P message type '{msg_type}' on stream from {source_peer_addr}")

        except json.JSONDecodeError:
            print(f"Worker '{local_worker_id}': Non-JSON header on stream from {source_peer_addr}: {header_line_bytes[:100]!r}")
            return

        # 2. If it's a benchmark stream, read the file data
        if is_benchmark_stream and file_info_for_stream:
            while True:
                chunk = await reader.read(65536) # Read in chunks
                if not chunk: # EOF
                    break
                file_info_for_stream["received_bytes"] += len(chunk)
                # Add progress logging every ~5MB
                if file_info_for_stream["received_bytes"] % (1024 * 1024 * 5) < len(chunk):
                    elapsed = time.monotonic() - file_info_for_stream["start_time"]
                    speed_mbps = (file_info_for_stream["received_bytes"] / (1024*1024)) / elapsed if elapsed > 0 else 0
                    print(f"Worker '{local_worker_id}': FILE_PROGRESS ('{file_info_for_stream['filename']}'): "
                          f"{file_info_for_stream['received_bytes'] / (1024*1024):.2f} MB at {speed_mbps:.2f} MB/s")

            # Benchmark receive complete
            elapsed = time.monotonic() - file_info_for_stream["start_time"]
            total_mb_received = file_info_for_stream["received_bytes"] / (1024 * 1024)
            expected_total_mb = file_info_for_stream["expected_total_size"] / (1024*1024)
            speed_mbps = total_mb_received / elapsed if elapsed > 0 else 0
            from_peer_id_for_log = file_info_for_stream.get("from_worker_id", "unknown_peer")

            status_msg = (f"Benchmark File Receive Complete: '{file_info_for_stream['filename']}' "
                          f"({total_mb_received:.2f} MB / {expected_total_mb:.2f} MB expected) from {from_peer_id_for_log} "
                          f"in {elapsed:.2f}s. Throughput: {speed_mbps:.2f} MB/s")
            print(f"Worker '{local_worker_id}': FINAL_BENCHMARK_RECEIVE_STATUS: {status_msg}")
            for ui_client_ws in list(ui_websocket_clients):
                asyncio.create_task(ui_client_ws.send(json.dumps({
                    "type": "benchmark_status", "message": status_msg
                })))

    except (ConnectionResetError, asyncio.IncompleteReadError) as e:
        print(f"Worker '{local_worker_id}': Stream from {source_peer_addr} reset or closed unexpectedly: {e}")
    except Exception as e:
        print(f"Worker '{local_worker_id}': Error handling P2P stream from {source_peer_addr}: {type(e).__name__} - {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Don't automatically close the writer here
        # The stream might be bidirectional and still in use
        # Let the protocol handle stream lifecycle
        pass

# P2PQuicHandler class has been removed - functionality moved to _async_handle_app_stream and QuicConnectionProtocol

# Create the synchronous callback for aioquic
def quic_protocol_stream_handler_callback(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
):
    """Synchronous stream handler callback for QuicConnectionProtocol"""
    global quic_dispatcher
    peer_addr = writer.get_extra_info('peername')
    
    peer_id = None
    is_client_role = False  # Default
    
    # Retrieve the QuicConnectionProtocol instance associated with this stream.
    # For aioquic, `writer.transport` is a `QuicStreamAdapter` (see
    # aioquic.asyncio.protocol.QuicStreamAdapter).  It exposes the parent
    # `QuicConnectionProtocol` through its public `protocol` attribute.
    # (Older versions used a protected `_protocol`, so we keep that as a
    #  fallback for compatibility.)
    qcp_instance = getattr(writer.transport, "protocol", None) or getattr(writer.transport, "_protocol", None)

    if qcp_instance and hasattr(qcp_instance, "_quic") and qcp_instance._quic:
        is_client_role = qcp_instance._quic._is_client
    
    if quic_dispatcher and peer_addr:
        peer_id = quic_dispatcher.peer_ids_by_addr.get(peer_addr)

    # Launch the async task, passing the correctly determined client role status
    asyncio.create_task(_async_handle_app_stream(reader, writer, peer_id, peer_addr, is_client_role))

class QuicServerDispatcher(asyncio.DatagramProtocol):
    """Dispatcher that handles both UDP hole-punching and QUIC connections using QuicConnectionProtocol"""
    
    def __init__(self, worker_id_val: str, application_stream_handler_cb):
        self.worker_id = worker_id_val
        self.transport: Optional[asyncio.DatagramTransport] = None
        # Store QuicConnectionProtocol instances per peer address
        self.peer_protocols: Dict[Tuple[str, int], QuicConnectionProtocol] = {}
        # Store peer IDs associated with an address
        self.peer_ids_by_addr: Dict[Tuple[str, int], str] = {}
        # The app callback for new streams
        self.application_stream_handler_callback = application_stream_handler_cb
        # Store datagram handler tasks
        self.datagram_handler_tasks: Dict[Tuple[str, int], asyncio.Task] = {}
        print(f"Worker '{self.worker_id}': QuicServerDispatcher (using QuicConnectionProtocol) created")
        
    def connection_made(self, transport: asyncio.DatagramTransport):
        global quic_dispatcher
        self.transport = transport
        quic_dispatcher = self
        local_addr = transport.get_extra_info('sockname')
        print(f"Worker '{self.worker_id}': UDP listener active on {local_addr} (Internal Port: {INTERNAL_UDP_PORT})")
        
    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        """Handle incoming UDP datagrams and route to QuicConnectionProtocol"""

        # Check for hole-punch pings (non-QUIC)
        message_str = data.decode(errors='ignore')
        if "P2P_PING_FROM_" in message_str or "P2P_HOLE_PUNCH_PING_FROM_" in message_str:
            print(f"Worker '{self.worker_id}': UDP Ping received from {addr}: {message_str}")
            return

        # Debug logging to track protocol lookup
        print(f"Worker '{self.worker_id}': datagram_received from {addr}. Current peer_protocols keys: {list(self.peer_protocols.keys())}")
        protocol = self.peer_protocols.get(addr)

        if protocol:
            print(f"Worker '{self.worker_id}': Found existing protocol for {addr}. Is client: {protocol._quic._is_client if hasattr(protocol, '_quic') and protocol._quic else 'N/A'}")
            protocol.datagram_received(data, addr) # QuicConnectionProtocol handles 'now' internally via loop
        else:
            print(f"Worker '{self.worker_id}': No existing protocol found for {addr}. Checking if this should create server-side protocol...")
            
            # Check if we're already tracking this as a peer (might have initiated connection to them)
            if addr in self.peer_ids_by_addr:
                print(f"Worker '{self.worker_id}': WARNING: Received packet from known peer {self.peer_ids_by_addr[addr]} at {addr} but no protocol found!")
                print(f"Worker '{self.worker_id}': This suggests the client protocol was lost. Skipping server creation.")
                return
            
            # Potentially a new incoming QUIC connection (server role)
            # print(f"Worker '{self.worker_id}': New QUIC source {addr}, attempting to create server protocol.")
            try:
                # Parse the Original Destination Connection ID (ODCID) from the incoming packet
                # This is required for server-side QuicConnection initialization
                odcid = None
                
                if len(data) > 0 and (data[0] & 0x80):  # Check if it's a long header (Initial packet)
                    if len(data) >= 6:  # Minimum for flags, version, and DCID_len
                        # Skip version (4 bytes after flags)
                        dcid_len = data[5]
                        dcid_end_offset = 6 + dcid_len
                        
                        if len(data) >= dcid_end_offset:
                            odcid = data[6:dcid_end_offset]
                        else:
                            print(f"Worker '{self.worker_id}': Packet from {addr} too short for stated DCID length.")
                            return
                    else:
                        print(f"Worker '{self.worker_id}': Packet from {addr} too short for Long Header essentials.")
                        return
                else:
                    # Short header, not an initial packet for a new connection
                    print(f"Worker '{self.worker_id}': Received short header packet from unknown source {addr}. Ignoring.")
                    return
                
                if odcid is None:
                    print(f"Worker '{self.worker_id}': Could not parse ODCID from initial packet from {addr}. Ignoring.")
                    return
                
                config = create_quic_configuration(is_client=False)  # Server-side config
                
                # Create QuicConnection with the parsed ODCID
                quic_connection = QuicConnection(
                    configuration=config,
                    original_destination_connection_id=odcid
                )

                # The stream_handler callback needs context about the peer
                protocol = QuicConnectionProtocol(quic_connection, stream_handler=self.application_stream_handler_callback)
                protocol.connection_made(self.transport)  # IMPORTANT: Give it the transport
                protocol.datagram_received(data, addr)    # Feed the first packet

                self.peer_protocols[addr] = protocol
                print(f"Worker '{self.worker_id}': Added SERVER protocol to peer_protocols[{addr}]")
                # Start datagram handler task
                self.datagram_handler_tasks[addr] = asyncio.create_task(
                    self._handle_datagrams_for_protocol(protocol, addr)
                )
                print(f"Worker '{self.worker_id}': Created new server-side QuicConnectionProtocol for {addr} with ODCID {odcid.hex()}")

            except Exception as e:
                print(f"Worker '{self.worker_id}': Error creating server protocol for {addr}: {type(e).__name__} - {e}")
                import traceback
                traceback.print_exc()
        
    async def initiate_quic_connection(self, peer_addr: Tuple[str, int], peer_id: str):
        """Initiate a QUIC connection to a peer (client role) using QuicConnectionProtocol"""
        global current_p2p_peer_id, current_p2p_peer_addr # Update global P2P state

        if peer_addr in self.peer_protocols:
            print(f"Worker '{self.worker_id}': Protocol for {peer_id} at {peer_addr} already exists.")
            existing_protocol = self.peer_protocols[peer_addr]
            if (hasattr(existing_protocol, '_quic') and existing_protocol._quic and
                existing_protocol._quic._state not in [QuicConnectionState.CLOSING, QuicConnectionState.DRAINING, QuicConnectionState.TERMINATED]):
                print(f"Worker '{self.worker_id}': Existing protocol to {peer_id} seems healthy. Proceeding with P2P test.")
                # Update globals if this is a re-connection attempt via rendezvous
                current_p2p_peer_id = peer_id
                current_p2p_peer_addr = peer_addr
                await self.send_pairing_test_to_protocol(existing_protocol, peer_id, peer_addr)
                return
            else:
                print(f"Worker '{self.worker_id}': Existing protocol to {peer_id} not healthy. Will try to re-initiate.")
                # Clean up the old protocol
                self._cleanup_protocol(peer_addr)

        print(f"Worker '{self.worker_id}': Initiating new QUIC (client) QuicConnectionProtocol to {peer_id} at {peer_addr}")
        current_p2p_peer_id = peer_id
        current_p2p_peer_addr = peer_addr
        self.peer_ids_by_addr[peer_addr] = peer_id

        config = create_quic_configuration(is_client=True)
        quic_connection = QuicConnection(configuration=config)

        # Use the same stream handler callback
        protocol = QuicConnectionProtocol(quic_connection, stream_handler=self.application_stream_handler_callback)
        protocol.connection_made(self.transport)

        self.peer_protocols[peer_addr] = protocol
        print(f"Worker '{self.worker_id}': Added CLIENT protocol to peer_protocols[{peer_addr}]")
        # Start datagram handler task
        self.datagram_handler_tasks[peer_addr] = asyncio.create_task(
            self._handle_datagrams_for_protocol(protocol, peer_addr)
        )

        # Start the QUIC handshake (this method is on QuicConnection, accessed via _quic)
        now = time.time()
        protocol._quic.connect(peer_addr, now) # This populates data to be sent
        self._flush_protocol_datagrams(protocol, peer_addr)

        print(f"Worker '{self.worker_id}': QUIC client handshake initiated with {peer_id} at {peer_addr}.")
        await self.send_pairing_test_to_protocol(protocol, peer_id, peer_addr)
        
    async def send_pairing_test_to_protocol(self, protocol: QuicConnectionProtocol, peer_id_to_test: str, peer_addr: Tuple[str, int]):
        """Helper to send a pairing test message"""
        if not (hasattr(protocol, '_quic') and protocol._quic and
                protocol._quic._state not in [QuicConnectionState.CLOSING, QuicConnectionState.DRAINING, QuicConnectionState.TERMINATED]):
            print(f"Worker '{self.worker_id}': Cannot send pairing test to {peer_id_to_test}, protocol unhealthy or closed (state: {protocol._quic._state if hasattr(protocol, '_quic') and protocol._quic else 'N/A'}).")
            return

        # Send a pairing test datagram
        pairing_test_message = {
            "type": "p2p_pairing_test", 
            "from_worker_id": self.worker_id, 
            "original_timestamp": time.time()
        }
        try:
            protocol._quic.send_datagram_frame(json.dumps(pairing_test_message).encode())
            self._flush_protocol_datagrams(protocol, peer_addr) # Use the passed peer_addr
            print(f"Worker '{self.worker_id}': Sent pairing test datagram to {peer_id_to_test}.")
        except Exception as e:
            print(f"Worker '{self.worker_id}': Error sending pairing test datagram to {peer_id_to_test}: {e}")

    def _flush_protocol_datagrams(self, protocol: QuicConnectionProtocol, peer_addr: Tuple[str, int]):
        """Flush pending datagrams for a protocol (mainly for initial packets or explicit datagram sends)"""
        if self.transport and hasattr(protocol, '_quic'):
            now = time.time()
            for data_to_send, _ in protocol._quic.datagrams_to_send(now):
                self.transport.sendto(data_to_send, peer_addr)

    def get_protocol_for_peer(self, peer_addr: Tuple[str, int]) -> Optional[QuicConnectionProtocol]:
        return self.peer_protocols.get(peer_addr)
    
    def _cleanup_protocol(self, addr: Tuple[str, int]):
        """Clean up protocol and associated resources"""
        print(f"Worker '{self.worker_id}': Starting cleanup for protocol at {addr}")
        
        if addr in self.datagram_handler_tasks:
            task = self.datagram_handler_tasks.pop(addr)
            if not task.done():
                task.cancel()
        
        if addr in self.peer_protocols:
            protocol_to_close = self.peer_protocols.pop(addr)
            was_client = protocol_to_close._quic._is_client if hasattr(protocol_to_close, '_quic') and protocol_to_close._quic else 'unknown'
            print(f"Worker '{self.worker_id}': Removed {'CLIENT' if was_client else 'SERVER'} protocol from peer_protocols[{addr}]")
            if (hasattr(protocol_to_close, '_quic') and protocol_to_close._quic and
                protocol_to_close._quic._state not in [QuicConnectionState.CLOSING, QuicConnectionState.DRAINING, QuicConnectionState.TERMINATED]):
                protocol_to_close._quic.close(error_code=0x00, reason_phrase=b"cleanup")
        
        if addr in self.peer_ids_by_addr:
            del self.peer_ids_by_addr[addr]
        
        print(f"Worker '{self.worker_id}': Cleaned up protocol for {addr}")
    
    async def _handle_datagrams_for_protocol(self, protocol: QuicConnectionProtocol, peer_addr: Tuple[str, int]):
        """Handle incoming QUIC datagrams for a specific protocol"""
        global ui_websocket_clients, worker_id
        peer_id = self.peer_ids_by_addr.get(peer_addr)
        print(f"Worker '{self.worker_id}': Datagram handler active for {peer_addr}, known peer ID: {peer_id}")
        
        while (hasattr(protocol, '_quic') and protocol._quic and
               protocol._quic._state not in [QuicConnectionState.CLOSING, QuicConnectionState.DRAINING, QuicConnectionState.TERMINATED]):
            try:
                # Fetch and process events one-by-one (next_event returns a single QuicEvent or None)
                event = protocol._quic.next_event()
                while event is not None:
                    if isinstance(event, DatagramFrameReceived):
                        # Handle the datagram
                        data = event.data
                        message_str = data.decode(errors='ignore')
                        try:
                            p2p_message = json.loads(message_str)
                            msg_type = p2p_message.get("type")
                            from_id = p2p_message.get("from_worker_id")
                            
                            if msg_type == "p2p_pairing_test":
                                original_timestamp = p2p_message.get("original_timestamp")
                                rtt = (time.time() - original_timestamp) * 1000 if original_timestamp else -1
                                print(f"Worker '{self.worker_id}': Received p2p_pairing_test datagram from '{from_id}'. RTT: {rtt:.2f} ms")
                                for ui_client_ws in list(ui_websocket_clients):
                                    asyncio.create_task(ui_client_ws.send(json.dumps({
                                        "type": "p2p_status_update",
                                        "message": f"Pairing test with {from_id[:8]} successful! RTT: {rtt:.2f}ms"
                                    })))
                            
                            elif msg_type == "p2p_keep_alive":
                                # Keep-alive received – no action required beyond keeping connection alive
                                pass
                            
                            elif msg_type == "chat_message":
                                content = p2p_message.get("content")
                                print(f"Worker '{self.worker_id}': Received P2P chat datagram from '{from_id}': '{content}'")
                                for ui_client_ws in list(ui_websocket_clients):
                                    asyncio.create_task(ui_client_ws.send(json.dumps({
                                        "type": "p2p_message_received",
                                        "from_peer_id": from_id,
                                        "content": content,
                                        "reliable": False  # Datagrams are unreliable
                                    })))
                        except json.JSONDecodeError:
                            print(f"Worker '{self.worker_id}': Received non-JSON QUIC datagram: {message_str[:100]}")
                    # Fetch next pending event
                    event = protocol._quic.next_event()

                # Small delay to yield control back to the event loop and avoid busy-waiting
                await asyncio.sleep(0.1)  # Check periodically
            except Exception as e:
                print(f"Worker '{self.worker_id}': Error in datagram handler for {peer_addr}: {e}")
                break
        
        print(f"Worker '{self.worker_id}': Datagram handler task for {peer_addr} ending")
        
    def send_hole_punch_ping(self, peer_addr: Tuple[str, int], ping_num: int):
        """Send UDP hole punch ping"""
        if self.transport:
            message = f"P2P_HOLE_PUNCH_PING_FROM_{self.worker_id}_NUM_{ping_num}"
            self.transport.sendto(message.encode(), peer_addr)
            # print(f"Worker '{self.worker_id}': Sent UDP Hole Punch PING {ping_num} to {peer_addr}")
            
    # get_handler method removed - use get_protocol_for_peer instead
        
    def error_received(self, exc: Exception):
        print(f"Worker '{self.worker_id}': UDP listener error: {exc}")
        # Clean up all protocols
        for addr in list(self.peer_protocols.keys()):
            self._cleanup_protocol(addr)
        
    def connection_lost(self, exc: Optional[Exception]):
        global quic_dispatcher
        print(f"Worker '{self.worker_id}': UDP listener connection lost: {exc if exc else 'Closed normally'}")
        # Clean up all protocols
        for addr in list(self.peer_protocols.keys()):
            self._cleanup_protocol(addr)
        
        if self == quic_dispatcher:
            quic_dispatcher = None

def handle_shutdown_signal(signum, _frame):
    global stop_signal_received, quic_dispatcher
    print(f"Shutdown signal ({signum}) received. Worker '{worker_id}' attempting graceful shutdown.")
    stop_signal_received = True
    
    # Close QUIC connections
    if quic_dispatcher:
        for addr, protocol in list(quic_dispatcher.peer_protocols.items()):
            try:
                if (hasattr(protocol, '_quic') and protocol._quic and
                    protocol._quic._state not in [QuicConnectionState.CLOSING, QuicConnectionState.DRAINING, QuicConnectionState.TERMINATED]):
                    protocol._quic.close()
                print(f"Worker '{worker_id}': Closed QUIC connection to {addr}")
            except Exception as e:
                print(f"Worker '{worker_id}': Error closing QUIC connection: {e}")
                
    # Close UI websocket clients
    for ws_client in list(ui_websocket_clients):
        asyncio.create_task(ws_client.close(reason="Server shutting down"))

async def process_http_request(path: str, _request_headers: Headers) -> Optional[Tuple[int, Headers, bytes]]:
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
    global worker_id, quic_dispatcher
    
    benchmark_file_url = os.environ.get("BENCHMARK_GCS_URL")
    if not benchmark_file_url:
        err_msg = "BENCHMARK_GCS_URL not set. Cannot run file benchmark."
        print(f"Worker '{worker_id}': {err_msg}")
        if not ui_ws.closed: await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Error: {err_msg}"}))
        return

    # Use target_ip and target_port to identify the peer
    peer_addr_for_benchmark = (target_ip, target_port)
    
    if not quic_dispatcher:
        err_msg = "QUIC dispatcher not available."
        print(f"Worker '{worker_id}': {err_msg}")
        if not ui_ws.closed: await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Error: {err_msg}"}))
        return
        
    protocol = quic_dispatcher.get_protocol_for_peer(peer_addr_for_benchmark)
    if not protocol or not hasattr(protocol, '_quic') or not protocol._quic or \
       protocol._quic._state in [QuicConnectionState.CLOSING, QuicConnectionState.DRAINING, QuicConnectionState.TERMINATED]:
        err_msg = f"No active or healthy QUIC protocol for target {target_ip}:{target_port}."
        print(f"Worker '{worker_id}': {err_msg}")
        if not ui_ws.closed: await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Error: {err_msg}"}))
        return
    
    print(f"Worker '{worker_id}': Benchmark using protocol for {peer_addr_for_benchmark}. Is client: {protocol._quic._is_client}")
        
    print(f"Worker '{worker_id}': UI requested benchmark of {size_kb}KB to {target_ip}:{target_port} (file: {benchmark_file_url})")
    if not ui_ws.closed: await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Benchmarking: Preparing file from {benchmark_file_url}..."}))
    
    file_name_from_url = "unknown_file"
    actual_total_file_bytes = 0
    download_duration = 0.0

    try:
        print(f"Worker '{worker_id}': Downloading benchmark file: {benchmark_file_url}")
        download_start_time = time.monotonic()
        response = requests.get(benchmark_file_url, stream=True, timeout=180)
        response.raise_for_status()
        file_content_bytes = response.content
        actual_total_file_bytes = len(file_content_bytes)
        file_name_from_url = benchmark_file_url.split('/')[-1]
        download_duration = time.monotonic() - download_start_time

        download_status_msg = f"Downloaded '{file_name_from_url}' ({actual_total_file_bytes / (1024*1024):.2f} MB) in {download_duration:.2f}s. Preparing QUIC stream..."
        print(f"Worker '{worker_id}': {download_status_msg}")
        if not ui_ws.closed: await ui_ws.send(json.dumps({"type": "benchmark_status", "message": download_status_msg}))

        if protocol._quic._state in [QuicConnectionState.CLOSING, QuicConnectionState.DRAINING, QuicConnectionState.TERMINATED]: # Re-check after download
            raise ConnectionAbortedError("QUIC connection became invalid during file download.")

        data_stream_id = protocol._quic.get_next_available_stream_id(is_unidirectional=False)
        print(f"Worker '{worker_id}': Got stream ID {data_stream_id} from protocol (is_client: {protocol._quic._is_client})")
        quic_transfer_start_time = time.monotonic() 

        if stop_signal_received or ui_ws.closed:
            print(f"Worker '{worker_id}': Benchmark for '{file_name_from_url}' cancelled before QUIC send.")
            if not ui_ws.closed: await ui_ws.send(json.dumps({"type": "benchmark_status", "message": "Benchmark cancelled."}))
            return

        header_payload = {"type": "benchmark_start", "filename": file_name_from_url, "total_size": actual_total_file_bytes, "from_worker_id": worker_id}
        header_bytes = json.dumps(header_payload).encode() + b"\n"
        protocol._quic.send_stream_data(data_stream_id, header_bytes, end_stream=False)
        bytes_sent_on_stream = len(header_bytes)
        print(f"Worker '{worker_id}': Sent benchmark_start header on stream {data_stream_id}.")
        await asyncio.sleep(0)

        # 4. Send the actual downloaded file data
        if actual_total_file_bytes == 0:
            print(f"Worker '{worker_id}': Benchmark file '{file_name_from_url}' is empty. Will send header and then 0-byte FIN.")
        else:
            print(f"Worker '{worker_id}': Sending entire file '{file_name_from_url}' ({actual_total_file_bytes} bytes) over QUIC stream {data_stream_id}.")
            
            if stop_signal_received or ui_ws.closed:
                print(f"Worker '{worker_id}': Benchmark file send for '{file_name_from_url}' cancelled.")
                try:
                    cancel_marker = json.dumps({"type":"benchmark_cancelled", "reason":"client_request"}).encode() + b"\n"
                    protocol._quic.send_stream_data(data_stream_id, cancel_marker, end_stream=True)
                except Exception as cancel_e:
                    print(f"Worker '{worker_id}': Error sending CANCELLED marker: {cancel_e}")
                    if (protocol._quic and
                        protocol._quic._state not in [QuicConnectionState.CLOSING, QuicConnectionState.DRAINING, QuicConnectionState.TERMINATED]):
                       protocol._quic.close(error_code=0, reason_phrase=b"benchmark_cancelled_by_sender")
                await ui_ws.send(json.dumps({"type": "benchmark_status", "message": "Benchmark file send cancelled."}))
                return

            # Send entire file content at once - QUIC will handle segmentation
            protocol._quic.send_stream_data(data_stream_id, file_content_bytes, end_stream=False)
            bytes_sent_on_stream += len(file_content_bytes)
            
            # Flush to ensure data starts transmitting
            if quic_dispatcher:
                quic_dispatcher._flush_protocol_datagrams(protocol, peer_addr_for_benchmark)
            
            # Allow event loop to process
            await asyncio.sleep(0)
            
            progress_msg = f"Benchmark Send '{file_name_from_url}': Entire {actual_total_file_bytes / (1024*1024):.2f} MB queued for transmission"
            print(f"Worker '{worker_id}': {progress_msg}")
            await ui_ws.send(json.dumps({"type": "benchmark_status", "message": progress_msg}))
        
        # 5. Send final 0-length data with end_stream=True
        print(f"Worker '{worker_id}': All file data sent for '{file_name_from_url}'. Sending 0-byte frame with end_stream=True on stream {data_stream_id}.")
        protocol._quic.send_stream_data(data_stream_id, b'', end_stream=True)

        # 6. Final flush to ensure the last data and FIN are sent
        print(f"Worker '{worker_id}': Attempting final flush for stream {data_stream_id}.")
        await asyncio.sleep(0.01) # Tiny sleep to allow event loop to process outgoing if busy
        if quic_dispatcher: 
            quic_dispatcher._flush_protocol_datagrams(protocol, peer_addr_for_benchmark)
        print(f"Worker '{worker_id}': Final flush executed for stream {data_stream_id}.")

        quic_transfer_duration = time.monotonic() - quic_transfer_start_time
        
        throughput_mbps = (actual_total_file_bytes / (1024 * 1024)) / quic_transfer_duration if quic_transfer_duration > 0 else 0
        
        final_msg = f"Benchmark File Send Complete: Sent '{file_name_from_url}' ({actual_total_file_bytes / (1024*1024):.2f} MB) in {quic_transfer_duration:.2f}s (QUIC transfer). Throughput: {throughput_mbps:.2f} MB/s. (Download took {download_duration:.2f}s)"
        print(f"Worker '{worker_id}': {final_msg}")
        await ui_ws.send(json.dumps({"type": "benchmark_status", "message": final_msg}))
            
        return

    except requests.exceptions.RequestException as req_e:
        err_msg = f"Benchmark Error: Failed to download file from {benchmark_file_url}: {req_e}"
        print(f"Worker '{worker_id}': {err_msg}")
        if not ui_ws.closed: # Check if UI websocket is still open
            await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Error: {err_msg}"}))
    except Exception as e:
        error_msg = f"Benchmark Send Error: {type(e).__name__} - {e}"
        print(f"Worker '{worker_id}': {error_msg}")
        if not ui_ws.closed:
            await ui_ws.send(json.dumps({"type": "benchmark_status", "message": f"Error: {error_msg}"}))

async def ui_websocket_handler(websocket: websockets.WebSocketServerProtocol, _path: str):
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
                        protocol = quic_dispatcher.get_protocol_for_peer(current_p2p_peer_addr)
                        if (protocol and protocol._quic and
                            protocol._quic._state not in [QuicConnectionState.CLOSING, QuicConnectionState.DRAINING, QuicConnectionState.TERMINATED]):
                            print(f"Worker '{worker_id}': Sending P2P message '{content}' to peer {current_p2p_peer_id} (reliable={reliable})")
                            p2p_message = {
                                "type": "chat_message",
                                "from_worker_id": worker_id,
                                "content": content
                            }
                            
                            if reliable:
                                # Send via stream
                                stream_id = protocol._quic.get_next_available_stream_id()
                                print(f"Worker '{worker_id}': Sending chat on stream {stream_id} (is_client: {protocol._quic._is_client})")
                                json_bytes = json.dumps(p2p_message).encode() + b"\n"
                                protocol._quic.send_stream_data(stream_id, json_bytes, end_stream=True)
                            else:
                                # Send via datagram
                                protocol._quic.send_datagram_frame(json.dumps(p2p_message).encode())
                            
                            # Trigger sending
                            quic_dispatcher._flush_protocol_datagrams(protocol, current_p2p_peer_addr)
                        else:
                            await websocket.send(json.dumps({"type": "error", "message": "QUIC protocol not available."}))
                        
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
        if worker_id < peer_worker_id:
            # This side acts as the client-initiator for the QUIC handshake
            print(f"Worker '{worker_id}': Lexicographically first. Initiating QUIC connection to '{peer_worker_id}'.")
            await quic_dispatcher.initiate_quic_connection(current_p2p_peer_addr, peer_worker_id)

            # Pairing test is already sent in initiate_quic_connection
        else:
            # We are the responder; do NOT initiate a duplicate client connection – wait for peer's handshake
            print(f"Worker '{worker_id}': Lexicographically second. Waiting for peer '{peer_worker_id}' to initiate QUIC connection.")

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
            protocol = quic_dispatcher.get_protocol_for_peer(current_p2p_peer_addr)
            if (protocol and protocol._quic and
                protocol._quic._state not in [QuicConnectionState.CLOSING, QuicConnectionState.DRAINING, QuicConnectionState.TERMINATED]):
                try:
                    keep_alive_message = {
                        "type": "p2p_keep_alive",
                        "from_worker_id": worker_id
                    }
                    # print(f"Worker '{worker_id}': Sending keep-alive to {current_p2p_peer_addr}")
                    protocol._quic.send_datagram_frame(json.dumps(keep_alive_message).encode())
                    
                    # Trigger sending
                    quic_dispatcher._flush_protocol_datagrams(protocol, current_p2p_peer_addr)
                except Exception as e:
                    print(f"Worker '{worker_id}': Error sending P2P keep-alive: {e}")
            else:
                pass
                # print(f"Worker '{worker_id}': No protocol for peer {current_p2p_peer_addr}")
        else:
            pass
            # print(f"Worker '{worker_id}': Keep-alive skipped - dispatcher={quic_dispatcher is not None}, peer_addr={current_p2p_peer_addr}")
                    
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
                            lambda: QuicServerDispatcher(worker_id, quic_protocol_stream_handler_callback),
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
