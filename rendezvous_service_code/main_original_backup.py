import asyncio
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict, Optional, List, Tuple
from pydantic import BaseModel
import os
import json
from pathlib import Path

app = FastAPI(title="Rendezvous Service")

# Add CORS middleware for API access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify allowed origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# connected_workers structure from Step 3A:
# { worker_id: { "websocket_observed_ip": ..., "websocket_observed_port": ...,
#                "websocket": WebSocket, 
#                "stun_reported_udp_ip": ..., "stun_reported_udp_port": ...,
#                "http_reported_public_ip": ... # Optional, if you keep it
#              }}
connected_workers: Dict[str, Dict] = {}

# List of worker_ids that have reported UDP info and are waiting for a peer
workers_ready_for_pairing: List[str] = []

# Admin UI WebSocket clients
admin_websocket_clients: List[WebSocket] = []

# Chat sessions: admin_id -> worker_id -> websocket
chat_sessions: Dict[str, Dict[str, WebSocket]] = {}

# Pydantic models for API requests
class ConnectionRequest(BaseModel):
    worker_a_id: str
    worker_b_id: str

class ChatMessage(BaseModel):
    content: str
    worker_id: str

async def broadcast_to_admin_clients(message: dict):
    """Send a message to all connected admin UI WebSocket clients"""
    global admin_websocket_clients
    if admin_websocket_clients:
        # Create a copy to avoid modification during iteration
        clients_copy = admin_websocket_clients.copy()
        for client in clients_copy:
            try:
                await client.send_text(json.dumps(message))
            except Exception as e:
                print(f"Error broadcasting to admin client: {e}")
                # Remove disconnected client
                if client in admin_websocket_clients:
                    admin_websocket_clients.remove(client)

