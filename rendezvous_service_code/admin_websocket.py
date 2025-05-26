"""
WebSocket handlers for admin UI and chat functionality
"""
import json
import asyncio
from fastapi import WebSocket, WebSocketDisconnect

from models import service_state


async def admin_websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for admin UI real-time updates"""
    await websocket.accept()
    service_state.admin_websocket_clients.append(websocket)
    
    print(f"Admin WebSocket client connected. Total admin clients: {len(service_state.admin_websocket_clients)}")
    
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
        if websocket in service_state.admin_websocket_clients:
            service_state.admin_websocket_clients.remove(websocket)
        print(f"Admin WebSocket client disconnected. Total admin clients: {len(service_state.admin_websocket_clients)}")


async def chat_websocket_endpoint(websocket: WebSocket, admin_session_id: str, worker_id: str):
    """WebSocket endpoint for admin to chat with a specific worker"""
    # Verify worker exists and is connected
    if worker_id not in service_state.connected_workers:
        await websocket.close(code=1008, reason="Worker not found")
        return
    
    worker_data = service_state.connected_workers[worker_id]
    if not (worker_data.get("websocket") and hasattr(worker_data["websocket"], 'client_state') 
            and worker_data["websocket"].client_state.value == 1):
        await websocket.close(code=1008, reason="Worker not connected")
        return
    
    await websocket.accept()
    
    # Store the chat session
    if admin_session_id not in service_state.chat_sessions:
        service_state.chat_sessions[admin_session_id] = {}
    service_state.chat_sessions[admin_session_id][worker_id] = websocket
    
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
                    if content and worker_id in service_state.connected_workers:
                        worker_ws = service_state.connected_workers[worker_id].get("websocket")
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
        if admin_session_id in service_state.chat_sessions and worker_id in service_state.chat_sessions[admin_session_id]:
            del service_state.chat_sessions[admin_session_id][worker_id]
            if not service_state.chat_sessions[admin_session_id]:  # Remove empty session dict
                del service_state.chat_sessions[admin_session_id]
        print(f"Admin chat session '{admin_session_id}' disconnected from worker '{worker_id}'")