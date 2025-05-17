#!/usr/bin/env python3
"""Cloud-Run worker that attempts simultaneous-open TCP hole punching (2025-ready)."""
import asyncio
import http.server
import json
import logging # <<< Added
import os
import signal
import sys # Added for sys.version_info
import threading
import uuid
from typing import Any, Dict, Optional, List # Added List for type hinting

import requests # type: ignore
import websockets # type: ignore
import aiohttp # NEW: For async HTTP requests

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
EXTERNAL_ECHO_SERVER_HOST = os.getenv("EXTERNAL_ECHO_SERVER_HOST") # NEW
EXTERNAL_ECHO_SERVER_PORT = int(os.getenv("EXTERNAL_ECHO_SERVER_PORT", "12345")) # NEW
ECHO_CONNECT_TIMEOUT = float(os.getenv("ECHO_CONNECT_TIMEOUT", "20.0")) # NEW
ECHO_READ_TIMEOUT = float(os.getenv("ECHO_READ_TIMEOUT", "20.0")) # NEW


STOP_EVENT = asyncio.Event()

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
    try:
        server = await asyncio.start_server(
            lambda r, w: handle_peer_connection(r, w, "server"),
            "0.0.0.0",
            INTERNAL_TCP_PORT
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
        if server:
            logger.info(f"Worker {WORKER_ID} [TCP Server]: Shutting down listener.")
            server.close()
            try:
                await server.wait_closed()
            except Exception: # Handle potential errors during server close
                logger.exception(f"Worker {WORKER_ID} [TCP Server]: Error during server.wait_closed().")
        else:
            logger.info(f"Worker {WORKER_ID} [TCP Server]: Listener was not started or already stopped.")


async def attempt_p2p_tcp_connect(peer_public_ip: str, peer_internal_tcp_port: int) -> None:
    logger.info(
        f"Worker {WORKER_ID} [TCP Client]: Attempting to connect to peer {peer_public_ip}:{peer_internal_tcp_port} "
        f"from local port {INTERNAL_TCP_PORT}"
    )
    reader: Optional[asyncio.StreamReader] = None
    writer: Optional[asyncio.StreamWriter] = None
    try:
        # Crucial part: binding the source port for the outgoing connection
        # Timeout for the connection attempt itself can be managed by asyncio.wait_for if needed
        # asyncio.open_connection itself doesn't have a direct timeout param like requests.get
        reader, writer = await asyncio.wait_for(asyncio.open_connection(
            host=peer_public_ip,
            port=peer_internal_tcp_port
            # local_addr=('0.0.0.0', INTERNAL_TCP_PORT) # REMOVED: Let OS pick source port to avoid Errno 98
        ), timeout=60.0) # Increased timeout to 60 seconds

        await handle_peer_connection(reader, writer, "client")
    except asyncio.TimeoutError:
        logger.warning(f"Worker {WORKER_ID} [TCP Client]: Timeout connecting to {peer_public_ip}:{peer_internal_tcp_port}.")
    except OSError as e:
        logger.warning(
            f"Worker {WORKER_ID} [TCP Client]: OSError connecting to {peer_public_ip}:{peer_internal_tcp_port} "
            f"(from local port {INTERNAL_TCP_PORT}): {e}" # Note: INTERNAL_TCP_PORT is still in log, but not used for bind
        )
    except Exception:
        logger.exception(f"Worker {WORKER_ID} [TCP Client]: Failed to connect to {peer_public_ip}:{peer_internal_tcp_port}.")
    # No finally block here to close writer; handle_peer_connection does that.
    # If open_connection fails, writer is None.
    # STOP_EVENT is set by handle_peer_connection. If connect fails, listener continues for a while.


async def discover_true_mapped_port(
    local_bind_ip: str,
    local_bind_port: int
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
    reader = None
    writer = None
    try:
        # Connect to the external echo server from the specific internal port
        logger.debug(f"Attempting to open connection to {EXTERNAL_ECHO_SERVER_HOST}:{EXTERNAL_ECHO_SERVER_PORT} local_addr=({local_bind_ip}, {local_bind_port})")
        reader, writer = await asyncio.wait_for(asyncio.open_connection(
            host=EXTERNAL_ECHO_SERVER_HOST,
            port=EXTERNAL_ECHO_SERVER_PORT,
            local_addr=(local_bind_ip, local_bind_port)
        ), timeout=ECHO_CONNECT_TIMEOUT)
        logger.debug(f"Connection established with external echo server.")

        # Send a newline to prompt a response from simple echo servers
        writer.write(b"\\n")
        await writer.drain()
        logger.debug(f"Sent data to external echo server.")

        # Read the JSON response (e.g., up to a newline or specific byte count)
        # Assuming the echo server sends a single line of JSON terminated by newline
        response_bytes = await asyncio.wait_for(reader.readuntil(b'\\n'), timeout=ECHO_READ_TIMEOUT)
        response_str = response_bytes.decode().strip()
        logger.debug(f"Worker {WORKER_ID}: Received from external echo server: '{response_str}'")
        
        data = json.loads(response_str)
        mapped_port_raw = data.get("mapped_public_port")
        nat_public_ip_for_this_connection = data.get("public_ip") 

        if mapped_port_raw is not None and nat_public_ip_for_this_connection is not None:
            mapped_port = int(mapped_port_raw)
            logger.info(
                f"Worker {WORKER_ID}: True Mapped Public Port (from external echo): {mapped_port} "
                f"on NAT Public IP: {nat_public_ip_for_this_connection}"
            )
            return {"ip": str(nat_public_ip_for_this_connection), "port": mapped_port}
        else:
            logger.error(f"Worker {WORKER_ID}: Incomplete data from external echo server response: {data}")
            return None
    except json.JSONDecodeError:
        logger.exception(f"Worker {WORKER_ID}: Failed to decode JSON from external echo server (data: '{response_str}').")
        return None
    except asyncio.TimeoutError:
        logger.warning(f"Worker {WORKER_ID}: Timeout during STUN-like discovery to {EXTERNAL_ECHO_SERVER_HOST}:{EXTERNAL_ECHO_SERVER_PORT}")
        return None
    except OSError as e_os:
        # Log specific OSError details which can be very helpful (e.g., Errno 98 Address already in use)
        logger.warning(
            f"Worker {WORKER_ID}: OSError during STUN-like discovery to {EXTERNAL_ECHO_SERVER_HOST}:{EXTERNAL_ECHO_SERVER_PORT} "
            f"(from {local_bind_ip}:{local_bind_port}): {e_os}"
        )
        return None
    except Exception:
        logger.exception(f"Worker {WORKER_ID}: Failed to discover mapped port from external echo server.")
        return None
    finally:
        if writer:
            if not writer.is_closing():
                logger.debug("Closing writer to external echo server.")
                writer.close()
            try:
                await writer.wait_closed()
                logger.debug("Writer to external echo server closed.")
            except Exception as e_close:
                 logger.exception(f"Error closing writer to external echo server: {e_close}")


async def rendezvous_client_and_p2p_manager() -> None:
    if not RENDEZVOUS_URL:
        logger.critical(f"Worker {WORKER_ID}: RENDEZVOUS_SERVICE_URL not set. Exiting.")
        STOP_EVENT.set()
        return
    if not RENDEZVOUS_URL.startswith(("http://", "https://")):
        logger.critical(f"Worker {WORKER_ID}: RENDEZVOUS_SERVICE_URL must start with http:// or https://. Value: '{RENDEZVOUS_URL}'. Exiting.")
        STOP_EVENT.set()
        return

    ws_scheme = "wss" if RENDEZVOUS_URL.startswith("https://") else "ws"
    base_url_no_scheme = RENDEZVOUS_URL.split("://", 1)[1]
    full_rendezvous_ws_url = f"{ws_scheme}://{base_url_no_scheme.rstrip('/')}/ws/register_tcp/{WORKER_ID}"

    # --- Public IP Discovery (General NAT IP) ---
    public_ip_from_ipify: Optional[str] = None
    try:
        logger.info(f"Worker {WORKER_ID}: Determining public IP via api.ipify.org...")
        # Using aiohttp for this call as well to keep things async
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.ipify.org", timeout=aiohttp.ClientTimeout(total=10)) as response:
                response.raise_for_status()
                public_ip_from_ipify = (await response.text()).strip()
                logger.info(f"Worker {WORKER_ID}: Public IP (ipify) discovered as {public_ip_from_ipify}")
    except Exception as e: # Catching generic Exception for aiohttp and requests' potential errors
        logger.warning(f"Worker {WORKER_ID}: Could not determine public IP from ipify ({type(e).__name__}): {e}.")
        # If ipify fails, we must exit as we need some public IP reference.
        logger.critical(f"Worker {WORKER_ID}: Failed to get any public IP reference. Exiting.")
        STOP_EVENT.set()
        return

    # --- Discover True Mapped Port via External Echo Server ---
    # This gives the specific NAT IP and Mapped Port for an outbound connection from INTERNAL_TCP_PORT.
    # The IP from echo *should* match ipify_ip, but we prioritize the echo server's view for P2P.
    nat_mapping_info = await discover_true_mapped_port("0.0.0.0", INTERNAL_TCP_PORT)

    p2p_public_ip_to_report: Optional[str] = None
    p2p_mapped_port_to_report: Optional[int] = None

    if nat_mapping_info:
        p2p_public_ip_to_report = nat_mapping_info["ip"]
        p2p_mapped_port_to_report = nat_mapping_info["port"]
        if public_ip_from_ipify and p2p_public_ip_to_report != public_ip_from_ipify:
            logger.warning(
                f"Worker {WORKER_ID}: NAT Public IP from external echo ({p2p_public_ip_to_report}) "
                f"differs from ipify's ({public_ip_from_ipify}). Using echo server's IP for P2P info."
            )
    else:
        logger.error(
            f"Worker {WORKER_ID}: Failed to discover true NAT-mapped public IP/port via external echo. "
            "Using ipify IP and INTERNAL_TCP_PORT as fallback for rendezvous report (P2P likely to fail)."
        )
        p2p_public_ip_to_report = public_ip_from_ipify # Fallback to general NAT IP
        # Fallback to reporting internal port as mapped; less ideal than having no mapped port or a placeholder.
        # Rendezvous server handles `None` for mapped_public_port, worker peer will then use peer_internal_tcp_port.
        p2p_mapped_port_to_report = None # Explicitly None if discovery fails
        logger.warning(
             f"Worker {WORKER_ID}: Reporting to rendezvous: public_ip='{p2p_public_ip_to_report}', mapped_public_port=None (discovery failed)."
        )
        # Consider if P2P should be aborted if true mapping fails. For now, we proceed with fallback.

    listener_ready_event = asyncio.Event()
    # Start the TCP listener task. It will set STOP_EVENT if it fails critically.
    listener_task = asyncio.create_task(tcp_listener_task(listener_ready_event), name="TCPListenerTask")

    logger.info(f"Worker {WORKER_ID}: Waiting for TCP listener (port {INTERNAL_TCP_PORT}) to be ready...")
    try:
        await asyncio.wait_for(listener_ready_event.wait(), timeout=LISTENER_READY_TIMEOUT)
        logger.info(f"Worker {WORKER_ID}: TCP listener is ready.")
    except asyncio.TimeoutError:
        logger.critical(f"Worker {WORKER_ID}: Timeout waiting for TCP listener to start. Exiting.")
        if not listener_task.done(): listener_task.cancel()
        try: await listener_task
        except asyncio.CancelledError: pass
        STOP_EVENT.set() # Ensure main loop terminates if listener doesn't start
        return
    except Exception as e:
        logger.critical(f"Worker {WORKER_ID}: Error waiting for TCP listener: {e}. Exiting.")
        if not listener_task.done(): listener_task.cancel()
        try: await listener_task
        except asyncio.CancelledError: pass # Expected if cancelled
        except Exception as e_inner: logger.error(f"Error during listener task await after failure: {e_inner}")
        STOP_EVENT.set()
        return


    if STOP_EVENT.is_set(): # Check if listener task failed and set STOP_EVENT
        logger.warning(f"Worker {WORKER_ID}: Listener did not initialize correctly. Aborting rendezvous and P2P attempt.")
        # Wait for the listener_task to finish if it's still running (e.g., during its own cleanup)
        if not listener_task.done():
            try: await asyncio.wait_for(listener_task, timeout=5.0)
            except asyncio.TimeoutError: logger.warning("Timeout waiting for failed listener task to complete.")
            except Exception as e: logger.warning(f"Error awaiting failed listener task: {e}")
        return

    try:
        logger.info(f"Worker {WORKER_ID}: Connecting to Rendezvous server at {full_rendezvous_ws_url}")

        # Prepare SSL context that forces HTTP/1.1 ALPN to avoid Cloud Run occasionally selecting HTTP/2, which
        # the `websockets` library cannot parse. If the scheme is ws:// (plain-text), ssl_ctx will be None.
        ssl_ctx = None
        if full_rendezvous_ws_url.startswith("wss://"):
            import ssl
            ssl_ctx = ssl.create_default_context()
            try:
                # This attribute exists on modern OpenSSL / Python builds.
                ssl_ctx.set_alpn_protocols(["http/1.1"])
            except NotImplementedError:
                # Older OpenSSL versions may not support ALPN; ignore.
                pass

        async with websockets.connect(
            full_rendezvous_ws_url,
            ping_interval=20,
            open_timeout=WS_OPEN_TIMEOUT,
            ssl=ssl_ctx,
        ) as websocket:
            logger.info(f"Worker {WORKER_ID}: Connected to Rendezvous. Sending TCP info...")
            await websocket.send(json.dumps({
                "type": "report_tcp_info",
                "public_ip": p2p_public_ip_to_report, # Use IP seen by echo server, or ipify if echo failed
                "internal_tcp_port": INTERNAL_TCP_PORT,
                "mapped_public_port": p2p_mapped_port_to_report # Use port seen by echo, or None if failed
            }))

            logger.info(f"Worker {WORKER_ID}: Waiting for peer info from Rendezvous (timeout: {WS_RECV_TIMEOUT}s)...")
            message_raw: str = await asyncio.wait_for(websocket.recv(), timeout=WS_RECV_TIMEOUT)  # type: ignore
            message_data = json.loads(message_raw)

            if message_data.get("type") == "p2p_tcp_peer_info":
                peer_id = message_data.get("peer_worker_id")
                peer_public_ip = message_data.get("peer_public_ip")
                peer_internal_tcp_port_str = message_data.get("peer_internal_tcp_port")
                peer_mapped_public_port_str = message_data.get("peer_mapped_public_port")

                if not (peer_id and peer_public_ip and peer_internal_tcp_port_str is not None):
                    logger.error(f"Worker {WORKER_ID}: Incomplete peer info from Rendezvous: {message_data}")
                    # STOP_EVENT is not set here; listener continues, main task will finish
                    return

                try:
                    # Prefer mapped public port if provided; fall back to peer's internal port otherwise.
                    if peer_mapped_public_port_str is not None:
                        peer_port_to_use = int(peer_mapped_public_port_str)
                        logger.info(
                            f"Worker {WORKER_ID}: Using peer's mapped_public_port={peer_port_to_use} for connection attempts."
                        )
                    else:
                        peer_port_to_use = int(peer_internal_tcp_port_str)
                        logger.info(
                            f"Worker {WORKER_ID}: Peer did not provide mapped_public_port. Using internal port {peer_port_to_use}."
                        )
                    logger.info(
                        f"Worker {WORKER_ID}: Received peer info: ID={peer_id}, IP={peer_public_ip}, PortToUse={peer_port_to_use}"
                    )
                    # This call will set STOP_EVENT upon completion or P2P error via handle_peer_connection
                    await attempt_p2p_tcp_connect(str(peer_public_ip), peer_port_to_use)
                except ValueError:
                    logger.error(f"Worker {WORKER_ID}: Invalid peer port format from rendezvous: '{peer_internal_tcp_port_str}'")
                    # STOP_EVENT is not set here; listener continues
                    return 
            else:
                logger.warning(f"Worker {WORKER_ID}: Received unexpected message from Rendezvous: {message_data}")
                # STOP_EVENT is not set here; listener continues
                return

    except websockets.exceptions.InvalidURI:
        logger.critical(f"Worker {WORKER_ID}: Invalid Rendezvous URI: {full_rendezvous_ws_url}. Exiting.", exc_info=True)
        STOP_EVENT.set()
    except websockets.exceptions.InvalidMessage as im_err:
        # This often indicates the server replied with HTTP/2 (binary) instead of HTTP/1.1, which
        # can happen if ALPN negotiates h2. Retry once with plain ws:// as a fallback.
        if full_rendezvous_ws_url.startswith("wss://"):
            fallback_url = full_rendezvous_ws_url.replace("wss://", "ws://", 1)
            logger.warning(
                f"Worker {WORKER_ID}: Received InvalidMessage during WebSocket handshake (likely HTTP/2). "
                f"Retrying once with plain-text WebSocket at {fallback_url} ..."
            )
            try:
                async with websockets.connect(
                    fallback_url,
                    ping_interval=20,
                    open_timeout=WS_OPEN_TIMEOUT,
                ) as websocket:
                    logger.info(f"Worker {WORKER_ID}: Connected to Rendezvous (fallback ws). Sending TCP info...")
                    await websocket.send(json.dumps({
                        "type": "report_tcp_info",
                        "public_ip": p2p_public_ip_to_report, # Use IP seen by echo server, or ipify if echo failed
                        "internal_tcp_port": INTERNAL_TCP_PORT,
                        "mapped_public_port": p2p_mapped_port_to_report # Use port seen by echo, or None if failed
                    }))
                    # reuse the same receive / peer-handling logic by duplicating minimal portion.
                    logger.info(f"Worker {WORKER_ID}: Waiting for peer info from Rendezvous (timeout: {WS_RECV_TIMEOUT}s)...")
                    message_raw: str = await asyncio.wait_for(websocket.recv(), timeout=WS_RECV_TIMEOUT)  # type: ignore
                    message_data = json.loads(message_raw)
                    # We jump to same handling logic by simulating else path; easiest is to recursion or copy: we'll copy below quickly.
                    if message_data.get("type") == "p2p_tcp_peer_info":
                        peer_id = message_data.get("peer_worker_id")
                        peer_public_ip = message_data.get("peer_public_ip")
                        peer_internal_tcp_port_str = message_data.get("peer_internal_tcp_port")
                        peer_mapped_public_port_str = message_data.get("peer_mapped_public_port")

                        if not (peer_id and peer_public_ip and peer_internal_tcp_port_str is not None):
                            logger.error(f"Worker {WORKER_ID}: Incomplete peer info from Rendezvous: {message_data}")
                        else:
                            try:
                                if peer_mapped_public_port_str is not None:
                                    peer_port_to_use = int(peer_mapped_public_port_str)
                                    logger.info(
                                        f"Worker {WORKER_ID}: Using peer's mapped_public_port={peer_port_to_use} for connection attempts."
                                    )
                                else:
                                    peer_port_to_use = int(peer_internal_tcp_port_str)
                                    logger.info(
                                        f"Worker {WORKER_ID}: Peer did not provide mapped_public_port. Using internal port {peer_port_to_use}."
                                    )
                                logger.info(
                                    f"Worker {WORKER_ID}: Received peer info: ID={peer_id}, IP={peer_public_ip}, PortToUse={peer_port_to_use}"
                                )
                                await attempt_p2p_tcp_connect(str(peer_public_ip), peer_port_to_use)
                            except ValueError:
                                logger.error(f"Worker {WORKER_ID}: Invalid peer port format from rendezvous: '{peer_internal_tcp_port_str}'")
                    else:
                        logger.warning(f"Worker {WORKER_ID}: Received unexpected message from Rendezvous: {message_data}")
            except Exception as fallback_err:
                logger.critical(
                    f"Worker {WORKER_ID}: Fallback ws:// handshake also failed: {fallback_err}. Exiting.",
                    exc_info=True,
                )
                STOP_EVENT.set()
        else:
            logger.critical(
                f"Worker {WORKER_ID}: WebSocket connection error with Rendezvous (InvalidMessage): {im_err}. Exiting.",
                exc_info=True,
            )
            STOP_EVENT.set()
    except (websockets.exceptions.WebSocketException, ConnectionRefusedError) as e:  # More specific catch
        logger.critical(f"Worker {WORKER_ID}: WebSocket connection error with Rendezvous ({type(e).__name__}): {e}. Exiting.", exc_info=True)
        STOP_EVENT.set()
    except asyncio.TimeoutError:
        logger.critical(f"Worker {WORKER_ID}: Timeout during rendezvous communication. Exiting.", exc_info=True)
        STOP_EVENT.set()
    except json.JSONDecodeError:
        logger.critical(f"Worker {WORKER_ID}: Could not decode JSON from Rendezvous message. Exiting.", exc_info=True)
        STOP_EVENT.set()
    except Exception: # Catch-all for other errors
        logger.critical(f"Worker {WORKER_ID}: Error in Rendezvous client or P2P management. Exiting.", exc_info=True)
        STOP_EVENT.set()
    finally:
        logger.info(f"Worker {WORKER_ID}: Rendezvous client and P2P manager task finished.")
        # If this phase finishes without STOP_EVENT being set by P2P interaction or a critical error,
        # set it now to ensure the worker terminates gracefully after one cycle.
        if not STOP_EVENT.is_set():
            logger.info(f"Worker {WORKER_ID}: Main P2P/Rendezvous logic finished. Setting STOP_EVENT to terminate worker cycle.")
            STOP_EVENT.set()
        
        # Ensure listener task is cleaned up if it hasn't already finished due to STOP_EVENT
        if listener_task and not listener_task.done():
            logger.info(f"Worker {WORKER_ID}: Ensuring listener task ({listener_task.get_name()}) is handled post-manager logic.")
            # STOP_EVENT being set should cause listener_task to exit its `await STOP_EVENT.wait()`
            # We await it here to ensure its own finally block (server.close()) completes.
            try:
                await asyncio.wait_for(listener_task, timeout=10.0) # Give it time to shutdown gracefully
                logger.info(f"Worker {WORKER_ID}: Listener task confirmed finished post-manager logic.")
            except asyncio.TimeoutError:
                logger.warning(f"Worker {WORKER_ID}: Timeout waiting for listener task to complete during final cleanup. Cancelling.")
                listener_task.cancel()
                try: await listener_task
                except asyncio.CancelledError: pass # Expected
            except Exception as e:
                logger.error(f"Worker {WORKER_ID}: Error awaiting listener task during final cleanup: {e}")


def health_check_server_thread_func() -> None:
    class SimpleHTTPRequestHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        def log_message(self, format_str: str, *args: Any) -> None: # type: ignore
            # logger.debug(f"Health check request: {format_str % args}") # Optional: log health checks at DEBUG
            return # Suppress default http.server logs unless debugging

    server_address = ("0.0.0.0", APP_PORT)
    logger.info(f"Worker {WORKER_ID}: Health check HTTP server starting on 0.0.0.0:{APP_PORT}")
    try:
        with http.server.ThreadingHTTPServer(server_address, SimpleHTTPRequestHandler) as httpd:
            logger.info(f"Worker {WORKER_ID}: Health check HTTP server listening on 0.0.0.0:{APP_PORT}")
            httpd.serve_forever() # This will block until server is shut down, but it's in a daemon thread.
    except Exception:
        logger.critical(f"Worker {WORKER_ID}: Health check server failed to start or crashed.", exc_info=True)
        # This might warrant setting STOP_EVENT if health checks are critical for Cloud Run liveness
        # However, health check failing won't stop the daemon thread unless it crashes the whole process.
        # If Cloud Run relies on this health check, the instance might be recycled.
        # STOP_EVENT.set() # Consider if this is desired.
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