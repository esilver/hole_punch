#!/usr/bin/env python3
"""Cloud-Run worker that attempts simultaneous-open TCP hole punching (2025-ready)."""
import asyncio
import http.server
import json
import logging # <<< Added
import os
import signal
import socket # NEW: For SO_REUSEPORT
import sys # Added for sys.version_info
import threading
import uuid
from typing import Any, Dict, Optional, List # Added List for type hinting
import errno

import requests # type: ignore
import websockets # type: ignore
import aiohttp # NEW: For async HTTP requests
# import stun # REMOVED: For STUN-based NAT discovery (pip install pystun3)

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(module)s:%(lineno)d - %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__) # Using a specific logger for the module

WORKER_ID = str(uuid.uuid4())
# Ensure getenv defaults are strings for safety, then cast
INTERNAL_TCP_PORT = int(os.getenv("INTERNAL_TCP_PORT", "8082"))
APP_PORT = int(os.getenv("PORT", "8080")) # For Cloud Run health checks
RENDEZVOUS_URL = os.getenv("RENDEZVOUS_SERVICE_URL")
# Timeouts can be made configurable
WS_OPEN_TIMEOUT = float(os.getenv("WS_OPEN_TIMEOUT", "15.0"))
WS_RECV_TIMEOUT = float(os.getenv("WS_RECV_TIMEOUT", "120.0"))
LISTENER_READY_TIMEOUT = float(os.getenv("LISTENER_READY_TIMEOUT", "15.0"))
P2P_MSG_TIMEOUT = float(os.getenv("P2P_MSG_TIMEOUT", "10.0"))
# STUN_HOST = os.getenv("STUN_HOST") # REMOVED: STUN server host
# STUN_PORT = int(os.getenv("STUN_PORT", "3478")) # REMOVED: STUN server port
# STUN_TIMEOUT = float(os.getenv("STUN_TIMEOUT", "10.0")) # REMOVED: Timeout for STUN query
EXTERNAL_ECHO_SERVER_HOST = os.getenv("EXTERNAL_ECHO_SERVER_HOST") # REINSTATED
EXTERNAL_ECHO_SERVER_PORT = int(os.getenv("EXTERNAL_ECHO_SERVER_PORT", "8080")) # REINSTATED (defaulted to 8080, was 12345)
ECHO_CONNECT_TIMEOUT = float(os.getenv("ECHO_CONNECT_TIMEOUT", "20.0")) # REINSTATED
ECHO_READ_TIMEOUT = float(os.getenv("ECHO_READ_TIMEOUT", "20.0")) # REINSTATED

STOP_EVENT = asyncio.Event()

# P2P connection retry parameters (override with env-vars if you like)
P2P_CONNECT_MAX_RETRIES = int(os.getenv("P2P_CONNECT_MAX_RETRIES", "6"))  # total dial attempts
P2P_RETRY_DELAY = float(os.getenv("P2P_RETRY_DELAY", "5.0"))              # seconds between attempts

# ────────────────────── TCP helpers ──────────────────────
async def handle_peer_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, role: str) -> None:
    peer_address_info = writer.get_extra_info("peername", "Unknown Peer")
    logger.info(f"Worker {WORKER_ID} [{role.upper()}]: TCP P2P Connection ESTABLISHED with {peer_address_info}!")
    try:
        if role == "client":
            message = f"Hello from P2P TCP Client {WORKER_ID}"
            logger.info(f"Worker {WORKER_ID} [CLIENT]: Sending: '{message}' to {peer_address_info}")
            writer.write(message.encode())
            await writer.drain()

        data = await asyncio.wait_for(reader.read(100), timeout=P2P_MSG_TIMEOUT)
        received_message = data.decode()
        logger.info(f"Worker {WORKER_ID} [{role.upper()}]: Received: '{received_message}' from {peer_address_info}")

        if role == "server":
            response_message = f"Hello back from P2P TCP Server {WORKER_ID}"
            logger.info(f"Worker {WORKER_ID} [SERVER]: Sending: '{response_message}' to {peer_address_info}")
            writer.write(response_message.encode())
            await writer.drain()
        logger.info(f"Worker {WORKER_ID} [{role.upper()}]: P2P message exchange with {peer_address_info} successful.")

    except asyncio.TimeoutError:
        logger.warning(f"Worker {WORKER_ID} [{role.upper()}]: Timeout waiting for message from {peer_address_info}.")
    except ConnectionResetError:
        logger.warning(f"Worker {WORKER_ID} [{role.upper()}]: Connection reset by {peer_address_info}.")
    except Exception:
        logger.exception(f"Worker {WORKER_ID} [{role.upper()}]: Error in P2P communication with {peer_address_info}.")
    finally:
        logger.info(f"Worker {WORKER_ID} [{role.upper()}]: Closing P2P connection with {peer_address_info}.")
        if not writer.is_closing():
            writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            logger.exception(f"Worker {WORKER_ID} [{role.upper()}]: Error during writer.wait_closed() for {peer_address_info}.")
        STOP_EVENT.set() # Signal stop after one P2P interaction (success or failure)


