import asyncio
import json
from pathlib import Path
from typing import Optional, Tuple, Set, Dict, Callable, Any
import time

import websockets
from websockets.http import Headers # For type hint, or use wsproto.connection.Headers

# This set will store active UI websocket clients, managed within this module.
ui_websocket_clients: Set[websockets.WebSocketServerProtocol] = set()

async def process_http_request(path: str, request_headers: Headers) -> Optional[Tuple[int, Headers, bytes]]:
    if path == "/ui_ws": return None  
    if path == "/":
        try:
            # Assuming index.html is in the same directory as ui_server.py or main.py directory structure needs care.
            # For now, let's assume it's relative to where ui_server.py is.
            # If ui_server.py is in a subdirectory, this Path needs to be adjusted or made absolute/configurable.
            html_path = Path(__file__).parent / "index.html"
            with open(html_path, "rb") as f: content = f.read()
            headers = Headers([("Content-Type", "text/html"), ("Content-Length", str(len(content)))])
            return (200, headers, content)
        except FileNotFoundError: 
            # Try relative to one level up if main.py calls this and index.html is with main.py
            try:
                html_path = Path(__file__).parent.parent / "index.html"
                with open(html_path, "rb") as f: content = f.read()
                headers = Headers([("Content-Type", "text/html"), ("Content-Length", str(len(content)))])
                return (200, headers, content)
            except FileNotFoundError:
                return (404, Headers([("Content-Type", "text/plain")]), b"index.html not found")
        except Exception as e_file: 
            print(f"Error serving index.html: {e_file}")
            return (500, Headers([("Content-Type", "text/plain")]), b"Internal Server Error")
    elif path == "/health": return (200, Headers([("Content-Type", "text/plain")]), b"OK")
    else: return (404, Headers([("Content-Type", "text/plain")]), b"Not Found")

