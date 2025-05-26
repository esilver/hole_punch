"""
WebSocket handler for worker connections
"""
import json
import asyncio
from fastapi import WebSocket, WebSocketDisconnect

from models import service_state
from broadcast import broadcast_to_admin_clients
from pairing import attempt_to_pair_workers


async def websocket_register_worker(websocket: WebSocket, worker_id: str):
    """Handle WebSocket connection for a worker"""
    await websocket.accept()
    client_host = websocket.client.host 
    client_port = websocket.client.port 
    
    print(f"Worker '{worker_id}' connecting from WebSocket endpoint: {client_host}:{client_port}")

    if worker_id in service_state.connected_workers:
        print(f"Worker '{worker_id}' re-connecting or duplicate ID detected.")
        old_ws_data = service_state.connected_workers.get(worker_id)
        if old_ws_data:
            old_ws = old_ws_data.get("websocket")
            if old_ws and hasattr(old_ws, 'client_state') and old_ws.client_state.value == 1:
                 try: 
                     await old_ws.close(code=1012, reason="New connection from same worker ID / Service Restarting")
                 except Exception: 
                     pass
        if worker_id in service_state.workers_ready_for_pairing: 
            service_state.workers_ready_for_pairing.remove(worker_id)

    service_state.connected_workers[worker_id] = {
        "websocket_observed_ip": client_host, 
        "websocket_observed_port": client_port, 
        "websocket": websocket,
        "stun_reported_udp_ip": None, 
        "stun_reported_udp_port": None,
        "http_reported_public_ip": None
    }
    print(f"Worker '{worker_id}' registered. WebSocket EP: {client_host}:{client_port}. Total: {len(service_state.connected_workers)}")
    
    # Broadcast worker connected event
    await broadcast_to_admin_clients({
        "type": "worker_connected",
        "worker_id": worker_id,
        "websocket_ip": client_host,
        "websocket_port": client_port,
        "total_workers": len(service_state.connected_workers)
    })

    try:
        while True: 
            raw_data = await websocket.receive_text()
            print(f"Rendezvous: Received raw message from '{worker_id}': {raw_data}")
            try:
                message = json.loads(raw_data)
                msg_type = message.get("type")

                if msg_type == "register_public_ip":
                    new_ip = message.get("ip")
                    if new_ip and worker_id in service_state.connected_workers:
                        service_state.connected_workers[worker_id]["http_reported_public_ip"] = new_ip 
                        print(f"Worker '{worker_id}' reported HTTP-based public IP: {new_ip}")
                
                elif msg_type == "update_udp_endpoint":
                    udp_ip = message.get("udp_ip")
                    udp_port = message.get("udp_port")
                    if udp_ip and udp_port and worker_id in service_state.connected_workers:
                        service_state.connected_workers[worker_id]["stun_reported_udp_ip"] = udp_ip
                        service_state.connected_workers[worker_id]["stun_reported_udp_port"] = int(udp_port)
                        print(f"Worker '{worker_id}' updated STUN UDP endpoint to: {udp_ip}:{udp_port}")
                        await websocket.send_text(json.dumps({"type": "udp_endpoint_ack", "status": "success"}))
                        # Broadcast UDP endpoint update
                        await broadcast_to_admin_clients({
                            "type": "worker_udp_updated",
                            "worker_id": worker_id,
                            "udp_ip": udp_ip,
                            "udp_port": udp_port
                        })
                        await attempt_to_pair_workers(worker_id)
                    else:
                        await websocket.send_text(json.dumps({"type": "udp_endpoint_ack", "status": "error", "detail": "Missing IP or Port"}))
                
                elif msg_type == "echo_request": 
                    payload = message.get("payload", "")
                    await websocket.send_text(json.dumps({
                        "type": "echo_response",
                        "original_payload": payload,
                        "processed_by_rendezvous": f"Rendezvous processed: '{payload.upper()}' for worker {worker_id}"
                    }))
                
                elif msg_type == "chat_response":
                    # Worker is responding to admin chat
                    admin_session_id = message.get("admin_session_id")
                    chat_content = message.get("content")
                    if admin_session_id and chat_content:
                        # Forward to the appropriate admin chat session
                        if admin_session_id in service_state.chat_sessions and worker_id in service_state.chat_sessions[admin_session_id]:
                            admin_ws = service_state.chat_sessions[admin_session_id][worker_id]
                            try:
                                await admin_ws.send_text(json.dumps({
                                    "type": "chat_message",
                                    "from": "worker",
                                    "worker_id": worker_id,
                                    "content": chat_content,
                                    "timestamp": asyncio.get_event_loop().time()
                                }))
                            except Exception as e:
                                print(f"Error forwarding chat to admin: {e}")
                
                else:
                    print(f"Rendezvous: Worker '{worker_id}' sent unhandled message type: {msg_type}")

            except json.JSONDecodeError: 
                print(f"Rendezvous: Worker '{worker_id}' sent non-JSON: {raw_data}")
            except AttributeError: 
                print(f"Rendezvous: Worker '{worker_id}' sent malformed JSON: {raw_data}")
            except KeyError: 
                print(f"Rendezvous: Worker '{worker_id}' no longer in dict.")

    except WebSocketDisconnect:
        print(f"Worker '{worker_id}' disconnected from WebSocket EP: {client_host}:{client_port}.")
    except Exception as e:
        print(f"Error with worker '{worker_id}' WebSocket: {e}")
    finally:
        if worker_id in service_state.connected_workers and service_state.connected_workers[worker_id].get("websocket") == websocket:
            del service_state.connected_workers[worker_id]
            print(f"Worker '{worker_id}' de-registered. Total: {len(service_state.connected_workers)}")
            # Broadcast worker disconnected event
            await broadcast_to_admin_clients({
                "type": "worker_disconnected",
                "worker_id": worker_id,
                "total_workers": len(service_state.connected_workers)
            })
        if worker_id in service_state.workers_ready_for_pairing: 
            service_state.workers_ready_for_pairing.remove(worker_id)
            print(f"Worker '{worker_id}' removed from pending pairing list due to disconnect.")