async def tcp_listener_task(ready_event: asyncio.Event) -> None:
    server: Optional[asyncio.AbstractServer] = None
    # --- Manually create socket for SO_REUSEPORT --- 
    listener_socket: Optional[socket.socket] = None
    try:
        listener_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # SO_REUSEPORT allows multiple sockets to bind to the same IP address and port.
        # This is crucial if we want our outgoing P2P connection to also originate
        # from INTERNAL_TCP_PORT while this listener is active.
        if hasattr(socket, 'SO_REUSEPORT'):
            try:
                listener_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                logger.info(f"Worker {WORKER_ID} [TCP Server]: SO_REUSEPORT set on listener socket.")
            except OSError as e_reuseport:
                # Some systems might not support SO_REUSEPORT even if defined (e.g. Windows)
                logger.warning(f"Worker {WORKER_ID} [TCP Server]: Failed to set SO_REUSEPORT: {e_reuseport}. TCP P2P from same port might fail.")
        else:
            logger.warning(f"Worker {WORKER_ID} [TCP Server]: SO_REUSEPORT not available on this system. TCP P2P from same port might fail.")
        
        try:
            listener_socket.bind(("0.0.0.0", INTERNAL_TCP_PORT))
        except OSError as e_bind:
            if e_bind.errno == errno.EADDRINUSE:
                logger.warning(
                    f"Worker {WORKER_ID} [TCP Server]: Port {INTERNAL_TCP_PORT} already in use. "
                    "Proceeding without server socket (client-only mode)."
                )
                ready_event.set()
                return  # Skip starting the listener, stay alive for outbound dial
            else:
                raise
        # The backlog for listen() is handled by asyncio.start_server if not specified via sock.listen() before passing.
        # asyncio.start_server will call listen() on the passed socket if it hasn't been called.

        server = await asyncio.start_server(
            lambda r, w: handle_peer_connection(r, w, "server"),
            sock=listener_socket # Pass the pre-bound and option-set socket
        )
        if server.sockets:
            server_addr = server.sockets[0].getsockname()
            logger.info(f"Worker {WORKER_ID} [TCP Server]: Listening on {server_addr}")
        else:
            logger.critical(f"Worker {WORKER_ID} [TCP Server]: Failed to get socket information. Cannot listen.")
            STOP_EVENT.set() # Critical failure if server doesn't effectively start
            return

        ready_event.set() # Signal that the listener is ready
        async with server:
            await STOP_EVENT.wait() # Keep server running until STOP_EVENT
        logger.info(f"Worker {WORKER_ID} [TCP Server]: Server.serve_forever() exited gracefully.")

    except asyncio.CancelledError:
        logger.info(f"Worker {WORKER_ID} [TCP Server]: Listener task cancelled.")
    except Exception: # Catch any other exception during server setup or run
        logger.exception(f"Worker {WORKER_ID} [TCP Server]: Critical error.")
        STOP_EVENT.set() # Signal stop on critical listener error
    finally:
        if server: # Server object exists
            logger.info(f"Worker {WORKER_ID} [TCP Server]: Shutting down listener.")
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                logger.exception(f"Worker {WORKER_ID} [TCP Server]: Error during server.wait_closed().")
        elif listener_socket: # Only socket was created, server failed before assignment
            logger.info(f"Worker {WORKER_ID} [TCP Server]: Listener server not fully started, closing raw socket.")
            listener_socket.close()
        else:
            logger.info(f"Worker {WORKER_ID} [TCP Server]: Listener was not started or already stopped.")


async def attempt_p2p_tcp_connect(peer_public_ip: str, peer_port_to_use: int) -> None: # Renamed peer_internal_tcp_port to peer_port_to_use
    logger.info(
        f"Worker {WORKER_ID} [TCP Client]: Attempting to connect to peer {peer_public_ip}:{peer_port_to_use} "
        f"from local port {INTERNAL_TCP_PORT}"
    )

    reader: Optional[asyncio.StreamReader] = None
    writer: Optional[asyncio.StreamWriter] = None
    loop = asyncio.get_running_loop()

    try:
        # Create a custom client socket bound to INTERNAL_TCP_PORT with SO_REUSEPORT so
        # it can coexist with the listener.
        client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError as e_rp:
                logger.debug(f"SO_REUSEPORT not usable on P2P client socket: {e_rp}")

        client_sock.bind(("0.0.0.0", INTERNAL_TCP_PORT))
        client_sock.setblocking(False)

        await asyncio.wait_for(
            loop.sock_connect(client_sock, (peer_public_ip, peer_port_to_use)),
            timeout=60.0,
        )

        reader, writer = await asyncio.open_connection(sock=client_sock)

        await handle_peer_connection(reader, writer, "client")

    except asyncio.TimeoutError:
        logger.warning(
            f"Worker {WORKER_ID} [TCP Client]: Timeout connecting to {peer_public_ip}:{peer_port_to_use}."
        )
    except OSError as e:
        logger.warning(
            f"Worker {WORKER_ID} [TCP Client]: OSError connecting to {peer_public_ip}:{peer_port_to_use} "
            f"(from local port {INTERNAL_TCP_PORT}): {e}"
        )
    except Exception:
        logger.exception(
            f"Worker {WORKER_ID} [TCP Client]: Failed to connect to {peer_public_ip}:{peer_port_to_use}."
        )


