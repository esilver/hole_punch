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
                
                elif msg_type == "explicit_deregister":
                    requesting_worker_id = message.get("worker_id")
                    if requesting_worker_id == worker_id: # Ensure the message is from the correct worker
                        print(f"Rendezvous: Worker '{worker_id}' requested explicit deregistration.")
                        # Perform cleanup immediately
                        if worker_id in connected_workers:
                            del connected_workers[worker_id]
                            print(f"Rendezvous: Worker '{worker_id}' explicitly de-registered (from connected_workers). Total active: {len(connected_workers)}")
                        if worker_id in workers_ready_for_pairing:
                            workers_ready_for_pairing.remove(worker_id)
                            print(f"Rendezvous: Worker '{worker_id}' explicitly de-registered (from workers_ready_for_pairing). Pairing list size: {len(workers_ready_for_pairing)}")
                        
                        try:
                            await websocket.send_text(json.dumps({"type": "deregister_ack", "worker_id": worker_id, "status": "success"}))
                        except Exception as e_ack:
                            print(f"Rendezvous: Error sending deregister_ack to '{worker_id}': {e_ack}")
                        
                        # Close connection from server side after explicit deregister
                        await websocket.close(code=1000, reason="Worker explicitly deregistered")
                        print(f"Rendezvous: Closed WebSocket for '{worker_id}' after explicit deregistration.")
                        break # Exit message loop, leading to the finally block for this WebSocket
                    else:
                        print(f"Rendezvous: Received explicit_deregister from '{worker_id}' with mismatched ID in payload: '{requesting_worker_id}'. Ignoring.")

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
    return {"message": "Rendezvous Service is running. Test."}

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