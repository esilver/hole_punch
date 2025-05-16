import asyncio
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from typing import Dict, Tuple
import os
import json

app = FastAPI(title="Rendezvous Service")

# In-memory storage for connected workers.
# Format: {worker_id: {"public_ip": str, "public_port": int, "websocket": WebSocket}}
# WARNING: This is for PoC only. Data will be lost if the service restarts or scales to zero.
# For production, use an external store like Redis or Firestore.
connected_workers: Dict[str, Dict] = {}

@app.websocket("/ws/register/{worker_id}")
async def websocket_register_worker(websocket: WebSocket, worker_id: str):
    await websocket.accept()
    client_host = websocket.client.host
    client_port = websocket.client.port
    
    print(f"Worker '{worker_id}' connecting from {client_host}:{client_port}")

    if worker_id in connected_workers:
        # Potentially handle re-connection or duplicate ID scenario
        print(f"Worker '{worker_id}' re-connecting or duplicate ID detected.")
        # Close previous connection if it exists and is active
        # This logic might need to be more robust in a real system
        old_ws = connected_workers[worker_id].get("websocket")
        if old_ws and old_ws.client_state == 1: # WebSocketState.CONNECTED
             try:
                await old_ws.close(code=1000, reason="New connection from same worker ID")
             except Exception:
                pass # Ignore errors closing old socket

    connected_workers[worker_id] = {
        "public_ip": client_host,
        "public_port": client_port,
        "websocket": websocket # Store the WebSocket object for potential bi-directional communication
    }
    print(f"Worker '{worker_id}' registered. Endpoint: {client_host}:{client_port}. Total workers: {len(connected_workers)}")

    try:
        while True: # Keep connection alive and listen for messages
            raw_data = await websocket.receive_text()
            print(f"Received raw message from '{worker_id}': {raw_data}")
            try:
                message = json.loads(raw_data)
                msg_type = message.get("type")

                if msg_type == "register_public_ip":
                    new_ip = message.get("ip")
                    if new_ip:
                        print(f"Worker '{worker_id}' self-reported public IP: {new_ip}. Updating from {connected_workers[worker_id]['public_ip']}.")
                        connected_workers[worker_id]["public_ip"] = new_ip
                    else:
                        print(f"Worker '{worker_id}' sent register_public_ip message without an IP.")
                # Add other message type handlers here if needed in the future
                else:
                    print(f"Worker '{worker_id}' sent unhandled message type: {msg_type}")

            except json.JSONDecodeError:
                print(f"Worker '{worker_id}' sent non-JSON message: {raw_data}")
            except AttributeError: # If message is not a dict or .get fails
                print(f"Worker '{worker_id}' sent malformed JSON message: {raw_data}")

    except WebSocketDisconnect:
        print(f"Worker '{worker_id}' disconnected from {client_host}:{client_port}.")
    except Exception as e:
        print(f"Error with worker '{worker_id}': {e}")
    finally:
        if worker_id in connected_workers and connected_workers[worker_id]["websocket"] == websocket:
            del connected_workers[worker_id]
            print(f"Worker '{worker_id}' de-registered. Total workers: {len(connected_workers)}")

@app.get("/")
async def read_root():
    return {"message": "Rendezvous Service is running. Connect via WebSocket at /ws/register/{worker_id}"}

@app.get("/debug/list_workers")
async def list_workers():
    # Exclude WebSocket objects for simpler JSON response
    # WARNING: This is a debug endpoint. Secure or remove for production.
    workers_info = {}
    for worker_id, data in connected_workers.items():
        ws_object = data["websocket"]
        current_client_state = ws_object.client_state
        print(f"DEBUG: Worker {worker_id}, WebSocket object: {ws_object}, client_state raw value: {current_client_state}")
        workers_info[worker_id] = {
            "public_ip": data["public_ip"],
            "public_port": data["public_port"],
            "connected": current_client_state.value == 1, # WebSocketState.CONNECTED
            "raw_state": current_client_state
        }
    return {"connected_workers_count": len(workers_info), "workers": workers_info}

if __name__ == "__main__":
    # This is for local execution only (e.g., `python main.py`)
    # For Cloud Run, Uvicorn is started by the CMD in the Dockerfile.
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080))) 