async def attempt_to_pair_workers(newly_ready_worker_id: str):
    global workers_ready_for_pairing, connected_workers

    # Ensure the worker trying to pair is valid and has WebSocket
    newly_ready_worker_data = connected_workers.get(newly_ready_worker_id)
    if not (newly_ready_worker_data and newly_ready_worker_data.get("stun_reported_udp_ip") and 
            newly_ready_worker_data.get("websocket") and 
            hasattr(newly_ready_worker_data["websocket"], 'client_state') and 
            newly_ready_worker_data["websocket"].client_state.value == 1):
        print(f"Pairing: Newly ready worker '{newly_ready_worker_id}' is not valid, lacks UDP info, or WebSocket is disconnected. Cannot initiate pairing.")
        if newly_ready_worker_id in workers_ready_for_pairing:
            workers_ready_for_pairing.remove(newly_ready_worker_id)
        # Also ensure it's removed from connected_workers if its WebSocket is truly gone or invalid
        if newly_ready_worker_id in connected_workers and (not newly_ready_worker_data or not newly_ready_worker_data.get("websocket") or 
                                                        not hasattr(newly_ready_worker_data.get("websocket"), 'client_state') or
                                                        newly_ready_worker_data.get("websocket").client_state.value != 1):
            del connected_workers[newly_ready_worker_id]
            print(f"Cleaned up disconnected newly_ready_worker_id '{newly_ready_worker_id}' from connected_workers.")
        return

    if newly_ready_worker_id not in workers_ready_for_pairing:
        workers_ready_for_pairing.append(newly_ready_worker_id)
        print(f"Rendezvous: Worker '{newly_ready_worker_id}' added to ready_for_pairing list. Current list: {workers_ready_for_pairing}")

    if len(workers_ready_for_pairing) < 2:
        print(f"Rendezvous: Not enough workers ready for pairing ({len(workers_ready_for_pairing)}). Waiting for more.")
        return

    peer_a_id = None
    peer_b_id = None
    
    # Create a copy of the list to iterate over, allowing modification of the original
    candidate_ids = list(workers_ready_for_pairing)
    
    # NEW: Expand candidate list to include other connected workers that have
    # valid UDP info and active WebSocket, even if they are not currently
    # in the "workers_ready_for_pairing" list. This prevents a situation
    # where a long-standing worker that was already removed from the ready
    # list never gets paired with a newly-arriving worker until it next
    # refreshes its STUN endpoint.
    extra_candidates = [wid for wid, wdata in connected_workers.items()
                        if wid not in workers_ready_for_pairing
                        and wid != newly_ready_worker_id
                        and wdata.get("stun_reported_udp_ip")
                        and wdata.get("stun_reported_udp_port")
                        and wdata.get("websocket")
                        and hasattr(wdata["websocket"], 'client_state')
                        and wdata["websocket"].client_state.value == 1]
    if extra_candidates:
        print(f"Rendezvous: Found {len(extra_candidates)} additional connected worker(s) with UDP info that were not in ready list: {extra_candidates}")
        candidate_ids.extend(extra_candidates)
    # END NEW BLOCK
    
    for worker_id in candidate_ids:
        worker_data = connected_workers.get(worker_id)
        # Check if worker is still valid and its WebSocket is connected
        if not (worker_data and worker_data.get("stun_reported_udp_ip") and 
                worker_data.get("websocket") and 
                hasattr(worker_data["websocket"], 'client_state') and 
                worker_data["websocket"].client_state.value == 1):
            
            print(f"Pairing: Worker '{worker_id}' in ready_list is stale or disconnected. Removing.")
            if worker_id in workers_ready_for_pairing:
                workers_ready_for_pairing.remove(worker_id)
            if worker_id in connected_workers: # Remove from main dict too if its websocket is bad
                 # Check ws state again to be sure before deleting, in case it reconnected quickly
                current_ws_state_in_dict = connected_workers[worker_id].get("websocket")
                if not current_ws_state_in_dict or not hasattr(current_ws_state_in_dict, 'client_state') or current_ws_state_in_dict.client_state.value != 1 :
                    del connected_workers[worker_id]
                    print(f"Cleaned up stale worker '{worker_id}' from connected_workers during pairing attempt.")
            continue # Skip this stale worker

        # Worker is valid, try to find a peer for it
        if peer_a_id is None:
            peer_a_id = worker_id
        elif peer_a_id != worker_id: # Found a distinct, valid second peer
            peer_b_id = worker_id
            break # Found a pair

    if peer_a_id and peer_b_id:
        # Selected pair: peer_a_id, peer_b_id
        print(f"Rendezvous: Attempting to pair Worker '{peer_a_id}' with Worker '{peer_b_id}'.")

        peer_a_data = connected_workers.get(peer_a_id) # Re-fetch, in case of rapid changes (though less likely now)
        peer_b_data = connected_workers.get(peer_b_id)

        # Final check for data integrity and WebSocket state before sending offers
        if not (peer_a_data and peer_b_data and
                peer_a_data.get("stun_reported_udp_ip") and peer_a_data.get("stun_reported_udp_port") and
                peer_b_data.get("stun_reported_udp_ip") and peer_b_data.get("stun_reported_udp_port") and
                peer_a_data.get("websocket") and hasattr(peer_a_data["websocket"], 'client_state') and peer_a_data["websocket"].client_state.value == 1 and
                peer_b_data.get("websocket") and hasattr(peer_b_data["websocket"], 'client_state') and peer_b_data["websocket"].client_state.value == 1):
            
            print(f"Pairing Error: Post-selection data integrity or WebSocket issue for {peer_a_id} or {peer_b_id}. Aborting this pair.")
            # Don't re-add to workers_ready_for_pairing here. If they are still valid, 
            # they'll be picked up in a subsequent call or re-register.
            # Cleanup if one of them is now definitively disconnected:
            for p_id in [peer_a_id, peer_b_id]:
                p_data = connected_workers.get(p_id)
                if p_data and (not p_data.get("websocket") or not hasattr(p_data["websocket"], 'client_state') or p_data["websocket"].client_state.value != 1):
                    if p_id in workers_ready_for_pairing: workers_ready_for_pairing.remove(p_id)
                    if p_id in connected_workers: del connected_workers[p_id]
                    print(f"Cleaned up disconnected peer '{p_id}' after failed pairing integrity check.")
            return

        # If all checks passed, remove from ready list and send offers
        if peer_a_id in workers_ready_for_pairing: workers_ready_for_pairing.remove(peer_a_id)
        if peer_b_id in workers_ready_for_pairing: workers_ready_for_pairing.remove(peer_b_id)
        print(f"Rendezvous: Removed '{peer_a_id}' and '{peer_b_id}' from ready_for_pairing. Updated list: {workers_ready_for_pairing}")

        peer_a_ws = peer_a_data["websocket"]
        peer_b_ws = peer_b_data["websocket"]

        offer_to_b_payload = { 
            "type": "p2p_connection_offer",
            "peer_worker_id": peer_a_id,
            "peer_udp_ip": peer_a_data["stun_reported_udp_ip"],
            "peer_udp_port": peer_a_data["stun_reported_udp_port"]
        }
        offer_to_a_payload = { 
            "type": "p2p_connection_offer",
            "peer_worker_id": peer_b_id,
            "peer_udp_ip": peer_b_data["stun_reported_udp_ip"],
            "peer_udp_port": peer_b_data["stun_reported_udp_port"]
        }

        try:
            # Send B's info to A
            # No need to check ws.client_state again due to comprehensive check above, but doesn't hurt.
            await peer_a_ws.send_text(json.dumps(offer_to_a_payload))
            print(f"Rendezvous: Sent connection offer to Worker '{peer_a_id}' (for peer '{peer_b_id}').")

            # Send A's info to B
            await peer_b_ws.send_text(json.dumps(offer_to_b_payload))
            print(f"Rendezvous: Sent connection offer to Worker '{peer_b_id}' (for peer '{peer_a_id}').")
        except Exception as e:
            print(f"Rendezvous: Error sending P2P connection offers: {e}")
    else:
        print(f"Rendezvous: No suitable peer found in ready_for_pairing list for newly ready worker '{newly_ready_worker_id}'.")