async def discover_true_mapped_port(
    local_bind_ip: str,
    local_bind_port: int,
    retry_attempts: int = 3, # Added retry logic
    retry_delay_seconds: float = 8.0 # Increased delay to allow VM echo container to start
) -> Optional[Dict[str, Any]]:
    """
    Discovers the true public-facing (NAT mapped) IP and port for an outbound connection
    originating from local_bind_ip:local_bind_port.
    Uses a simple, truly external TCP echo server that reports the client's source IP/port.
    Returns a dictionary like {"ip": "NAT_PUBLIC_IP", "port": MAPPED_PUBLIC_PORT} or None.
    """
    if not EXTERNAL_ECHO_SERVER_HOST:
        logger.error(f"Worker {WORKER_ID}: EXTERNAL_ECHO_SERVER_HOST not configured. Skipping true mapped port discovery.")
        return None

    logger.info(
        f"Worker {WORKER_ID}: Discovering true mapped port via external echo server "
        f"{EXTERNAL_ECHO_SERVER_HOST}:{EXTERNAL_ECHO_SERVER_PORT} from {local_bind_ip}:{local_bind_port}"
    )
    
    for attempt in range(retry_attempts):
        reader = None
        writer = None
        loop = asyncio.get_running_loop()
        response_str = "<not_received>" # For logging in case of decode error
        try:
            logger.debug(
                f"Attempt {attempt + 1}/{retry_attempts}: Opening connection to {EXTERNAL_ECHO_SERVER_HOST}:{EXTERNAL_ECHO_SERVER_PORT} "
                f"from local {local_bind_ip}:{local_bind_port} with custom socket"
            )

            # --- Create client socket manually so we can set SO_REUSEPORT ---
            client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    client_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError as e_rp:
                    logger.debug(f"SO_REUSEPORT not usable on client socket: {e_rp}")
            client_sock.bind((local_bind_ip, local_bind_port))
            client_sock.setblocking(False)

            await asyncio.wait_for(
                loop.sock_connect(client_sock, (EXTERNAL_ECHO_SERVER_HOST, EXTERNAL_ECHO_SERVER_PORT)),
                timeout=ECHO_CONNECT_TIMEOUT,
            )

            reader, writer = await asyncio.open_connection(sock=client_sock)
            logger.debug(f"Connection established with external echo server.")

            # Echo server expects a newline to trigger response, or responds immediately.
            # Let's send a newline to be sure, as per original echo server design.
            writer.write(b"\n") 
            await writer.drain()
            logger.debug(f"Sent newline to external echo server.")

            response_bytes = await asyncio.wait_for(reader.readuntil(b'\n'), timeout=ECHO_READ_TIMEOUT)
            response_str = response_bytes.decode().strip()
            logger.debug(f"Worker {WORKER_ID}: Received from external echo server: '{response_str}'")
            
            data = json.loads(response_str)
            mapped_port_raw = data.get("mapped_public_port")
            nat_public_ip_for_this_connection = data.get("public_ip")

            if mapped_port_raw is not None and nat_public_ip_for_this_connection is not None:
                mapped_port = int(mapped_port_raw)
                logger.info(
                    f"Worker {WORKER_ID}: True Mapped Public Port (from external echo): {mapped_port} "
                    f"on NAT Public IP: {nat_public_ip_for_this_connection} (Attempt {attempt + 1}/{retry_attempts})"
                )
                return {"ip": str(nat_public_ip_for_this_connection), "port": mapped_port}
            else:
                logger.warning(f"Worker {WORKER_ID}: Incomplete data from external echo server attempt {attempt + 1}/{retry_attempts}: {data}. Retrying...")
            
        except json.JSONDecodeError:
            logger.warning(f"Worker {WORKER_ID}: Failed to decode JSON from external echo server (data: '{response_str}') attempt {attempt + 1}/{retry_attempts}. Retrying...")
        except asyncio.TimeoutError:
            logger.warning(f"Worker {WORKER_ID}: Timeout during echo discovery to {EXTERNAL_ECHO_SERVER_HOST}:{EXTERNAL_ECHO_SERVER_PORT} attempt {attempt + 1}/{retry_attempts}. Retrying...")
        except OSError as e_os:
            logger.warning(
                f"Worker {WORKER_ID}: OSError during echo discovery to {EXTERNAL_ECHO_SERVER_HOST}:{EXTERNAL_ECHO_SERVER_PORT} "
                f"(from {local_bind_ip}:{local_bind_port}), attempt {attempt + 1}/{retry_attempts}: {e_os}. Retrying..."
            )
        except Exception:
            logger.exception(f"Worker {WORKER_ID}: Failed to discover mapped port from external echo server, attempt {attempt + 1}/{retry_attempts}. Retrying...")
        finally:
            if writer:
                if not writer.is_closing():
                    # logger.debug("Closing writer to external echo server.") # Too verbose for normal operation
                    writer.close()
                try:
                    await writer.wait_closed()
                    # logger.debug("Writer to external echo server closed.") # Too verbose
                except Exception:
                    logger.exception(f"Error closing writer to external echo server on attempt {attempt + 1}.")
        
        if attempt < retry_attempts - 1:
            await asyncio.sleep(retry_delay_seconds)
        else:
            logger.error(
                f"Worker {WORKER_ID}: Failed to discover mapped port via echo server after {retry_attempts} attempts."
            )
            return None
    return None # Should be unreachable


