"""FastAPI rendezvous coordinator for the Cloud Run TCP-hole-punch experiment (2025-ready)."""
import asyncio
import json
import logging # <<< Added
import os
from typing import Dict, List, Any, Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from starlette.websockets import WebSocketState

# --- Logging Setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(module)s - %(funcName)s - %(lineno)d - %(message)s",
    handlers=[logging.StreamHandler()] # Output to stdout/stderr for Cloud Run
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Rendezvous Service for TCP P2P Test (2025)")

# worker_id → { "ws": WebSocket, "public_ip": str, "internal_tcp_port": int }
# Using Optional for values that might not be set initially
ConnectedWorkerInfo = Dict[str, Optional[Any]]
connected_workers: Dict[str, ConnectedWorkerInfo] = {}
workers_ready_for_pairing: List[str] = []

# Define keys used in ConnectedWorkerInfo dictionaries for clarity
KEY_WS = "ws"
KEY_PUBLIC_IP = "public_ip"
KEY_INTERNAL_PORT = "internal_tcp_port"
KEY_MAPPED_PUBLIC_PORT = "mapped_public_port"  # NEW: external NAT port as observed by /discover_tcp_port


async def attempt_tcp_pairing() -> None:
    """Pop two workers from pending and send them each other's details."""
    if len(workers_ready_for_pairing) < 2:
        # logger.debug(f"TCP Pairing: Not enough workers ({len(workers_ready_for_pairing)}). Waiting.")
        return

    peer_a_id = workers_ready_for_pairing.pop(0)
    peer_b_id = workers_ready_for_pairing.pop(0)

    peer_a_data = connected_workers.get(peer_a_id)
    peer_b_data = connected_workers.get(peer_b_id)

    # Ensure data and WebSocket objects are present
    if not (
        peer_a_data and peer_b_data and
        peer_a_data.get(KEY_WS) and peer_b_data.get(KEY_WS) and
        peer_a_data.get(KEY_PUBLIC_IP) and peer_b_data.get(KEY_PUBLIC_IP) and
        peer_a_data.get(KEY_INTERNAL_PORT) and peer_b_data.get(KEY_INTERNAL_PORT)
    ):  # Check required fields
        logger.warning(
            f"TCP Pairing Error: Data, WebSocket, or IP missing for {peer_a_id} or {peer_b_id}. Re-queuing valid."
        )
        if peer_a_data and peer_a_data.get(KEY_PUBLIC_IP) and peer_a_id not in workers_ready_for_pairing:
            workers_ready_for_pairing.append(peer_a_id)
        if peer_b_data and peer_b_data.get(KEY_PUBLIC_IP) and peer_b_id not in workers_ready_for_pairing:
            workers_ready_for_pairing.append(peer_b_id)
        return

    logger.info(f"TCP Pairing: Attempting to pair Worker '{peer_a_id}' with Worker '{peer_b_id}'.")

    make_payload = lambda peer_id, record: json.dumps({
        "type": "p2p_tcp_peer_info",
        "peer_worker_id": peer_id,
        "peer_public_ip": record[KEY_PUBLIC_IP],
        "peer_internal_tcp_port": record[KEY_INTERNAL_PORT],
        "peer_mapped_public_port": record.get(KEY_MAPPED_PUBLIC_PORT),
    })

    try:
        # Types for Pylance/MyPy satisfaction if ws is Any initially.
        ws_a: WebSocket = peer_a_data[KEY_WS] # type: ignore
        ws_b: WebSocket = peer_b_data[KEY_WS] # type: ignore

        await ws_a.send_text(make_payload(peer_b_id, peer_b_data))
        await ws_b.send_text(make_payload(peer_a_id, peer_a_data))
        logger.info(f"TCP Pairing: Sent peer info to '{peer_a_id}' and '{peer_b_id}'.")
    except Exception:
        logger.exception(f"TCP Pairing: Error sending peer info to {peer_a_id} or {peer_b_id}.")
        # Re-add to list if they are still valid, in case of transient send error
        for wid, data in [(peer_a_id, peer_a_data), (peer_b_id, peer_b_data)]:
            if wid in connected_workers and data and data.get(KEY_PUBLIC_IP) and wid not in workers_ready_for_pairing:
                workers_ready_for_pairing.append(wid)