async def manual_pair_workers(worker_a_id: str, worker_b_id: str) -> bool:
    """Manually pair two specific workers. Returns True if successful."""
    global connected_workers
    
    # Get worker data
    worker_a_data = connected_workers.get(worker_a_id)
    worker_b_data = connected_workers.get(worker_b_id)
    
    # Validate both workers exist and have necessary data
    if not worker_a_data:
        raise HTTPException(status_code=404, detail=f"Worker '{worker_a_id}' not found")
    if not worker_b_data:
        raise HTTPException(status_code=404, detail=f"Worker '{worker_b_id}' not found")
    
    # Check WebSocket connections
    if not (worker_a_data.get("websocket") and hasattr(worker_a_data["websocket"], 'client_state') 
            and worker_a_data["websocket"].client_state.value == 1):
        raise HTTPException(status_code=400, detail=f"Worker '{worker_a_id}' WebSocket not connected")
    if not (worker_b_data.get("websocket") and hasattr(worker_b_data["websocket"], 'client_state') 
            and worker_b_data["websocket"].client_state.value == 1):
        raise HTTPException(status_code=400, detail=f"Worker '{worker_b_id}' WebSocket not connected")
    
    # Check UDP endpoints
    if not (worker_a_data.get("stun_reported_udp_ip") and worker_a_data.get("stun_reported_udp_port")):
        raise HTTPException(status_code=400, detail=f"Worker '{worker_a_id}' UDP endpoint not available")
    if not (worker_b_data.get("stun_reported_udp_ip") and worker_b_data.get("stun_reported_udp_port")):
        raise HTTPException(status_code=400, detail=f"Worker '{worker_b_id}' UDP endpoint not available")
    
    # Send connection offers
    try:
        # Prepare offers
        offer_to_a = {
            "type": "p2p_connection_offer",
            "peer_worker_id": worker_b_id,
            "peer_udp_ip": worker_b_data["stun_reported_udp_ip"],
            "peer_udp_port": worker_b_data["stun_reported_udp_port"]
        }
        offer_to_b = {
            "type": "p2p_connection_offer", 
            "peer_worker_id": worker_a_id,
            "peer_udp_ip": worker_a_data["stun_reported_udp_ip"],
            "peer_udp_port": worker_a_data["stun_reported_udp_port"]
        }
        
        # Send offers
        await worker_a_data["websocket"].send_text(json.dumps(offer_to_a))
        print(f"Manual pairing: Sent connection offer to Worker '{worker_a_id}' (for peer '{worker_b_id}')")
        
        await worker_b_data["websocket"].send_text(json.dumps(offer_to_b))
        print(f"Manual pairing: Sent connection offer to Worker '{worker_b_id}' (for peer '{worker_a_id}')")
        
        return True
        
    except Exception as e:
        print(f"Manual pairing: Error sending connection offers: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to send connection offers: {str(e)}")

