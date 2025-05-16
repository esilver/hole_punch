import asyncio
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from typing import Dict, Optional, List, Tuple
import os
import json

app = FastAPI(title="Rendezvous Service")

# connected_workers structure from Step 3A:
# { worker_id: { "websocket_observed_ip": ..., "websocket_observed_port": ...,
#                "websocket": WebSocket, 
#                "stun_reported_udp_ip": ..., "stun_reported_udp_port": ...,
#                "http_reported_public_ip": ... # Optional, if you keep it
#              }}
connected_workers: Dict[str, Dict] = {}

# List of worker_ids that have reported UDP info and are waiting for a peer
workers_ready_for_pairing: List[str] = []

async def attempt_to_pair_workers(newly_ready_worker_id: str):
    global workers_ready_for_pairing, connected_workers

    # Ensure the worker trying to pair is valid
    if newly_ready_worker_id not in connected_workers or \
       not connected_workers[newly_ready_worker_id].get("stun_reported_udp_ip"):
        print(f"Pairing: Worker '{newly_ready_worker_id}' not in connected_workers or no UDP info. Cannot initiate pairing.")
        if newly_ready_worker_id in workers_ready_for_pairing:
            workers_ready_for_pairing.remove(newly_ready_worker_id)
        return

    # Add the new worker to the ready list if not already present
    if newly_ready_worker_id not in workers_ready_for_pairing:
        workers_ready_for_pairing.append(newly_ready_worker_id)
        print(f"Rendezvous: Worker '{newly_ready_worker_id}' added to ready_for_pairing list. Current list: {workers_ready_for_pairing}")

    # Attempt to find a pair if there are at least two workers ready
    if len(workers_ready_for_pairing) < 2:
        print(f"Rendezvous: Not enough workers ready for pairing ({len(workers_ready_for_pairing)}). Waiting for more.")
        return

    # Take the first two distinct workers from the list for pairing
    # This is a simple strategy; more complex ones could be used (e.g., oldest, random)
    peer_a_id = None
    peer_b_id = None

    temp_ready_list = list(workers_ready_for_pairing) # Iterate over a copy
    for i in range(len(temp_ready_list)):
        potential_a = temp_ready_list[i]
        if potential_a in connected_workers and connected_workers[potential_a].get("stun_reported_udp_ip"):
            for j in range(i + 1, len(temp_ready_list)):
                potential_b = temp_ready_list[j]
                if potential_b in connected_workers and connected_workers[potential_b].get("stun_reported_udp_ip"):
                    peer_a_id = potential_a
                    peer_b_id = potential_b
                    break
            if peer_a_id and peer_b_id: # Found a pair
                break
        else: # Clean up stale entry from original list
            if potential_a in workers_ready_for_pairing:
                 workers_ready_for_pairing.remove(potential_a)


    if peer_a_id and peer_b_id:
        # Found a pair! Remove both from the ready list.
        workers_ready_for_pairing.remove(peer_a_id)
        workers_ready_for_pairing.remove(peer_b_id)
        print(f"Rendezvous: Pairing Worker '{peer_a_id}' with Worker '{peer_b_id}'. Updated ready list: {workers_ready_for_pairing}")

        peer_a_data = connected_workers.get(peer_a_id)
        peer_b_data = connected_workers.get(peer_b_id)

        if not (peer_a_data and peer_b_data and 
                peer_a_data.get("stun_reported_udp_ip") and peer_a_data.get("stun_reported_udp_port") and
                peer_b_data.get("stun_reported_udp_ip") and peer_b_data.get("stun_reported_udp_port") and
                peer_a_data.get("websocket") and peer_b_data.get("websocket")):
            print(f"Pairing Error: Data integrity issue for pairing {peer_a_id} and {peer_b_id}. One might have disconnected or lost info.")
            # Re-add valid ones if they were prematurely removed, or let them re-register
            return

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
            if hasattr(peer_a_ws, 'client_state') and peer_a_ws.client_state.value == 1:
                await peer_a_ws.send_text(json.dumps(offer_to_a_payload))
                print(f"Rendezvous: Sent connection offer to Worker '{peer_a_id}' (for peer '{peer_b_id}').")
            else:
                print(f"Rendezvous: Worker '{peer_a_id}' WebSocket not open. Cannot send offer.")

            # Send A's info to B
            if hasattr(peer_b_ws, 'client_state') and peer_b_ws.client_state.value == 1:
                await peer_b_ws.send_text(json.dumps(offer_to_b_payload))
                print(f"Rendezvous: Sent connection offer to Worker '{peer_b_id}' (for peer '{peer_a_id}').")
            else:
                print(f"Rendezvous: Worker '{peer_b_id}' WebSocket not open. Cannot send offer.")
        except Exception as e:
            print(f"Rendezvous: Error sending P2P connection offers: {e}")
    else:
        print(f"Rendezvous: No suitable peer found in ready_for_pairing list for newly ready worker '{newly_ready_worker_id}'.")

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
                 try: await old_ws.close(code=1000, reason="New connection from same worker ID")
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
        if worker_id in workers_ready_for_pairing: 
            workers_ready_for_pairing.remove(worker_id)
            print(f"Worker '{worker_id}' removed from pending pairing list due to disconnect.")

@app.get("/")
async def read_root():
    return {"message": "Rendezvous Service is running."}

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

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), log_level="trace") 