# NEW Test Function
def test_internal_vs_external_ip_theory(rendezvous_http_url: str):
    # This function is synchronous and will be run in an executor.
    logger.info(f"Worker {WORKER_ID}: === Starting Internal vs. External IP Discovery Test ===")
    public_ip_from_ipify: Optional[str] = None
    try:
        logger.info(f"Worker {WORKER_ID}: Fetching public IP from ipify.org...")
        response = requests.get("https://api.ipify.org", timeout=10)
        response.raise_for_status()
        public_ip_from_ipify = response.text.strip()
        logger.info(f"Worker {WORKER_ID}: IP from ipify.org (External View): {public_ip_from_ipify}")
    except requests.RequestException as e:
        logger.error(f"Worker {WORKER_ID}: Could not get IP from ipify.org: {e}")
        logger.info(f"Worker {WORKER_ID}: === IP Discovery Test Ended (ipify failed) ===")
        return

    if not rendezvous_http_url:
        logger.error(f"Worker {WORKER_ID}: Rendezvous HTTP URL not provided for discovery test.")
        logger.info(f"Worker {WORKER_ID}: === IP Discovery Test Ended (no rendezvous URL) ===")
        return

    discovery_target_url = f"{rendezvous_http_url.rstrip('/')}/discover_tcp_port"
    ip_seen_by_rendezvous: Optional[str] = None
    data_from_rendezvous = "<not received>"
    try:
        logger.info(f"Worker {WORKER_ID}: Calling Rendezvous' /discover_tcp_port at {discovery_target_url}...")
        response = requests.get(discovery_target_url, timeout=15)
        response.raise_for_status()
        data_from_rendezvous = response.json()
        ip_seen_by_rendezvous = data_from_rendezvous.get("public_ip_seen_by_rendezvous")
        port_seen_by_rendezvous = data_from_rendezvous.get("port_seen_by_rendezvous")
        logger.info(
            f"Worker {WORKER_ID}: IP:Port seen by Rendezvous service (Internal/Optimized Path View): "
            f"{ip_seen_by_rendezvous}:{port_seen_by_rendezvous}"
        )
    except requests.RequestException as e:
        logger.error(f"Worker {WORKER_ID}: Could not get IP from Rendezvous /discover_tcp_port: {e} (Details: {response.text if 'response' in locals() else 'N/A'})")
    except json.JSONDecodeError as e:
        logger.error(f"Worker {WORKER_ID}: Could not parse JSON from Rendezvous /discover_tcp_port: {e} (Data: {data_from_rendezvous})")
    except KeyError as e:
        logger.error(f"Worker {WORKER_ID}: Unexpected JSON structure from Rendezvous /discover_tcp_port (missing key {e}): {data_from_rendezvous}")

    if public_ip_from_ipify and ip_seen_by_rendezvous:
        if public_ip_from_ipify == ip_seen_by_rendezvous:
            logger.info(
                f"Worker {WORKER_ID}: THEORY NOT SUPPORTED (or complex routing): "
                f"IP from ipify ({public_ip_from_ipify}) MATCHES IP seen by Rendezvous "
                f"({ip_seen_by_rendezvous})."
            )
        else:
            logger.info(
                f"Worker {WORKER_ID}: === THEORY SUPPORTED === "
                f"IP from ipify ({public_ip_from_ipify}) DOES NOT MATCH IP seen by Rendezvous "
                f"({ip_seen_by_rendezvous}). This suggests internal routing."
            )
    elif public_ip_from_ipify and not ip_seen_by_rendezvous:
        logger.warning(f"Worker {WORKER_ID}: Could not determine IP seen by Rendezvous. Test inconclusive.")
    else:
        logger.warning(f"Worker {WORKER_ID}: Test inconclusive as external IP from ipify was not obtained.")
    logger.info(f"Worker {WORKER_ID}: === IP Discovery Test Finished ===")

