import asyncio
import os
import time
from typing import Optional, Tuple, Callable, List, Dict
from pathlib import Path

from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.connection import QuicConnection
from aioquic.quic.events import StreamDataReceived, HandshakeCompleted, ConnectionTerminated

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

        # Buffer for QUIC stream data that may arrive before the local TCP
        # relay is fully established (mainly on the server side where the
        # relay is spun up only after the first data frame is observed).
        self._pending_quic_stream_data: Dict[int, List[bytes]] = {}

        print(f"Worker '{self.worker_id}': QuicTunnel initialized for peer {self.peer_addr}. Role: {'Client' if self.is_client else 'Server'}")

    def connect_if_client(self):
        if self.is_client:
            print(f"Worker '{self.worker_id}': QUIC client connecting to {self.peer_addr}")
            self.quic_connection.connect(self.peer_addr, now=time.time())
            self._transmit_pending_udp()
            self._start_timer_loop()

    def feed_datagram(self, data: bytes, sender_addr: Tuple[str, int]):
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
                        pass  # Binary payload, not an echo control frame

                    if data_str.startswith("QUIC_ECHO_REQUEST"):
                        parts = data_str.split(" ", 3)
                        # Format: QUIC_ECHO_REQUEST <worker_id> <timestamp> <payload>
                        if len(parts) >= 3:
                            try:
                                request_worker_id = parts[1]
                                original_ts = float(parts[2])
                                echoed_payload = parts[3] if len(parts) > 3 else ""

                                if request_worker_id == self.worker_id:
                                    # This is the response to our earlier request – measure RTT.
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
                                    # Do not forward this response further.
                                    event = self.quic_connection.next_event()
                                    continue
                                else:
                                    # This is a request from the peer; echo it back verbatim.
                                    if self._quic_stream_id is not None:
                                        try:
                                            self.quic_connection.send_stream_data(
                                                self._quic_stream_id, event.data
                                            )
                                            self._transmit_pending_udp()
                                        except Exception as e_echo_send:
                                            print(
                                                f"Worker '{self.worker_id}': Error echoing QUIC request back: {e_echo_send}"
                                            )
                                    # No RTT calculation; we are just the mirror.
                                    event = self.quic_connection.next_event()
                                    continue
                            except (ValueError, IndexError):
                                print(
                                    f"Worker '{self.worker_id}': Malformed QUIC_ECHO_REQUEST string: {data_str}"
                                )

                    # Forward payload to local TCP relay or buffer until ready
                    if self._local_tcp_writer:
                        self._local_tcp_writer.write(event.data)
                    else:
                        self._pending_quic_stream_data.setdefault(event.stream_id, []).append(event.data)

                    if event.end_stream:
                        print(f"Worker '{self.worker_id}': QUIC stream {event.stream_id} ended by peer.")
                        if self._local_tcp_writer:
                            self._local_tcp_writer.close()
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
                self._transmit_pending_udp()
                
                timeout = self.quic_connection.get_timer()
                
                # send keep-alive ping every 5 s
                now_val = time.time()
                if now_val - self._last_ping_time > 5.0:
                    try:
                        self.quic_connection.ping()
                        self._last_ping_time = now_val
                    except Exception:
                        pass

                if self._connection_terminated:
                    print(f"Worker '{self.worker_id}': QUIC connection marked terminated, stopping timer loop.")
                    break

                if timeout is None:
                    await asyncio.sleep(0.05)
                    continue

                delay = max(0, timeout - time.time())
                await asyncio.sleep(delay)

                try:
                    self.quic_connection.handle_timer(now=time.time())
                except Exception as e_ht:
                    print(
                        f"Worker '{self.worker_id}': Exception in handle_timer: {type(e_ht).__name__} – {e_ht}"
                    )
                    self._connection_terminated = True
                    break
        except asyncio.CancelledError:
            print(f"Worker '{self.worker_id}': QUIC timer loop cancelled for peer {self.peer_addr}.")
        except Exception as e:
            print(f"Worker '{self.worker_id}': Exception in QUIC timer loop: {e}")
        finally:
            print(f"Worker '{self.worker_id}': QUIC timer loop exited for peer {self.peer_addr}.")

    async def close(self):
        # Assuming quic_engine global is managed in main.py or passed if needed
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

        if not self._connection_terminated:
            try:
                self.quic_connection.close(error_code=0, reason_phrase="Tunnel closing")
                self._transmit_pending_udp()
            except Exception:
                pass
        
        # Logic to update global quic_engine should be handled by the caller in main.py
        print(f"Worker '{self.worker_id}': QUIC tunnel with {self.peer_addr} resources released.")

    @property
    def handshake_completed(self) -> bool:
        return self._handshake_completed 

    async def send_app_data(self, data: bytes):
        """Send application data over the relay stream, opening one if necessary."""
        if self._quic_stream_id is None:
            # Allocate a bidirectional stream
            self._quic_stream_id = self.quic_connection.get_next_available_stream_id(False)
            print(f"Worker '{self.worker_id}': Opening new QUIC stream {self._quic_stream_id} for app data.")
        self.quic_connection.send_stream_data(self._quic_stream_id, data, end_stream=False)
        self._transmit_pending_udp() 