@app.websocket("/ws/register/{worker_id}")
async def websocket_register_worker(websocket: WebSocket, worker_id: str):
    global connected_workers, workers_ready_for_pairing
    await websocket.accept()
    client_host = websocket.client.host 
    client_port = websocket.client.port 
    
    print(f"Worker '{worker_id}' connecting from WebSocket endpoint: {client_host}:{client_port}")

    if worker_id in connected_workers:
        print(f"Worker '{worker_id}' re-connecting or duplicate ID detected.")
        old_ws_data = connected_workers.get(worker_id)
        if old_ws_data:
            old_ws = old_ws_data.get("websocket")
            if old_ws and hasattr(old_ws, 'client_state') and old_ws.client_state.value == 1:
                 try: await old_ws.close(code=1012, reason="New connection from same worker ID / Service Restarting")
                 except Exception: pass
        if worker_id in workers_ready_for_pairing: 
            workers_ready_for_pairing.remove(worker_id)

    connected_workers[worker_id] = {
        "websocket_observed_ip": client_host, 
        "websocket_observed_port": client_port, 
        "websocket": websocket,
        "stun_reported_udp_ip": None, 
        "stun_reported_udp_port": None,
        "http_reported_public_ip": None # Field for general public IP
    }
    print(f"Worker '{worker_id}' registered. WebSocket EP: {client_host}:{client_port}. Total: {len(connected_workers)}")
    
    # Broadcast worker connected event
    await broadcast_to_admin_clients({
        "type": "worker_connected",
        "worker_id": worker_id,
        "websocket_ip": client_host,
        "websocket_port": client_port,
        "total_workers": len(connected_workers)
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
                    if new_ip and worker_id in connected_workers:
                        connected_workers[worker_id]["http_reported_public_ip"] = new_ip 
                        print(f"Worker '{worker_id}' reported HTTP-based public IP: {new_ip}")
                
                elif msg_type == "update_udp_endpoint":
                    udp_ip = message.get("udp_ip")
                    udp_port = message.get("udp_port")
                    if udp_ip and udp_port and worker_id in connected_workers:
                        connected_workers[worker_id]["stun_reported_udp_ip"] = udp_ip
                        connected_workers[worker_id]["stun_reported_udp_port"] = int(udp_port)
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
                        if admin_session_id in chat_sessions and worker_id in chat_sessions[admin_session_id]:
                            admin_ws = chat_sessions[admin_session_id][worker_id]
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

            except json.JSONDecodeError: print(f"Rendezvous: Worker '{worker_id}' sent non-JSON: {raw_data}")
            except AttributeError: print(f"Rendezvous: Worker '{worker_id}' sent malformed JSON: {raw_data}")
            except KeyError: print(f"Rendezvous: Worker '{worker_id}' no longer in dict.")

    except WebSocketDisconnect:
        print(f"Worker '{worker_id}' disconnected from WebSocket EP: {client_host}:{client_port}.")
    except Exception as e:
        print(f"Error with worker '{worker_id}' WebSocket: {e}")
    finally:
        if worker_id in connected_workers and connected_workers[worker_id].get("websocket") == websocket:
            del connected_workers[worker_id]
            print(f"Worker '{worker_id}' de-registered. Total: {len(connected_workers)}")
            # Broadcast worker disconnected event
            await broadcast_to_admin_clients({
                "type": "worker_disconnected",
                "worker_id": worker_id,
                "total_workers": len(connected_workers)
            })
        if worker_id in workers_ready_for_pairing: 
            workers_ready_for_pairing.remove(worker_id)
            print(f"Worker '{worker_id}' removed from pending pairing list due to disconnect.")

@app.get("/")
async def read_root():
    return {"message": "Rendezvous Service is running. Test."}

@app.get("/admin")
async def admin_ui():
    """Serve the admin UI HTML page"""
    admin_html_path = Path(__file__).parent / "admin.html"
    if admin_html_path.exists():
        with open(admin_html_path, "r") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content)
    else:
        return HTMLResponse(content="<h1>Admin UI not found</h1>", status_code=404)

@app.get("/admin/chat")
async def admin_chat_ui():
    """Serve the admin chat UI HTML page"""
    chat_html_path = Path(__file__).parent / "admin-chat.html"
    if chat_html_path.exists():
        with open(chat_html_path, "r") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content)
    else:
        return HTMLResponse(content="<h1>Admin Chat UI not found</h1>", status_code=404)

