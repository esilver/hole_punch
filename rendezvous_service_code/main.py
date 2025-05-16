import asyncio
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from typing import Dict, Tuple, Optional
import os
import json

app = FastAPI(title="Rendezvous Service")

# Updated storage for connected workers to include UDP endpoint info
# Format: {worker_id: {
#    "public_ip": str, "public_port": int, # From WebSocket connection
#    "websocket": WebSocket,
#    "udp_ip": Optional[str], "udp_port": Optional[int] # Discovered via STUN by worker
# }}
connected_workers: Dict[str, Dict] = {}

@app.websocket("/ws/register/{worker_id}")
async def websocket_register_worker(websocket: WebSocket, worker_id: str):
    await websocket.accept()
    client_host = websocket.client.host # IP from WebSocket connection
    client_port = websocket.client.port # Port from WebSocket connection
    
    print(f"Worker '{worker_id}' connecting from WebSocket endpoint: {client_host}:{client_port}")

    if worker_id in connected_workers:
        print(f"Worker '{worker_id}' re-connecting or duplicate ID detected.")
        old_ws = connected_workers[worker_id].get("websocket")
        if old_ws and hasattr(old_ws, 'client_state') and old_ws.client_state.value == 1: # WebSocketState.CONNECTED
             try:
                await old_ws.close(code=1000, reason="New connection from same worker ID")
             except Exception:
                pass 

    connected_workers[worker_id] = {
        "public_ip": client_host, # This is the NAT IP for the WebSocket TCP connection
        "public_port": client_port, # This is the NAT port for the WebSocket TCP connection
        "websocket": websocket,
        "udp_ip": None, # Initialize UDP info as None
        "udp_port": None
    }
    print(f"Worker '{worker_id}' registered. WebSocket EP: {client_host}:{client_port}. Total: {len(connected_workers)}")

    try:
        while True: 
            raw_data = await websocket.receive_text()
            print(f"Received raw message from '{worker_id}': {raw_data}")
            try:
                message = json.loads(raw_data)
                msg_type = message.get("type")
                print(f"RENDEZVOUS_DEBUG: Parsed msg_type: {repr(msg_type)}")

                if msg_type == "register_public_ip": # Worker's self-discovered HTTP/general public IP
                    new_ip = message.get("ip")
                    if new_ip and worker_id in connected_workers:
                        print(f"Worker '{worker_id}' self-reported (HTTP-based) public IP: {new_ip}. Storing as 'public_ip'.")
                        connected_workers[worker_id]["public_ip"] = new_ip 
                    elif not new_ip:
                        print(f"Worker '{worker_id}' sent register_public_ip message without an IP.")
                    # else: worker may have disconnected before IP update processed
                
                # NEW: Handle worker reporting its discovered UDP endpoint
                elif msg_type == "update_udp_endpoint":
                    udp_ip = message.get("udp_ip")
                    udp_port = message.get("udp_port")
                    if udp_ip and udp_port and worker_id in connected_workers:
                        connected_workers[worker_id]["udp_ip"] = udp_ip
                        connected_workers[worker_id]["udp_port"] = int(udp_port) # Ensure port is int
                        print(f"Worker '{worker_id}' updated UDP endpoint to: {udp_ip}:{udp_port}")
                        # Optionally, send an ack back to the worker
                        await websocket.send_text(json.dumps({"type": "udp_endpoint_ack", "status": "success"}))
                    else:
                        print(f"Worker '{worker_id}' sent incomplete update_udp_endpoint message.")
                        await websocket.send_text(json.dumps({"type": "udp_endpoint_ack", "status": "error", "detail": "Missing IP or Port"}))
                
                elif msg_type == "echo_request": # From previous gist
                    payload = message.get("payload", "")
                    response_payload_dict = {
                        "type": "echo_response",
                        "original_payload": payload,
                        "processed_by_rendezvous": f"Rendezvous processed: '{payload.upper()}' for worker {worker_id}"
                    }
                    await websocket.send_text(json.dumps(response_payload_dict))
                    print(f"Sent echo_response back to worker '{worker_id}'")
                
                elif msg_type == "get_my_details": # From previous gist
                    if worker_id in connected_workers:
                        details = connected_workers[worker_id]
                        response_payload_dict = {
                            "type": "my_details_response",
                            "worker_id": worker_id,
                            "registered_ip": details["public_ip"],
                            "registered_port": details["public_port"],
                            "message": "These are your details as seen by the Rendezvous service."
                        }
                        await websocket.send_text(json.dumps(response_payload_dict))
                        print(f"Sent 'my_details_response' back to worker '{worker_id}'")
                    else:
                        await websocket.send_text(json.dumps({"type": "error", "message": "Could not find your details."}))

                else:
                    print(f"Worker '{worker_id}' sent unhandled message type: {msg_type}")

            except json.JSONDecodeError:
                print(f"Worker '{worker_id}' sent non-JSON message: {raw_data}")
            except AttributeError: 
                print(f"Worker '{worker_id}' sent malformed JSON message: {raw_data}")
            except KeyError:
                 print(f"Worker '{worker_id}' no longer in connected_workers dictionary, could not process message.")


    except WebSocketDisconnect:
        print(f"Worker '{worker_id}' disconnected from WebSocket EP: {client_host}:{client_port}.")
    except Exception as e:
        print(f"Error with worker '{worker_id}' WebSocket: {e}")
    finally:
        if worker_id in connected_workers and connected_workers[worker_id]["websocket"] == websocket:
            del connected_workers[worker_id]
            print(f"Worker '{worker_id}' de-registered. Total workers: {len(connected_workers)}")

@app.get("/")
async def read_root():
    return {"message": "Rendezvous Service is running. Connect via WebSocket at /ws/register/{worker_id}"}


@app.get("/debug/list_workers")
async def list_workers():
    workers_info = {}
    for worker_id_key, data_val in list(connected_workers.items()): # Use list() for safe iteration if dict changes
        ws_object = data_val.get("websocket")
        current_client_state_value = None
        is_connected = False
        if ws_object and hasattr(ws_object, 'client_state'):
            current_client_state = ws_object.client_state
            current_client_state_value = current_client_state.value
            is_connected = (current_client_state_value == 1) # WebSocketState.CONNECTED.value (1)
            # Removed verbose print from here, but kept the logic
        
        workers_info[worker_id_key] = {
            "websocket_observed_ip": data_val.get("public_ip"),    # Renamed for clarity
            "websocket_observed_port": data_val.get("public_port"),# Renamed for clarity
            "stun_reported_udp_ip": data_val.get("udp_ip"),      # UDP IP reported by worker after STUN
            "stun_reported_udp_port": data_val.get("udp_port"),  # UDP Port reported by worker after STUN
            "websocket_connected": is_connected,
            "websocket_raw_state": current_client_state_value
        }
    return {"connected_workers_count": len(workers_info), "workers": workers_info}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080))) 