async def rendezvous_client_and_p2p_manager() -> None:
    """Manages WebSocket connection to rendezvous, NAT discovery, and P2P lifecycle."""
    if not RENDEZVOUS_URL:
        logger.critical("RENDEZVOUS_SERVICE_URL is not set. Cannot connect to rendezvous.")
        STOP_EVENT.set()
        return

    # ------------------------------------------------------------------
    # Step 1.  NAT Mapping Discovery via External Echo Server
    # ------------------------------------------------------------------
    # We MUST run this *before* we start our local TCP listener so that the
    # source port (INTERNAL_TCP_PORT) is free for the outbound socket.  When
    # we previously started the listener first, the outbound dial failed with
    # "address already in use" / "address family for hostname not supported"
    # because Cloud Run (gVisor) does not allow another socket to bind to the
    # same IP:PORT even with SO_REUSEPORT.
    logger.info(
        f"Worker {WORKER_ID}: Attempting external echo server discovery for local port {INTERNAL_TCP_PORT}"
    )

    mapped_port_info: Optional[Dict[str, Any]] = await discover_true_mapped_port(
        local_bind_ip="0.0.0.0",
        local_bind_port=INTERNAL_TCP_PORT,
    )

    # Give the OS a brief moment to release the port from the client socket's
    # TIME_WAIT state before we try to re-bind it as a server.
    await asyncio.sleep(0.5)

    # ------------------------------------------------------------------
    # Step 2.  Start the TCP listener (now that the discovery socket is
    #          closed and the port is free again).
    # ------------------------------------------------------------------
    listener_ready_event = asyncio.Event()
    listener_task = asyncio.create_task(tcp_listener_task(listener_ready_event))

    if mapped_port_info:
        mapped_public_port_for_listener = mapped_port_info.get("port")
        discovered_public_ip = mapped_port_info.get("ip") # IP as seen by the echo server
        logger.info(f"Worker {WORKER_ID}: Discovered public IP:Port {discovered_public_ip}:{mapped_public_port_for_listener} for internal port {INTERNAL_TCP_PORT} via echo server.")
    else:
        logger.error(
            f"Worker {WORKER_ID}: Failed to discover mapped public port using external echo server for internal port {INTERNAL_TCP_PORT}. "
            "Proceeding without it, but TCP hole punching may fail."
        )
        mapped_public_port_for_listener = None
        discovered_public_ip = None # Ensure this is also None if discovery fails

    # Wait for the TCP listener to be ready before trying to connect to rendezvous
    try:
        logger.info(f"Worker {WORKER_ID}: Waiting for TCP listener on port {INTERNAL_TCP_PORT} to be ready...")
        await asyncio.wait_for(listener_ready_event.wait(), timeout=LISTENER_READY_TIMEOUT)
        logger.info(f"Worker {WORKER_ID}: TCP listener ready.")
    except asyncio.TimeoutError:
        logger.critical(f"Worker {WORKER_ID}: TCP listener on port {INTERNAL_TCP_PORT} did not become ready in time. Shutting down.")
        STOP_EVENT.set()
        # Ensure listener_task is cancelled if it's stuck
        if not listener_task.done():
            listener_task.cancel()
        try:
            await listener_task # Allow cancellation to propagate
        except asyncio.CancelledError:
            logger.info(f"Worker {WORKER_ID}: Listener task successfully cancelled after timeout.")
        return # Stop further execution

    # --- Connection to Rendezvous Server ---
    # Construct the WebSocket URL, ensuring ws:// or wss://
    ws_scheme = "wss" if RENDEZVOUS_URL.startswith("https://") else "ws"
    # Correctly extract base_url_no_scheme for ws/wss URL construction
    if "://" in RENDEZVOUS_URL:
        base_url_no_scheme = RENDEZVOUS_URL.split("://", 1)[1]
    else:
        base_url_no_scheme = RENDEZVOUS_URL # Assume it might be just host:port or host
    full_rendezvous_ws_url = f"{ws_scheme}://{base_url_no_scheme.rstrip('/')}/ws/register_tcp/{WORKER_ID}"

    try:
        logger.info(f"Worker {WORKER_ID}: Connecting to Rendezvous server at {full_rendezvous_ws_url}")
        ssl_ctx = None
        if full_rendezvous_ws_url.startswith("wss://"):
            import ssl
            ssl_ctx = ssl.create_default_context()
            try: ssl_ctx.set_alpn_protocols(["http/1.1"])
            except NotImplementedError: pass

        async with websockets.connect(
            full_rendezvous_ws_url, ping_interval=20, open_timeout=WS_OPEN_TIMEOUT, ssl=ssl_ctx
        ) as websocket:
            logger.info(f"Worker {WORKER_ID}: Connected to Rendezvous. Sending TCP info...")
            await websocket.send(json.dumps({
                "type": "report_tcp_info",
                "public_ip": discovered_public_ip,
                "internal_tcp_port": INTERNAL_TCP_PORT,
                "mapped_public_port": mapped_public_port_for_listener
            }))

            logger.info(f"Worker {WORKER_ID}: Waiting for peer info from Rendezvous (timeout: {WS_RECV_TIMEOUT}s)...")
            message_raw: str = await asyncio.wait_for(websocket.recv(), timeout=WS_RECV_TIMEOUT)
            message_data = json.loads(message_raw)

            if message_data.get("type") == "p2p_tcp_peer_info":
                peer_id = message_data.get("peer_worker_id")
                peer_public_ip = message_data.get("peer_public_ip")
                peer_internal_tcp_port_str = message_data.get("peer_internal_tcp_port")
                peer_mapped_public_port_str = message_data.get("peer_mapped_public_port")

                if not (peer_id and peer_public_ip and (peer_internal_tcp_port_str is not None or peer_mapped_public_port_str is not None)):
                    logger.error(f"Worker {WORKER_ID}: Incomplete peer info from Rendezvous: {message_data}")
                    return
                try:
                    peer_port_to_use: Optional[int] = None
                    if peer_mapped_public_port_str is not None:
                        peer_port_to_use = int(peer_mapped_public_port_str)
                        logger.info(f"Worker {WORKER_ID}: Using peer's mapped_public_port={peer_port_to_use}.")
                    elif peer_internal_tcp_port_str is not None: # Fallback if mapped is not available
                        peer_port_to_use = int(peer_internal_tcp_port_str)
                        logger.info(f"Worker {WORKER_ID}: Peer mapped port not available, using peer's internal_tcp_port={peer_port_to_use}.")
                    else:
                        logger.error(f"Worker {WORKER_ID}: No valid port (mapped or internal) received for peer {peer_id}.")
                        return
                        
                    logger.info(
                        f"Worker {WORKER_ID}: Received peer info: ID={peer_id}, "
                        f"Target IP={peer_public_ip}, Target Port={peer_port_to_use}. "
                        f"Will attempt up to {P2P_CONNECT_MAX_RETRIES} TCP dials (delay {P2P_RETRY_DELAY}s)."
                    )

                    for attempt_idx in range(1, P2P_CONNECT_MAX_RETRIES + 1):
                        if STOP_EVENT.is_set():
                            logger.info(
                                f"Worker {WORKER_ID}: STOP_EVENT set before P2P dial attempt {attempt_idx}; "
                                "aborting further connect attempts."
                            )
                            break

                        logger.info(
                            f"Worker {WORKER_ID}: ---- P2P dial attempt {attempt_idx}/{P2P_CONNECT_MAX_RETRIES} ----"
                        )
                        await attempt_p2p_tcp_connect(str(peer_public_ip), peer_port_to_use)

                        if STOP_EVENT.is_set():
                            # A successful connection (handle_peer_connection sets STOP_EVENT) – break early.
                            logger.info(
                                f"Worker {WORKER_ID}: P2P connection succeeded on attempt {attempt_idx}. "
                                "No further dials required."
                            )
                            break

                        if attempt_idx < P2P_CONNECT_MAX_RETRIES:
                            logger.info(
                                f"Worker {WORKER_ID}: P2P dial attempt {attempt_idx} did not succeed. "
                                f"Sleeping {P2P_RETRY_DELAY}s before next attempt."
                            )
                            await asyncio.sleep(P2P_RETRY_DELAY)
                        else:
                            logger.error(
                                f"Worker {WORKER_ID}: Exhausted {P2P_CONNECT_MAX_RETRIES} P2P dial attempts without success."
                            )
                            # Set STOP_EVENT so that the rest of the cleanup logic triggers gracefully.
                            STOP_EVENT.set()
                except ValueError:
                    logger.error(f"Worker {WORKER_ID}: Invalid peer port format from rendezvous. Mapped: '{peer_mapped_public_port_str}', Internal: '{peer_internal_tcp_port_str}'")
            else:
                logger.warning(f"Worker {WORKER_ID}: Received unexpected message from Rendezvous: {message_data}")

    except websockets.exceptions.InvalidURI:
        logger.critical(f"Worker {WORKER_ID}: Invalid Rendezvous URI: {full_rendezvous_ws_url}. Exiting.", exc_info=True)
        STOP_EVENT.set()
    except websockets.exceptions.InvalidMessage as im_err: # Handle potential HTTP/2 issues with ALPN
        if full_rendezvous_ws_url.startswith("wss://"):
            fallback_url = full_rendezvous_ws_url.replace("wss://", "ws://", 1)
            logger.warning(f"Worker {WORKER_ID}: InvalidMessage (likely HTTP/2). Retrying with plain ws:// {fallback_url}")
            try:
                async with websockets.connect(fallback_url, ping_interval=20, open_timeout=WS_OPEN_TIMEOUT) as websocket_fallback:
                    logger.info(f"Worker {WORKER_ID}: Connected (fallback ws). Sending TCP info...")
                    await websocket_fallback.send(json.dumps({
                        "type": "report_tcp_info", 
                        "public_ip": discovered_public_ip, 
                        "internal_tcp_port": INTERNAL_TCP_PORT, 
                        "mapped_public_port": mapped_public_port_for_listener
                    }))
                    message_raw_fallback: str = await asyncio.wait_for(websocket_fallback.recv(), timeout=WS_RECV_TIMEOUT)
                    message_data_fallback = json.loads(message_raw_fallback)
                    # (Re-paste peer handling logic here, or refactor to a common function)
                    if message_data_fallback.get("type") == "p2p_tcp_peer_info":
                        peer_id_fb = message_data_fallback.get("peer_worker_id")
                        peer_public_ip_fb = message_data_fallback.get("peer_public_ip")
                        peer_internal_tcp_port_str_fb = message_data_fallback.get("peer_internal_tcp_port")
                        peer_mapped_public_port_str_fb = message_data_fallback.get("peer_mapped_public_port")
                        if not (peer_id_fb and peer_public_ip_fb and (peer_internal_tcp_port_str_fb is not None or peer_mapped_public_port_str_fb is not None)):
                            logger.error(f"Worker {WORKER_ID}: Incomplete peer info (fallback): {message_data_fallback}")
                            return
                        try:
                            peer_port_to_use_fb: Optional[int] = None
                            if peer_mapped_public_port_str_fb is not None:
                                peer_port_to_use_fb = int(peer_mapped_public_port_str_fb)
                            elif peer_internal_tcp_port_str_fb is not None:
                                peer_port_to_use_fb = int(peer_internal_tcp_port_str_fb)
                            if peer_port_to_use_fb is not None:
                                 logger.info(f"Worker {WORKER_ID}: Received peer info (fallback): ID={peer_id_fb}, Target IP={peer_public_ip_fb}, Target Port={peer_port_to_use_fb}")
                                 await attempt_p2p_tcp_connect(str(peer_public_ip_fb), peer_port_to_use_fb)
                            else: logger.error(f"Worker {WORKER_ID}: No valid port for peer (fallback): {peer_id_fb}")
                        except ValueError: logger.error(f"Worker {WORKER_ID}: Invalid peer port (fallback). Mapped: '{peer_mapped_public_port_str_fb}', Internal: '{peer_internal_tcp_port_str_fb}'")
                    else: logger.warning(f"Worker {WORKER_ID}: Unexpected message (fallback): {message_data_fallback}")
            except Exception as fallback_err:
                logger.critical(f"Worker {WORKER_ID}: Fallback ws:// handshake also failed: {fallback_err}. Exiting.", exc_info=True)
                STOP_EVENT.set()
        else: # Was not wss:// to begin with, or other InvalidMessage issue
            logger.critical(f"Worker {WORKER_ID}: WebSocket InvalidMessage: {im_err}. Exiting.", exc_info=True)
            STOP_EVENT.set()
    except (websockets.exceptions.WebSocketException, ConnectionRefusedError) as e:
        logger.critical(f"Worker {WORKER_ID}: WebSocket connection error with Rendezvous ({type(e).__name__}): {e}. Exiting.", exc_info=True)
        STOP_EVENT.set()
    except asyncio.TimeoutError:
        logger.critical(f"Worker {WORKER_ID}: Timeout during rendezvous communication. Exiting.", exc_info=True)
        STOP_EVENT.set()
    except json.JSONDecodeError:
        logger.critical(f"Worker {WORKER_ID}: Could not decode JSON from Rendezvous message. Exiting.", exc_info=True)
        STOP_EVENT.set()
    except Exception:
        logger.critical(f"Worker {WORKER_ID}: Error in Rendezvous client or P2P management. Exiting.", exc_info=True)
        STOP_EVENT.set()
    finally:
        logger.info(f"Worker {WORKER_ID}: Rendezvous client and P2P manager task finished.")
        if not STOP_EVENT.is_set():
            logger.info(f"Worker {WORKER_ID}: Main P2P/Rendezvous logic finished. Setting STOP_EVENT.")
            STOP_EVENT.set()
        if listener_task and not listener_task.done():
            logger.info(f"Worker {WORKER_ID}: Ensuring listener task ({listener_task.get_name()}) is handled.")
            try:
                await asyncio.wait_for(listener_task, timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning(f"Worker {WORKER_ID}: Timeout waiting for listener task. Cancelling.")
                listener_task.cancel()
                try: await listener_task
                except asyncio.CancelledError: pass
            except Exception as e_lt_clean:
                logger.error(f"Worker {WORKER_ID}: Error awaiting listener task in cleanup: {e_lt_clean}")

def health_check_server_thread_func() -> None:
    class SimpleHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == '/': # Default health check path for Cloud Run
                self.send_response(200)
                self.send_header("Content-type", "text/plain")
                self.end_headers()
                self.wfile.write(b"OK")
            else:
                self.send_error(404, "File Not Found")
        def log_message(self, format_str: str, *args: Any) -> None:
            return
    server_address = ("0.0.0.0", APP_PORT)
    logger.info(f"Worker {WORKER_ID}: Health check HTTP server starting on 0.0.0.0:{APP_PORT}")
    try:
        with http.server.ThreadingHTTPServer(server_address, SimpleHTTPRequestHandler) as httpd:
            logger.info(f"Worker {WORKER_ID}: Health check HTTP server listening on 0.0.0.0:{APP_PORT}")
            httpd.serve_forever()
    except Exception:
        logger.critical(f"Worker {WORKER_ID}: Health check server failed to start or crashed.", exc_info=True)
    logger.info(f"Worker {WORKER_ID}: Health check HTTP server thread finished.")


async def graceful_shutdown(loop: asyncio.AbstractEventLoop, tasks_to_cancel: List[asyncio.Task]) -> None:
    logger.info(f"Worker {WORKER_ID}: Initiating graceful shutdown (simplified)...")

    for task in tasks_to_cancel:
        if task and not task.done():
            logger.info(f"Worker {WORKER_ID}: Cancelling task {task.get_name()} (from tasks_to_cancel)...")
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0) 
            except asyncio.CancelledError:
                logger.info(f"Task {task.get_name()} successfully cancelled.")
            except asyncio.TimeoutError:
                logger.warning(f"Timeout waiting for task {task.get_name()} to cancel.")
            except Exception as e:
                logger.error(f"Error during task {task.get_name()} cancellation: {e}")

    remaining_tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task(loop)]
    if remaining_tasks:
        logger.info(f"Worker {WORKER_ID}: Cleaning up {len(remaining_tasks)} other asyncio tasks...")
        for task in remaining_tasks:
            if not task.done():
                task.cancel()
        
        results = await asyncio.gather(*remaining_tasks, return_exceptions=True)
        for i, res in enumerate(results):
            task_name = "UnknownTask"
            try: task_name = remaining_tasks[i].get_name()
            except AttributeError: pass # Some internal tasks might not have a name
            if isinstance(res, asyncio.CancelledError):
                logger.debug(f"Task {task_name} (from all_tasks) was cancelled.")
            elif isinstance(res, Exception):
                logger.warning(f"Task {task_name} (from all_tasks) raised an exception during gather: {res}")
        
    logger.info(f"Worker {WORKER_ID}: Graceful shutdown: task processing complete.")
    # loop.close() and other loop shutdown methods are omitted here,
    # relying on the main run_until_complete to finalize loop state upon exit.