@app.get("/debug/list_workers")
async def list_workers():
    workers_info = {}
    for worker_id_key, data_val in list(connected_workers.items()):
        ws_object = data_val.get("websocket")
        is_connected = False
        if ws_object and hasattr(ws_object, 'client_state') and ws_object.client_state.value == 1:
            is_connected = True
        
        workers_info[worker_id_key] = {
            "websocket_observed_ip": data_val.get("websocket_observed_ip"),
            "websocket_observed_port": data_val.get("websocket_observed_port"),
            "http_reported_public_ip": data_val.get("http_reported_public_ip"),
            "stun_reported_udp_ip": data_val.get("stun_reported_udp_ip"),
            "stun_reported_udp_port": data_val.get("stun_reported_udp_port"),
            "websocket_connected": is_connected
        }
    return {"connected_workers_count": len(workers_info), "workers": workers_info, "ready_for_pairing_count": len(workers_ready_for_pairing), "ready_list": workers_ready_for_pairing}

@app.get("/api/workers")
async def api_list_workers():
    """API endpoint for listing all connected workers with their status"""
    workers_list = []
    for worker_id, data in connected_workers.items():
        ws_object = data.get("websocket")
        is_connected = False
        if ws_object and hasattr(ws_object, 'client_state') and ws_object.client_state.value == 1:
            is_connected = True
        
        # Check if worker has valid UDP endpoint
        has_udp = bool(data.get("stun_reported_udp_ip") and data.get("stun_reported_udp_port"))
        
        # Check if worker is ready for pairing
        is_ready = worker_id in workers_ready_for_pairing
        
        workers_list.append({
            "worker_id": worker_id,
            "websocket_connected": is_connected,
            "has_udp_endpoint": has_udp,
            "ready_for_pairing": is_ready,
            "websocket_ip": data.get("websocket_observed_ip"),
            "websocket_port": data.get("websocket_observed_port"),
            "public_ip": data.get("http_reported_public_ip"),
            "udp_ip": data.get("stun_reported_udp_ip"),
            "udp_port": data.get("stun_reported_udp_port")
        })
    
    return {
        "workers": workers_list,
        "total_count": len(workers_list),
        "connected_count": sum(1 for w in workers_list if w["websocket_connected"]),
        "ready_count": len(workers_ready_for_pairing)
    }