@app.websocket("/ws/register_tcp/{worker_id}")
async def websocket_register_tcp_worker(websocket: WebSocket, worker_id: str):
    await websocket.accept()
    logger.info(f"TCP Worker '{worker_id}' connecting via WebSocket...")

    old_data = connected_workers.pop(worker_id, None)
    if old_data and old_data.get(KEY_WS):
        old_ws: WebSocket = old_data[KEY_WS] # type: ignore
        if old_ws.application_state == WebSocketState.CONNECTED:
            try:
                await old_ws.close(code=1000)
                logger.info(f"Closed stale WebSocket for worker '{worker_id}'.")
            except Exception:
                logger.exception(f"Error closing stale WebSocket for '{worker_id}'.")

    if worker_id in workers_ready_for_pairing: # Clean up from pairing queue if re-registering
        try:
            workers_ready_for_pairing.remove(worker_id)
        except ValueError:
            pass # Already removed

    connected_workers[worker_id] = {
        KEY_WS: websocket,
        KEY_PUBLIC_IP: None,
        KEY_INTERNAL_PORT: None,
        KEY_MAPPED_PUBLIC_PORT: None,
    }

    try:
        while True:
            raw_data = await websocket.receive_text()
            message = json.loads(raw_data)
            msg_type = message.get("type")

            if msg_type == "report_tcp_info":
                pub_ip = message.get("public_ip")
                internal_port_str = message.get("internal_tcp_port")
                mapped_public_port_str = message.get("mapped_public_port")

                if pub_ip and isinstance(pub_ip, str) and internal_port_str is not None:
                    try:
                        internal_port = int(internal_port_str)
                        mapped_public_port = int(mapped_public_port_str) if mapped_public_port_str is not None else None
                        # Ensure worker_id is still in connected_workers, as disconnect could happen
                        if worker_id in connected_workers:
                            connected_workers[worker_id][KEY_PUBLIC_IP] = pub_ip
                            connected_workers[worker_id][KEY_INTERNAL_PORT] = internal_port
                            connected_workers[worker_id][KEY_MAPPED_PUBLIC_PORT] = mapped_public_port
                            logger.info(
                                "TCP Worker '%s': Reported IP=%s, internal_port=%s, mapped_public_port=%s",
                                worker_id,
                                pub_ip,
                                internal_port,
                                mapped_public_port,
                            )
                            if worker_id not in workers_ready_for_pairing:
                                workers_ready_for_pairing.append(worker_id)
                            await attempt_tcp_pairing()
                        else:
                            logger.warning("Worker '%s' reported info but was no longer in connected_workers.", worker_id)
                    except ValueError:
                        logger.warning("TCP Worker '%s': Invalid port format in: '%s' / '%s'", worker_id, internal_port_str, mapped_public_port_str)
                else:
                    logger.warning("TCP Worker '%s': Incomplete/invalid TCP info received: %s", worker_id, message)
            else:
                logger.warning(f"TCP Worker '{worker_id}': Unhandled message type: '{msg_type}'. Data: {message}")
    except WebSocketDisconnect:
        logger.info(f"TCP Worker '{worker_id}' WebSocket disconnected.")
    except json.JSONDecodeError:
        logger.warning(f"TCP Worker '{worker_id}': Received invalid JSON from WebSocket.")
    except Exception: # Catch other unexpected errors during WebSocket communication
        logger.exception(f"Unexpected error with TCP worker '{worker_id}' WebSocket.")
    finally:
        logger.info(f"Cleaning up worker '{worker_id}'.")
        if worker_id in connected_workers:
            del connected_workers[worker_id]
        if worker_id in workers_ready_for_pairing:
            try:
                workers_ready_for_pairing.remove(worker_id)
            except ValueError:
                pass # Should not happen if logic is sound
        logger.info(f"TCP Worker '{worker_id}' de-registered. Total connected: {len(connected_workers)}, Ready for pairing: {len(workers_ready_for_pairing)}")


@app.get("/debug/tcp_workers")
async def list_tcp_workers():
    # More detailed debug information
    workers_details: Dict[str, Dict[str, Any]] = {}
    for wid, wdata in connected_workers.items():
        ws_state_name = "UNKNOWN_STATE"
        ws_obj = wdata.get(KEY_WS)
        if ws_obj and isinstance(ws_obj, WebSocket):
            try:
                ws_state_name = ws_obj.application_state.name
            except Exception: # If state cannot be accessed
                ws_state_name = "ERROR_ACCESSING_STATE"

        workers_details[wid] = {
            KEY_PUBLIC_IP: wdata.get(KEY_PUBLIC_IP),
            KEY_INTERNAL_PORT: wdata.get(KEY_INTERNAL_PORT),
            "websocket_state": ws_state_name
        }
    return {
        "connected_workers_count": len(connected_workers),
        "workers_ready_for_pairing_count": len(workers_ready_for_pairing),
        "ready_for_pairing_ids": list(workers_ready_for_pairing),
        "connected_workers_details": workers_details,
    }

@app.get("/discover_tcp_port")
async def discover_tcp_port(request: Request):
    """Endpoint used by workers to learn which NAT port their outbound connection is using.

    It simply returns the client's apparent IP and *source* TCP port as observed by this
    rendezvous instance. Workers can use this data to discover whether Cloud NAT preserves
    their chosen source port or rewrites it. This is analogous to a very small subset of
    STUN functionality, specialised for TCP.
    """
    client = request.client
    if client is None:
        return {"error": "Could not determine client address."}

    client_ip, client_port = client.host, client.port
    logger.info(
        "Port discovery request from %s:%s", client_ip, client_port
    )
    return {
        "your_ip": client_ip,
        "your_mapped_port": client_port,
    }

if __name__ == "__main__":
    service_port = int(os.getenv("PORT", "8080")) # Get port from environment
    logger.info(f"Starting Rendezvous Service on host 0.0.0.0, port {service_port}")
    uvicorn.run(app, host="0.0.0.0", port=service_port) 