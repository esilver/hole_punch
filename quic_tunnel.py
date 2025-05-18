import asyncio
import os
import time
from typing import Optional, Tuple, Callable, List, Dict
from pathlib import Path

from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import StreamDataReceived, HandshakeCompleted, ConnectionTerminated, PingAcknowledged

# Attempt to import QuicErrorCode, otherwise use a default integer
try:
    from aioquic.quic.packet import QuicErrorCode
    INTERNAL_QUIC_ERROR_CODE = QuicErrorCode.INTERNAL_ERROR
except ImportError:
    INTERNAL_QUIC_ERROR_CODE = 1 # Default internal error code

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import datetime
import ssl # Added for ssl.CERT_NONE

# --- QUIC Tunnel Globals & Constants ---
CERTIFICATE_FILE = Path(__file__).parent / "cert.pem"
PRIVATE_KEY_FILE = Path(__file__).parent / "key.pem"
LOCAL_QUIC_RELAY_HOST = "127.0.0.1"
LOCAL_QUIC_RELAY_PORT = int(os.environ.get("LOCAL_QUIC_RELAY_PORT", "9000"))

# Notify UI clients about QUIC status changes
from ui_server import broadcast_to_all_ui_clients

class QuicTunnel:
    def __init__(
        self,
        worker_id_val: str,
        peer_addr_val: Tuple[str, int],
        udp_sender_func: Callable[[bytes, Tuple[str, int]], None],
        is_client_role: bool,
        original_destination_cid: Optional[bytes] = None,
        on_close_callback: Optional[Callable[[], None]] = None,
    ):
        self.worker_id = worker_id_val
        self.peer_addr = peer_addr_val
        self.udp_sender = udp_sender_func
        self.is_client = is_client_role
        
        self.quic_config = QuicConfiguration(
            is_client=self.is_client,
            alpn_protocols=["p2p-tunnel/0.1"],
            idle_timeout=20.0  # seconds – keep connection for at least 20 s of silence
        )

        if self.is_client:
            self.quic_config.verify_mode = ssl.CERT_NONE

        if not CERTIFICATE_FILE.exists() or not PRIVATE_KEY_FILE.exists():
            if not self.is_client:
                try:
                    print(f"Worker '{self.worker_id}': Generating self-signed certificate (RSA-2048) for QUIC server …")

                    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                    subject = issuer = x509.Name([
                        x509.NameAttribute(NameOID.COMMON_NAME, u"localhost")
                    ])
                    cert = (
                        x509.CertificateBuilder()
                        .subject_name(subject)
                        .issuer_name(issuer)
                        .public_key(key.public_key())
                        .serial_number(x509.random_serial_number())
                        .not_valid_before(datetime.datetime.utcnow() - datetime.timedelta(days=1))
                        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
                        .add_extension(
                            x509.SubjectAlternativeName([x509.DNSName(u"localhost")]),
                            critical=False,
                        )
                        .sign(key, hashes.SHA256())
                    )

                    try:
                        with open(PRIVATE_KEY_FILE, "wb") as f_key:
                            f_key.write(
                                key.private_bytes(
                                    encoding=serialization.Encoding.PEM,
                                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                                    encryption_algorithm=serialization.NoEncryption(),
                                )
                            )
                        with open(CERTIFICATE_FILE, "wb") as f_cert:
                            f_cert.write(cert.public_bytes(serialization.Encoding.PEM))
                    except PermissionError:
                        import tempfile
                        import uuid as _uuid # Renamed to avoid conflict with global uuid
                        tmp_dir = Path(tempfile.gettempdir())
                        # Use global CERTIFICATE_FILE, PRIVATE_KEY_FILE for modification
                        # This assumes these are intended to be module-level globals in quic_tunnel.py
                        globals()['CERTIFICATE_FILE'] = tmp_dir / f"cert-{_uuid.uuid4().hex}.pem"
                        globals()['PRIVATE_KEY_FILE'] = tmp_dir / f"key-{_uuid.uuid4().hex}.pem"

                        with open(globals()['PRIVATE_KEY_FILE'], "wb") as f_key:
                            f_key.write(
                                key.private_bytes(
                                    encoding=serialization.Encoding.PEM,
                                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                                    encryption_algorithm=serialization.NoEncryption(),
                                )
                            )
                        with open(globals()['CERTIFICATE_FILE'], "wb") as f_cert:
                            f_cert.write(cert.public_bytes(serialization.Encoding.PEM))
                        print(f"Worker '{self.worker_id}': Filesystem read-only, wrote self-signed certs to {globals()['CERTIFICATE_FILE']} / {globals()['PRIVATE_KEY_FILE']}.")
                except Exception as e_gen:
                    print(f"Worker '{self.worker_id}': ERROR generating self-signed cert: {type(e_gen).__name__} – {e_gen}")
            else:
                pass

        if CERTIFICATE_FILE.exists() and PRIVATE_KEY_FILE.exists():
            try:
                self.quic_config.load_cert_chain(str(CERTIFICATE_FILE), str(PRIVATE_KEY_FILE))
            except Exception as e_load:
                print(f"Worker '{self.worker_id}': Failed to load certificate chain – proceeding as client-only. {type(e_load).__name__}: {e_load}")

        odcid = (
            original_destination_cid
            if original_destination_cid is not None
            else os.urandom(self.quic_config.connection_id_length)
        )
        conn_kwargs = {
            "configuration": self.quic_config,
        }
        if not self.is_client:
            conn_kwargs["original_destination_connection_id"] = odcid

        self.quic_connection = QuicConnection(**conn_kwargs)

        try:
            from aioquic.quic.connection import QuicReceiveContext  # type: ignore

            if not hasattr(QuicReceiveContext, "is_handshake_complete"):
                def _old_alias(self):  # pylint: disable=unused-argument
                    return getattr(self, "handshake_complete", False)

                QuicReceiveContext.is_handshake_complete = property(_old_alias)  # type: ignore
        except Exception:
            pass

        self._local_tcp_reader: Optional[asyncio.StreamReader] = None
        self._local_tcp_writer: Optional[asyncio.StreamWriter] = None
        self._quic_stream_id: Optional[int] = None
        self._timer_task: Optional[asyncio.Task] = None
        self._connection_terminated: bool = False
        self._relay_tasks: List[asyncio.Task] = []
        self._handshake_completed: bool = False
        self._last_ping_time: float = time.time()
        self._ping_uid_counter: int = 1  # incremental UID for send_ping()
        self._underlying_transport_lost: bool = False # Flag for lost transport

        # Buffer for QUIC stream data that may arrive before the local TCP
        # relay is fully established (mainly on the server side where the
        # relay is spun up only after the first data frame is observed).
        self._pending_quic_stream_data: Dict[int, List[bytes]] = {}
        self._quic_state_lock = asyncio.Lock() # Lock for QUIC connection operations
        self._on_close_callback = on_close_callback # Store callback

        print(f"Worker '{self.worker_id}': QuicTunnel initialized for peer {self.peer_addr}. Role: {'Client' if self.is_client else 'Server'}")

    async def connect_if_client(self):
        if self.is_client:
            print(f"Worker '{self.worker_id}': QUIC client connecting to {self.peer_addr}")
            async with self._quic_state_lock: # Protect connect and initial transmit
                self.quic_connection.connect(self.peer_addr, now=time.time())
                self._transmit_pending_udp() # Assumes this is safe to call under lock / doesn't re-lock
            self._start_timer_loop()

    async def feed_datagram(self, data: bytes, sender_addr: Tuple[str, int]):
        async with self._quic_state_lock:
            try:
                self.quic_connection.receive_datagram(data, sender_addr, now=time.time())
            except Exception as e_rd:
                print(
                    f"Worker '{self.worker_id}': QUIC receive_datagram ignored invalid packet from {sender_addr}: {type(e_rd).__name__} – {e_rd}"
                )
                return

            self._process_quic_events()
            self._transmit_pending_udp()

    def _transmit_pending_udp(self):
        for data, addr in self.quic_connection.datagrams_to_send(now=time.time()):
            self.udp_sender(data, addr)

    def _process_quic_events(self):
        event = self.quic_connection.next_event()
        while event:
            if isinstance(event, HandshakeCompleted):
                print(f"Worker '{self.worker_id}': QUIC HandshakeCompleted with {self.peer_addr}. ALPN: {event.alpn_protocol}")
                if self.is_client:
                    self._quic_stream_id = self.quic_connection.get_next_available_stream_id(is_unidirectional=False)
                    print(f"Worker '{self.worker_id}': QUIC client opening stream {self._quic_stream_id} for relay.")
                    self._relay_tasks.append(asyncio.create_task(self._start_local_tcp_relay()))
                # Inform UI clients so they can show a connected badge.
                asyncio.create_task(
                    broadcast_to_all_ui_clients({
                        "type": "quic_status_update",
                        "state": "connected",
                        "role": "client" if self.is_client else "server",
                        "peer": f"{self.peer_addr[0]}:{self.peer_addr[1]}"
                    })
                )
                self._handshake_completed = True  # Mark tunnel as ready upon handshake completion
            elif isinstance(event, StreamDataReceived):
                if self._quic_stream_id is None and not self.is_client :
                    self._quic_stream_id = event.stream_id
                    print(f"Worker '{self.worker_id}': QUIC server accepting relay on new stream {self._quic_stream_id}.")
                    self._relay_tasks.append(asyncio.create_task(self._start_local_tcp_relay()))

                if event.stream_id == self._quic_stream_id:
                    # Attempt echo-detection regardless of TCP relay state
                    data_str = ""
                    try:
                        data_str = event.data.decode()
                    except UnicodeDecodeError:
                        # Non-UTF8 payload – not an echo control frame.
                        data_str = ""

                    # Efficiently handle the possibility that multiple echo requests
                    # were coalesced into a single StreamDataReceived event.  We split
                    # the buffer at the control-frame prefix and process each chunk
                    # independently.
                    if "QUIC_ECHO_REQUEST" in data_str:
                        # The first element from split() may be an empty string if the
                        # data begins with the prefix, so we skip empties.
                        # We re-attach the prefix so each chunk is a full frame.
                        potential_frames = [
                            "QUIC_ECHO_REQUEST " + chunk for chunk in data_str.split("QUIC_ECHO_REQUEST ") if chunk
                        ]

                        for frame in potential_frames:
                            parts = frame.split(" ", 3)
                            # Expected format:
                            #   QUIC_ECHO_REQUEST <worker_id> <timestamp> <payload>
                            if len(parts) < 3:
                                print(
                                    f"Worker '{self.worker_id}': Malformed QUIC_ECHO_REQUEST (too few parts): {frame}"
                                )
                                continue

                            request_worker_id = parts[1]
                            try:
                                original_ts = float(parts[2])
                            except ValueError:
                                print(
                                    f"Worker '{self.worker_id}': Invalid timestamp in QUIC_ECHO_REQUEST: {parts[2]}"
                                )
                                continue

                            echoed_payload = parts[3] if len(parts) > 3 else ""

                            if request_worker_id == self.worker_id:
                                # This is *our* original request coming back – measure RTT.
                                rtt_ms = (time.monotonic() - original_ts) * 1000
                                print(
                                    f"Worker '{self.worker_id}': QUIC Echo response received, RTT: {rtt_ms:.2f} ms. Broadcasting to UI."
                                )
                                asyncio.create_task(
                                    broadcast_to_all_ui_clients(
                                        {
                                            "type": "quic_echo_response",
                                            "rtt_ms": rtt_ms,
                                            "payload": echoed_payload,
                                            "peer": f"{self.peer_addr[0]}:{self.peer_addr[1]}",
                                        }
                                    )
                                )
                            else:
                                # This is a request from the peer – echo it back verbatim.
                                if self._quic_stream_id is not None:
                                    try:
                                        self.quic_connection.send_stream_data(
                                            self._quic_stream_id, frame.encode()
                                        )
                                        self._transmit_pending_udp()
                                    except Exception as e_echo_send:
                                        print(
                                            f"Worker '{self.worker_id}': Error echoing QUIC request back: {e_echo_send}"
                                        )
                        # After handling echo frames (either responded or mirrored),
                        # we continue to the next QUIC event so the remainder of the
                        # processing logic does not incorrectly forward these control
                        # frames to the TCP relay.
                        event = self.quic_connection.next_event()
                        continue

                    # Forward payload to local TCP relay or buffer until ready
                    if self._local_tcp_writer:
                        self._local_tcp_writer.write(event.data)
                    else:
                        self._pending_quic_stream_data.setdefault(event.stream_id, []).append(event.data)

                    if event.end_stream:
                        print(f"Worker '{self.worker_id}': QUIC stream {event.stream_id} ended by peer.")
                        if self._local_tcp_writer:
                            self._local_tcp_writer.close()
            elif isinstance(event, PingAcknowledged):
                # Simple keep-alive / RTT observation – we currently just log.
                print(f"Worker '{self.worker_id}': QUIC PingAcknowledged (uid={event.uid}) from {self.peer_addr}.")
            elif isinstance(event, ConnectionTerminated):
                print(
                    f"Worker '{self.worker_id}': QUIC ConnectionTerminated with {self.peer_addr}. "
                    f"Error: {event.error_code}, Reason: {event.reason_phrase}"
                )
                # Notify UI of disconnection
                asyncio.create_task(
                    broadcast_to_all_ui_clients({
                        "type": "quic_status_update",
                        "state": "disconnected",
                        "peer": f"{self.peer_addr[0]}:{self.peer_addr[1]}"
                    })
                )
                self._connection_terminated = True
                asyncio.create_task(self.close())
            event = self.quic_connection.next_event()

        if not self._handshake_completed:
            tls_obj = getattr(self.quic_connection, 'tls', None)
            if tls_obj and getattr(tls_obj, 'handshake_complete', False):
                self._handshake_completed = True

    async def _start_local_tcp_relay(self):
        try:
            print(f"Worker '{self.worker_id}': Attempting to connect local TCP relay to {LOCAL_QUIC_RELAY_HOST}:{LOCAL_QUIC_RELAY_PORT}")
            self._local_tcp_reader, self._local_tcp_writer = await asyncio.open_connection(
                LOCAL_QUIC_RELAY_HOST, LOCAL_QUIC_RELAY_PORT
            )
            print(f"Worker '{self.worker_id}': Local TCP relay connected to {LOCAL_QUIC_RELAY_HOST}:{LOCAL_QUIC_RELAY_PORT}. Relaying stream {self._quic_stream_id}.")
            
            self._relay_tasks.append(asyncio.create_task(self._local_tcp_to_quic_loop()))
            
            # Flush any QUIC payloads that arrived before the relay was ready
            # (common for the first packet on the server side).
            try:
                buffered = self._pending_quic_stream_data.pop(self._quic_stream_id, [])
                for chunk in buffered:
                    self._local_tcp_writer.write(chunk)
                if buffered:
                    await self._local_tcp_writer.drain()
            except Exception as e_flush:
                print(f"Worker '{self.worker_id}': Error flushing buffered QUIC data to local TCP: {e_flush}")
            
        except ConnectionRefusedError:
            print(
                f"Worker '{self.worker_id}': QUIC Relay WARNING - Connection refused for local TCP {LOCAL_QUIC_RELAY_HOST}:{LOCAL_QUIC_RELAY_PORT}. "
                "Leaving QUIC connection open so peer can retry later."
            )
            if self._quic_stream_id is not None:
                try:
                    self.quic_connection.send_stream_data(self._quic_stream_id, b"", end_stream=True)
                except Exception:
                    pass
                self._quic_stream_id = None
        except Exception as e:
            print(
                f"Worker '{self.worker_id}': QUIC Relay ERROR - Unexpected failure connecting local TCP relay: {e}. "
                "Keeping QUIC link up."
            )
            if self._quic_stream_id is not None:
                try:
                    self.quic_connection.send_stream_data(self._quic_stream_id, b"", end_stream=True)
                except Exception:
                    pass
                self._quic_stream_id = None

    async def _local_tcp_to_quic_loop(self):
        if not self._local_tcp_reader or not self._quic_stream_id: return
        try:
            while True:
                data = await self._local_tcp_reader.read(4096)
                if not data:
                    print(f"Worker '{self.worker_id}': Local TCP connection closed. Ending QUIC stream {self._quic_stream_id}.")
                    self.quic_connection.send_stream_data(self._quic_stream_id, b'', end_stream=True)
                    break
                self.quic_connection.send_stream_data(self._quic_stream_id, data)
                self._transmit_pending_udp()
        except asyncio.CancelledError:
            print(f"Worker '{self.worker_id}': _local_tcp_to_quic_loop cancelled.")
        except Exception as e:
            print(f"Worker '{self.worker_id}': Error in _local_tcp_to_quic_loop: {e}")
            if self._quic_stream_id is not None:
                self.quic_connection.send_stream_data(self._quic_stream_id, b'', end_stream=True)
                self._transmit_pending_udp()
        finally:
            if self._local_tcp_writer and not self._local_tcp_writer.is_closing():
                self._local_tcp_writer.close()

    def _start_timer_loop(self):
        if self._timer_task is None or self._timer_task.done():
            self._timer_task = asyncio.create_task(self._timer_management_loop())
            print(f"Worker '{self.worker_id}': QUIC timer loop started for peer {self.peer_addr}.")

    async def _timer_management_loop(self):
        try:
            while True:
                # Transmit any datagrams queued by previous operations,
                # before acquiring lock for new timer-driven operations.
                # This _transmit_pending_udp is outside the main lock for this iteration's timer events.
                # It handles packets from prior locked sections.
                # If _transmit_pending_udp needs a lock itself, this design needs adjustment.
                # For now, assuming _transmit_pending_udp just sends and doesn't need _quic_state_lock itself.
                # This is consistent with it being called inside the lock in feed_datagram/handle_timer.
                # So, calls to _transmit_pending_udp and _process_quic_events are *part* of the locked operation.
                # This initial transmit handles datagrams from operations outside this loop's main locked block.
                # This needs to be re-evaluated.
                # Let's assume the lock should cover any sequence of Q opérations.
                # The original code:
                # self._transmit_pending_udp() --> This is for prior things.
                # timeout = self.quic_connection.get_timer() --> Needs lock
                # if PING_TIME: self.quic_connection.ping() --> Needs lock, then transmit
                # await asyncio.sleep(delay)
                # self.quic_connection.handle_timer() --> Needs lock
                # self._process_quic_events() --> Part of handle_timer op
                # self._transmit_pending_udp() --> Part of handle_timer op

                # Revised structure for timer loop:

                # Determine next timeout. get_timer() reads state, let's assume it's safe or lock it briefly.
                # For safety, operations on quic_connection should be locked.
                current_timeout: Optional[float]
                async with self._quic_state_lock:
                    current_timeout = self.quic_connection.get_timer()
                    # Transmit anything pending before sleeping or deciding next action
                    self._transmit_pending_udp()


                # Ping logic
                now_val = time.time()
                if now_val - self._last_ping_time > 5.0:
                    async with self._quic_state_lock:
                        try:
                            print(f"Worker '{self.worker_id}': QUIC sending PING to peer {self.peer_addr}")
                            # aioquic expects a unique int UID for each PING frame.
                            self.quic_connection.send_ping(self._ping_uid_counter)
                            # Increment counter, wrap at 2^31 to avoid overflow.
                            self._ping_uid_counter = (self._ping_uid_counter + 1) & 0x7FFFFFFF
                        except Exception as e_ping:
                            print(f"Worker '{self.worker_id}': QUIC ping failed: {e_ping}")
                    self._last_ping_time = now_val
                    # After pinging, re-check timer as ping might affect it
                    async with self._quic_state_lock:
                        current_timeout = self.quic_connection.get_timer()


                if self._connection_terminated:
                    print(f"Worker '{self.worker_id}': QUIC connection marked terminated, stopping timer loop.")
                    break

                if current_timeout is None:
                    # No specific timer, short poll.
                    # This might happen if connection is idle but not yet timed out by idle_timeout.
                    # Pings are handled above.
                    await asyncio.sleep(0.1) # Check again in 100ms
                    continue

                delay = max(0, current_timeout - time.time())
                await asyncio.sleep(delay)

                # Timer has expired, handle it
                async with self._quic_state_lock:
                    try:
                        # print(f"Worker '{self.worker_id}': QUIC handling timer for peer {self.peer_addr}")
                        self.quic_connection.handle_timer(now=time.time())
                        self._process_quic_events() 
                        self._transmit_pending_udp()
                    except Exception as e_ht:
                        print(
                            f"Worker '{self.worker_id}': Exception in handle_timer sequence: {type(e_ht).__name__} – {e_ht}"
                        )
                        # If handle_timer itself raises ConnectionTerminated, _process_quic_events might set this.
                        # Or if another error occurs, we might need to terminate.
                        if not isinstance(e_ht, ConnectionTerminated): # aioquic might raise this
                             print(f"Worker \'{self.worker_id}\': Unhandled exception in handle_timer, closing connection.")
                             # We are already in a lock, so direct calls are fine.
                             self.quic_connection.close(error_code=INTERNAL_QUIC_ERROR_CODE, reason_phrase=f"Timer handling error: {type(e_ht).__name__}")
                             self._connection_terminated = True # Mark it
                             self._transmit_pending_udp() # Send the close
                        # If it was ConnectionTerminated, _process_quic_events would handle flag.
                        
                if self._connection_terminated: # Re-check after handle_timer processing
                    print(f"Worker '{self.worker_id}': QUIC connection terminated after handle_timer, stopping timer loop.")
                    break
        except asyncio.CancelledError:
            print(f"Worker '{self.worker_id}': QUIC timer loop cancelled for peer {self.peer_addr}.")
        except Exception as e:
            print(f"Worker '{self.worker_id}': Exception in QUIC timer loop: {e}")
        finally:
            print(f"Worker '{self.worker_id}': QUIC timer loop exited for peer {self.peer_addr}.")

    async def notify_transport_lost(self):
        print(f"Worker '{self.worker_id}': Underlying UDP transport was lost for QUIC tunnel to {self.peer_addr}.")
        self._underlying_transport_lost = True
        if not self._connection_terminated:
            self._connection_terminated = True 
            
            async with self._quic_state_lock: # Lock for QUIC operations
                try:
                    self.quic_connection.close(error_code=INTERNAL_QUIC_ERROR_CODE, reason_phrase="Underlying transport lost")
                    self._transmit_pending_udp() 
                except Exception as e_close_on_lost_transport:
                    print(f"Worker '{self.worker_id}': Exception while trying to close QUIC connection on transport loss: {e_close_on_lost_transport}")

            asyncio.create_task(self.close())

    async def close(self):
        print(f"Worker '{self.worker_id}': Closing QUIC tunnel with {self.peer_addr}.")
        if self._timer_task:
            self._timer_task.cancel()
            try: await self._timer_task
            except asyncio.CancelledError: pass
        for task in self._relay_tasks:
            if not task.done():
                task.cancel()
                try: await task
                except asyncio.CancelledError: pass
        
        if self._local_tcp_writer and not self._local_tcp_writer.is_closing():
            self._local_tcp_writer.close()
            try: await self._local_tcp_writer.wait_closed()
            except Exception: pass

        # _connection_terminated might be set by notify_transport_lost or ConnectionTerminated event
        # We still attempt a final close operation on quic_connection if not already done.
        if not getattr(self.quic_connection, '_is_closed_completely', False): # Hypothetical check if aioquic has such a flag
             # More robust: check self._connection_terminated. If another path already closed it, this is a no-op or safe re-close.
            if not self._connection_terminated: # Only attempt close if not already marked terminated
                async with self._quic_state_lock: 
                    try:
                        print(f"Worker '{self.worker_id}': Attempting to close QUIC connection for {self.peer_addr}.")
                        self.quic_connection.close(error_code=0, reason_phrase="Tunnel closing") 
                        self._transmit_pending_udp()
                    except Exception as e_close: 
                        print(f"Worker '{self.worker_id}': Exception during QUIC connection close: {e_close}")
                self._connection_terminated = True # Mark terminated after attempting close
        
        print(f"Worker '{self.worker_id}': QUIC tunnel with {self.peer_addr} resources released.")
        # Call the on_close_callback if it exists, after all resources are released.
        if self._on_close_callback:
            try:
                self._on_close_callback()
            except Exception as e_callback:
                print(f"Worker '{self.worker_id}': Error in QuicTunnel on_close_callback: {e_callback}")

    @property
    def handshake_completed(self) -> bool:
        return self._handshake_completed 

    async def send_app_data(self, data: bytes):
        """Send application data over the relay stream, opening one if necessary."""
        # This method could be called from external application logic.
        if self._connection_terminated or self._underlying_transport_lost:
            print(f"Worker '{self.worker_id}': Cannot send app data, connection is terminated or transport lost.")
            return

        async with self._quic_state_lock:
            if self._quic_stream_id is None:
                # TODO: what if not self.is_client? Server might not initiate streams this way.
                # This is for app-initiated data, primarily from client perspective or established stream.
                # Consider if server needs to open new stream for app data not in reply.
                self._quic_stream_id = self.quic_connection.get_next_available_stream_id(is_unidirectional=False)
                print(f"Worker '{self.worker_id}': Opening new QUIC stream {self._quic_stream_id} for app data.")
            self.quic_connection.send_stream_data(self._quic_stream_id, data, end_stream=False)
            self._transmit_pending_udp()

    def is_terminated_or_transport_lost(self) -> bool:
        """Check if the tunnel is effectively unusable."""
        return self._connection_terminated or self._underlying_transport_lost 