@app.post("/api/connect")
async def connect_workers(request: ConnectionRequest):
    """Manually connect two specific workers"""
    if request.worker_a_id == request.worker_b_id:
        raise HTTPException(status_code=400, detail="Cannot connect a worker to itself")
    
    try:
        success = await manual_pair_workers(request.worker_a_id, request.worker_b_id)
        # Broadcast connection event to admin clients
        await broadcast_to_admin_clients({
            "type": "connection_initiated",
            "worker_a_id": request.worker_a_id,
            "worker_b_id": request.worker_b_id,
            "timestamp": asyncio.get_event_loop().time()
        })
        return {
            "success": success,
            "message": f"Connection initiated between {request.worker_a_id} and {request.worker_b_id}"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.websocket("/ws/admin")
async def admin_websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for admin UI real-time updates"""
    global admin_websocket_clients
    await websocket.accept()
    admin_websocket_clients.append(websocket)
    
    print(f"Admin WebSocket client connected. Total admin clients: {len(admin_websocket_clients)}")
    
    try:
        # Send initial state
        await websocket.send_text(json.dumps({
            "type": "connected",
            "message": "Connected to admin WebSocket"
        }))
        
        # Keep connection alive and wait for messages
        while True:
            try:
                # Wait for any message from client (for ping/pong)
                data = await websocket.receive_text()
                # Echo back for now
                if data == "ping":
                    await websocket.send_text(json.dumps({"type": "pong"}))
            except WebSocketDisconnect:
                break
                
    except Exception as e:
        print(f"Error in admin WebSocket: {e}")
    finally:
        if websocket in admin_websocket_clients:
            admin_websocket_clients.remove(websocket)
        print(f"Admin WebSocket client disconnected. Total admin clients: {len(admin_websocket_clients)}")

@app.websocket("/ws/chat/{admin_session_id}/{worker_id}")
async def chat_websocket_endpoint(websocket: WebSocket, admin_session_id: str, worker_id: str):
    """WebSocket endpoint for admin to chat with a specific worker"""
    global chat_sessions, connected_workers
    
    # Verify worker exists and is connected
    if worker_id not in connected_workers:
        await websocket.close(code=1008, reason="Worker not found")
        return
    
    worker_data = connected_workers[worker_id]
    if not (worker_data.get("websocket") and hasattr(worker_data["websocket"], 'client_state') 
            and worker_data["websocket"].client_state.value == 1):
        await websocket.close(code=1008, reason="Worker not connected")
        return
    
    await websocket.accept()
    
    # Store the chat session
    if admin_session_id not in chat_sessions:
        chat_sessions[admin_session_id] = {}
    chat_sessions[admin_session_id][worker_id] = websocket
    
    print(f"Admin chat session '{admin_session_id}' connected to worker '{worker_id}'")
    
    # Send initial connection message
    await websocket.send_text(json.dumps({
        "type": "chat_connected",
        "worker_id": worker_id,
        "message": f"Connected to worker {worker_id}"
    }))
    
    try:
        while True:
            try:
                # Receive message from admin
                data = await websocket.receive_text()
                message = json.loads(data)
                
                if message.get("type") == "chat_message":
                    content = message.get("content")
                    if content and worker_id in connected_workers:
                        worker_ws = connected_workers[worker_id].get("websocket")
                        if worker_ws:
                            # Forward message to worker
                            await worker_ws.send_text(json.dumps({
                                "type": "admin_chat_message",
                                "admin_session_id": admin_session_id,
                                "content": content
                            }))
                            
                            # Echo back to admin with timestamp
                            await websocket.send_text(json.dumps({
                                "type": "chat_message",
                                "from": "admin",
                                "content": content,
                                "timestamp": asyncio.get_event_loop().time()
                            }))
                        else:
                            await websocket.send_text(json.dumps({
                                "type": "error",
                                "message": "Worker disconnected"
                            }))
                            break
                            
            except WebSocketDisconnect:
                break
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Invalid message format"
                }))
                
    except Exception as e:
        print(f"Error in chat session '{admin_session_id}' with worker '{worker_id}': {e}")
    finally:
        # Clean up chat session
        if admin_session_id in chat_sessions and worker_id in chat_sessions[admin_session_id]:
            del chat_sessions[admin_session_id][worker_id]
            if not chat_sessions[admin_session_id]:  # Remove empty session dict
                del chat_sessions[admin_session_id]
        print(f"Admin chat session '{admin_session_id}' disconnected from worker '{worker_id}'")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), log_level="trace") 