if __name__ == "__main__":
    logger.info(
        f"Minimal TCP P2P Test Worker {WORKER_ID} starting... "
        f"INTERNAL_TCP_PORT={INTERNAL_TCP_PORT}, APP_PORT (Health Check)={APP_PORT}"
    )

    # Start health check server in a daemon thread
    health_thread = threading.Thread(target=health_check_server_thread_func, daemon=True)
    health_thread.name = "HealthCheckThread"
    health_thread.start()

    event_loop = asyncio.get_event_loop()

    main_task: Optional[asyncio.Task] = None
    # listener_task handle is now primarily within rendezvous_client_and_p2p_manager

    def signal_handler(sig: signal.Signals) -> None:
        logger.warning(f"Worker {WORKER_ID}: Received signal {sig.name}. Initiating shutdown via STOP_EVENT.")
        STOP_EVENT.set()

    if os.name == "posix":
        for sig_name in ('SIGINT', 'SIGTERM'):
            sig = getattr(signal, sig_name)
            try:
                event_loop.add_signal_handler(sig, signal_handler, sig)
            except (ValueError, OSError) as e: # ValueError if loop is closed, OSError if in non-main thread sometimes
                 logger.warning(f"Could not set signal handler for {sig_name}: {e}")
    else: # For Windows
        try:
            signal.signal(signal.SIGINT, lambda s,f : signal_handler(signal.Signals(s)))
        except (ValueError, AttributeError) as e:
            logger.warning(f"Could not set SIGINT handler for Windows: {e}")

    try:
        main_task = event_loop.create_task(rendezvous_client_and_p2p_manager(), name="RendezvousP2PManager")
        event_loop.run_until_complete(STOP_EVENT.wait())
        logger.info(f"Worker {WORKER_ID}: STOP_EVENT was set, main wait completed.")

    except KeyboardInterrupt:
        logger.info(f"Worker {WORKER_ID}: KeyboardInterrupt caught in main. Setting STOP_EVENT.")
        if not STOP_EVENT.is_set(): STOP_EVENT.set()
    except SystemExit as e:
        logger.info(f"Worker {WORKER_ID}: SystemExit caught: {e}. Setting STOP_EVENT.")
        if not STOP_EVENT.is_set(): STOP_EVENT.set()
    except Exception as e:
        logger.critical(f"Worker {WORKER_ID}: Unhandled exception in main event loop execution: {e}", exc_info=True)
        if not STOP_EVENT.is_set(): STOP_EVENT.set()
    finally:
        logger.info(f"Worker {WORKER_ID}: Main execution finished or interrupted. Running graceful shutdown.")
        
        # Prepare tasks for graceful_shutdown. 
        # main_task should be the primary one here.
        # Other tasks (like listener_task) are managed and awaited within rendezvous_client_and_p2p_manager's finally block or by graceful_shutdown's all_tasks sweep.
        tasks_to_clean_up = []
        if main_task and not main_task.done():
            tasks_to_clean_up.append(main_task)
        
        # It's crucial that graceful_shutdown is the last thing run by run_until_complete on this loop.
        if not event_loop.is_closed():
            event_loop.run_until_complete(graceful_shutdown(event_loop, tasks_to_clean_up))
        else:
            logger.warning(f"Worker {WORKER_ID}: Event loop was already closed before final graceful_shutdown call.")
        
        logger.info(f"Worker {WORKER_ID}: Program exiting.") 