async def ui_websocket_handler(
    websocket: websockets.WebSocketServerProtocol,
    path: str, # path is passed by websockets_serve but not used by original handler
    worker_id_val: str,
    get_p2p_info: Callable[[], Tuple[Optional[str], Optional[asyncio.DatagramTransport], Optional[Tuple[str, int]]]],
    get_rendezvous_ws: Callable[[], Optional[websockets.WebSocketClientProtocol]],
    # STUN related config for redial:
    config_internal_udp_port: int,
    config_default_stun_host: str,
    config_default_stun_port: int,
    # Functions to call (originally global or from other modules)
    benchmark_sender_func: Callable, # benchmark_send_udp_data from main.py
    delayed_exit_caller: Callable, # delayed_exit from main.py
    discover_stun_caller: Callable, # discover_stun_endpoint from network_utils.py
    # For STUN redial to update main.py's globals for discovered IP/Port
    update_main_stun_globals_func: Callable[[Optional[str], Optional[int]], None],
    # Provide access to current QuicTunnel (if any)
    get_quic_engine_global_func: Callable[[], Optional[Any]]
):
    global ui_websocket_clients
    ui_websocket_clients.add(websocket)
    print(f"Worker '{worker_id_val}': UI WebSocket client connected from {websocket.remote_address}")
    
    current_p2p_peer_id, _, _ = get_p2p_info() # Initial P2P info

    try:
        await websocket.send(json.dumps({"type": "init_info", "worker_id": worker_id_val, "p2p_peer_id": current_p2p_peer_id}))
        # Send initial QUIC status if tunnel already up
        try:
            init_quic_engine = get_quic_engine_global_func()
            if init_quic_engine and getattr(init_quic_engine, "handshake_completed", False):
                await websocket.send(json.dumps({
                    "type": "quic_status_update",
                    "state": "connected",
                    "role": "client" if init_quic_engine.is_client else "server",
                    "peer": f"{init_quic_engine.peer_addr[0]}:{init_quic_engine.peer_addr[1]}"
                }))
        except Exception:
            pass

        async for message_raw in websocket:
            print(f"Worker '{worker_id_val}': Message from UI WebSocket: {message_raw}")
            try:
                message = json.loads(message_raw)
                msg_type = message.get("type")

                # Refresh P2P info as it might change
                current_p2p_peer_id_latest, p2p_udp_transport_latest, current_p2p_peer_addr_latest = get_p2p_info()
                active_rendezvous_websocket_latest = get_rendezvous_ws()

                if msg_type == "send_p2p_message":
                    content = message.get("content")
                    if content and current_p2p_peer_addr_latest and p2p_udp_transport_latest:
                        print(f"Worker '{worker_id_val}': Sending P2P UDP message '{content}' to peer {current_p2p_peer_id_latest} at {current_p2p_peer_addr_latest}")
                        p2p_message = {"type": "chat_message", "from_worker_id": worker_id_val, "content": content}
                        p2p_udp_transport_latest.sendto(json.dumps(p2p_message).encode(), current_p2p_peer_addr_latest)
                    elif not current_p2p_peer_addr_latest: await websocket.send(json.dumps({"type": "error", "message": "Not connected to a P2P peer."}))
                    elif not content: await websocket.send(json.dumps({"type": "error", "message": "Cannot send empty message."}))
                
                elif msg_type == "p2p_ping_command":
                    if current_p2p_peer_addr_latest and p2p_udp_transport_latest:
                        ping_payload = {
                            "type": "p2p_ping_request",
                            "from_worker_id": worker_id_val,
                            "timestamp": time.monotonic()
                        }
                        p2p_udp_transport_latest.sendto(json.dumps(ping_payload).encode(), current_p2p_peer_addr_latest)
                        print(f"Worker '{worker_id_val}': Sent P2P Ping Request to {current_p2p_peer_id_latest}.")
                        # UI will show RTT when "p2p_ping_response" is received via broadcast
                    else:
                        await websocket.send(json.dumps({"type": "system_message", "message": "Cannot send P2P Ping: No active P2P peer."}))

                elif msg_type == "quic_echo_command":
                    quic_engine = get_quic_engine_global_func()
                    echo_payload_str = message.get("payload", f"Echo from {worker_id_val}")

                    if quic_engine and getattr(quic_engine, "handshake_completed", False):
                        timestamp = time.monotonic()
                        full_echo_payload = f"QUIC_ECHO_REQUEST {worker_id_val} {timestamp} {echo_payload_str}".encode()
                        # Use helper on QuicTunnel
                        try:
                            await quic_engine.send_app_data(full_echo_payload)
                        except Exception as e:
                            console_msg = f"Error sending QUIC echo: {e}"
                            print(console_msg)
                            await websocket.send(json.dumps({"type":"system_message","message":console_msg}))
                        print(f"Worker '{worker_id_val}': Sent QUIC Echo Request: '{echo_payload_str}'")
                    else:
                        await websocket.send(json.dumps({"type": "system_message", "message": "Cannot send QUIC Echo: QUIC tunnel not ready."}))

                elif msg_type == "p2p_ping_response": # Received from P2PUDPProtocol broadcast
                    original_timestamp = message.get("original_timestamp")
                    from_peer = message.get("from_peer_id") # Should be the remote peer
                    if original_timestamp is not None:
                        rtt_ms = (time.monotonic() - float(original_timestamp)) * 1000
                        print(f"Worker '{worker_id_val}': Received P2P Ping Response from '{from_peer}'. RTT: {rtt_ms:.2f} ms.")
                        # Send to the specific UI client that might be waiting, or broadcast if simpler
                        await websocket.send(json.dumps({
                            "type": "p2p_ping_result", # New type for UI to display
                            "rtt_ms": rtt_ms,
                            "peer_id": from_peer
                        }))
                
                elif msg_type == "quic_echo_response": # Received from QuicTunnel broadcast
                    rtt_ms = message.get("rtt_ms")
                    echoed_payload = message.get("payload")
                    from_peer_addr = message.get("peer") # This is peer_addr from QuicTunnel event
                    print(f"Worker '{worker_id_val}': Received QUIC Echo Response from peer {from_peer_addr}. RTT: {rtt_ms:.2f} ms. Payload: '{echoed_payload}'")
                    await websocket.send(json.dumps({
                        "type": "quic_echo_result", # New type for UI
                        "rtt_ms": rtt_ms,
                        "payload": echoed_payload,
                        "peer": from_peer_addr
                    }))

                elif msg_type == "ui_client_hello":
                    print(f"Worker '{worker_id_val}': UI Client says hello.")
                    if current_p2p_peer_id_latest:
                         await websocket.send(json.dumps({"type": "p2p_status_update", "message": f"P2P link active with {current_p2p_peer_id_latest[:8]}...", "peer_id": current_p2p_peer_id_latest}))
                
                elif msg_type == "start_benchmark_send":
                    size_kb = message.get("size_kb", 1024)
                    if current_p2p_peer_addr_latest:
                        print(f"Worker '{worker_id_val}': UI requested benchmark send of {size_kb}KB to {current_p2p_peer_id_latest}")
                        asyncio.create_task(benchmark_sender_func(current_p2p_peer_addr_latest[0], current_p2p_peer_addr_latest[1], size_kb, websocket))
                    else:
                        await websocket.send(json.dumps({"type": "benchmark_status", "message": "Error: No P2P peer to start benchmark with."}))
                
                elif msg_type == "restart_worker_request":
                    print(f"Worker '{worker_id_val}': Received restart_worker_request from UI. Attempting explicit deregistration.")
                    if active_rendezvous_websocket_latest:
                        try:
                            deregister_payload = json.dumps({"type": "explicit_deregister", "worker_id": worker_id_val})
                            await asyncio.wait_for(active_rendezvous_websocket_latest.send(deregister_payload), timeout=2.0)
                            print(f"Worker '{worker_id_val}': Sent explicit_deregister to Rendezvous.")
                        except asyncio.TimeoutError:
                            print(f"Worker '{worker_id_val}': Timeout sending explicit_deregister to Rendezvous.")
                        except Exception as e_dereg:
                            print(f"Worker '{worker_id_val}': Error sending explicit_deregister: {e_dereg}")
                    else:
                        print(f"Worker '{worker_id_val}': No active Rendezvous WebSocket to send explicit_deregister.")

                    await websocket.send(json.dumps({"type": "system_message", "message": "Worker is restarting..."}))
                    await websocket.close(code=1000, reason="Worker restarting")
                    asyncio.create_task(delayed_exit_caller(1))
                
                elif msg_type == "redo_stun_request":
                    # The check for p2p_udp_transport closing was removed from original logic when moving discover_and_report_stun_udp_endpoint
                    # It was: if p2p_udp_transport: try: p2p_udp_transport.close(); ...
                    # This logic needs to be handled in main.py via a callback if transport needs closing before STUN.
                    # For now, this handler assumes main.py manages transport closure if needed.
                    print(f"Worker '{worker_id_val}': UI requests STUN re-discovery.")

                    if active_rendezvous_websocket_latest:
                        try:
                            stun_result = await discover_stun_caller(worker_id_val, config_internal_udp_port, config_default_stun_host, config_default_stun_port)
                            if stun_result:
                                disc_ip, disc_port = stun_result
                                update_main_stun_globals_func(disc_ip, disc_port) # Update globals in main.py
                                await active_rendezvous_websocket_latest.send(
                                    json.dumps({
                                        "type": "update_udp_endpoint",
                                        "udp_ip": disc_ip,
                                        "udp_port": disc_port,
                                    })
                                )
                                print(f"Worker '{worker_id_val}': Sent STUN UDP endpoint ({disc_ip}:{disc_port}) to Rendezvous after UI request.")
                                await websocket.send(json.dumps({"type": "system_message", "message": "STUN re-discovery successful and endpoint re-registered."}))
                            else:
                                update_main_stun_globals_func(None, None) # Clear globals if STUN failed
                                await websocket.send(json.dumps({"type": "error", "message": "STUN re-discovery failed. Check logs."}))
                        except Exception as e_stun:
                            await websocket.send(json.dumps({"type": "error", "message": f"Error during STUN re-discovery: {type(e_stun).__name__} - {e_stun}"}))
                    else:
                        await websocket.send(json.dumps({"type": "error", "message": "No active connection to Rendezvous service for STUN re-register."}))
                    
                    try:
                        await websocket.send(json.dumps({"type": "system_message", "message": "STUN re-discovery process finished. Closing UI WebSocket to refresh state…"}))
                    except Exception: pass
                    try: await websocket.close(code=1000, reason="STUN rediscovery complete – please reconnect")
                    except Exception: pass
            
            except json.JSONDecodeError:
                print(f"Worker '{worker_id_val}': UI WebSocket received non-JSON: {message_raw}")
            except Exception as e_ui_msg:
                print(f"Worker '{worker_id_val}': Error processing UI WebSocket message: {type(e_ui_msg).__name__} - {e_ui_msg}")
    
    except websockets.exceptions.ConnectionClosed:
        print(f"Worker '{worker_id_val}': UI WebSocket client {websocket.remote_address} disconnected.")
    except Exception as e_ui_conn:
        print(f"Worker '{worker_id_val}': Error with UI WebSocket connection {websocket.remote_address}: {type(e_ui_conn).__name__} - {e_ui_conn}")
    finally:
        ui_websocket_clients.remove(websocket)
        print(f"Worker '{worker_id_val}': UI WebSocket client {websocket.remote_address} removed.")

async def broadcast_to_all_ui_clients(message_payload: Dict):
    """Broadcasts a JSON serializable dictionary to all connected UI clients."""
    global ui_websocket_clients
    if ui_websocket_clients:
        message_str = json.dumps(message_payload)
        tasks = [client.send(message_str) for client in ui_websocket_clients]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                # This identification of client is not perfect if the set changes during await
                # but provides some clue.
                client_list_for_error = list(ui_websocket_clients) 
                client_info = "unknown client" 
                if i < len(client_list_for_error): 
                    client_info = str(client_list_for_error[i].remote_address)
                print(f"Error broadcasting to UI client {client_info}: {result}")

# Placeholder for broadcasting to UI clients, if needed from ui_server.py itself.
# async def broadcast_to_ui_clients(message: str):
#     if ui_websocket_clients:
#         await asyncio.wait([client.send(message) for client in ui_websocket_clients]) 