"""
Worker pairing logic for P2P connections
"""
import asyncio
import json
from typing import Optional
from fastapi import HTTPException

from models import service_state


async def attempt_to_pair_workers(newly_ready_worker_id: str):
    """Attempt to pair a newly ready worker with another available worker"""
    # Ensure the worker trying to pair is valid and has WebSocket
    newly_ready_worker_data = service_state.connected_workers.get(newly_ready_worker_id)
    if not (newly_ready_worker_data and newly_ready_worker_data.get("stun_reported_udp_ip") and 
            newly_ready_worker_data.get("websocket") and 
            hasattr(newly_ready_worker_data["websocket"], 'client_state') and 
            newly_ready_worker_data["websocket"].client_state.value == 1):
        print(f"Pairing: Newly ready worker '{newly_ready_worker_id}' is not valid, lacks UDP info, or WebSocket is disconnected. Cannot initiate pairing.")
        if newly_ready_worker_id in service_state.workers_ready_for_pairing:
            service_state.workers_ready_for_pairing.remove(newly_ready_worker_id)
        # Also ensure it's removed from connected_workers if its WebSocket is truly gone or invalid
        if newly_ready_worker_id in service_state.connected_workers and (not newly_ready_worker_data or not newly_ready_worker_data.get("websocket") or 
                                                        not hasattr(newly_ready_worker_data.get("websocket"), 'client_state') or
                                                        newly_ready_worker_data.get("websocket").client_state.value != 1):
            del service_state.connected_workers[newly_ready_worker_id]
            print(f"Cleaned up disconnected newly_ready_worker_id '{newly_ready_worker_id}' from connected_workers.")
        return

    if newly_ready_worker_id not in service_state.workers_ready_for_pairing:
        service_state.workers_ready_for_pairing.append(newly_ready_worker_id)
        print(f"Rendezvous: Worker '{newly_ready_worker_id}' added to ready_for_pairing list. Current list: {service_state.workers_ready_for_pairing}")

    if len(service_state.workers_ready_for_pairing) < 2:
        print(f"Rendezvous: Not enough workers ready for pairing ({len(service_state.workers_ready_for_pairing)}). Waiting for more.")
        return

    peer_a_id = None
    peer_b_id = None
    
    # Create a copy of the list to iterate over, allowing modification of the original
    candidate_ids = list(service_state.workers_ready_for_pairing)
    
    # Expand candidate list to include other connected workers that have
    # valid UDP info and active WebSocket
    extra_candidates = [wid for wid, wdata in service_state.connected_workers.items()
                        if wid not in service_state.workers_ready_for_pairing
                        and wid != newly_ready_worker_id
                        and wdata.get("stun_reported_udp_ip")
                        and wdata.get("stun_reported_udp_port")
                        and wdata.get("websocket")
                        and hasattr(wdata["websocket"], 'client_state')
                        and wdata["websocket"].client_state.value == 1]
    if extra_candidates:
        print(f"Rendezvous: Found {len(extra_candidates)} additional connected worker(s) with UDP info that were not in ready list: {extra_candidates}")
        candidate_ids.extend(extra_candidates)
    
    for worker_id in candidate_ids:
        worker_data = service_state.connected_workers.get(worker_id)
        # Check if worker is still valid and its WebSocket is connected
        if not (worker_data and worker_data.get("stun_reported_udp_ip") and 
                worker_data.get("websocket") and 
                hasattr(worker_data["websocket"], 'client_state') and 
                worker_data["websocket"].client_state.value == 1):
            
            print(f"Pairing: Worker '{worker_id}' in ready_list is stale or disconnected. Removing.")
            if worker_id in service_state.workers_ready_for_pairing:
                service_state.workers_ready_for_pairing.remove(worker_id)
            if worker_id in service_state.connected_workers:
                current_ws_state_in_dict = service_state.connected_workers[worker_id].get("websocket")
                if not current_ws_state_in_dict or not hasattr(current_ws_state_in_dict, 'client_state') or current_ws_state_in_dict.client_state.value != 1 :
                    del service_state.connected_workers[worker_id]
                    print(f"Cleaned up stale worker '{worker_id}' from connected_workers during pairing attempt.")
            continue

        # Worker is valid, try to find a peer for it
        if peer_a_id is None:
            peer_a_id = worker_id
        elif peer_a_id != worker_id:
            peer_b_id = worker_id
            break

    if peer_a_id and peer_b_id:
        # Selected pair: peer_a_id, peer_b_id
        print(f"Rendezvous: Attempting to pair Worker '{peer_a_id}' with Worker '{peer_b_id}'.")

        peer_a_data = service_state.connected_workers.get(peer_a_id)
        peer_b_data = service_state.connected_workers.get(peer_b_id)

        # Final check for data integrity and WebSocket state before sending offers
        if not (peer_a_data and peer_b_data and
                peer_a_data.get("stun_reported_udp_ip") and peer_a_data.get("stun_reported_udp_port") and
                peer_b_data.get("stun_reported_udp_ip") and peer_b_data.get("stun_reported_udp_port") and
                peer_a_data.get("websocket") and hasattr(peer_a_data["websocket"], 'client_state') and peer_a_data["websocket"].client_state.value == 1 and
                peer_b_data.get("websocket") and hasattr(peer_b_data["websocket"], 'client_state') and peer_b_data["websocket"].client_state.value == 1):
            
            print(f"Pairing Error: Post-selection data integrity or WebSocket issue for {peer_a_id} or {peer_b_id}. Aborting this pair.")
            for p_id in [peer_a_id, peer_b_id]:
                p_data = service_state.connected_workers.get(p_id)
                if p_data and (not p_data.get("websocket") or not hasattr(p_data["websocket"], 'client_state') or p_data["websocket"].client_state.value != 1):
                    if p_id in service_state.workers_ready_for_pairing: 
                        service_state.workers_ready_for_pairing.remove(p_id)
                    if p_id in service_state.connected_workers: 
                        del service_state.connected_workers[p_id]
                    print(f"Cleaned up disconnected peer '{p_id}' after failed pairing integrity check.")
            return

        # If all checks passed, remove from ready list and send offers
        if peer_a_id in service_state.workers_ready_for_pairing: 
            service_state.workers_ready_for_pairing.remove(peer_a_id)
        if peer_b_id in service_state.workers_ready_for_pairing: 
            service_state.workers_ready_for_pairing.remove(peer_b_id)
        print(f"Rendezvous: Removed '{peer_a_id}' and '{peer_b_id}' from ready_for_pairing. Updated list: {service_state.workers_ready_for_pairing}")

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
            await peer_a_ws.send_text(json.dumps(offer_to_a_payload))
            print(f"Rendezvous: Sent connection offer to Worker '{peer_a_id}' (for peer '{peer_b_id}').")

            await peer_b_ws.send_text(json.dumps(offer_to_b_payload))
            print(f"Rendezvous: Sent connection offer to Worker '{peer_b_id}' (for peer '{peer_a_id}').")
        except Exception as e:
            print(f"Rendezvous: Error sending P2P connection offers: {e}")
    else:
        print(f"Rendezvous: No suitable peer found in ready_for_pairing list for newly ready worker '{newly_ready_worker_id}'.")


async def manual_pair_workers(worker_a_id: str, worker_b_id: str) -> bool:
    """Manually pair two specific workers. Returns True if successful."""
    # Get worker data
    worker_a_data = service_state.connected_workers.get(worker_a_id)
    worker_b_data = service_state.connected_workers.get(worker_b_id)
    
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