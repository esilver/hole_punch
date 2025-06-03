"""
REST API endpoints for the rendezvous service
"""
import asyncio
from pathlib import Path
from fastapi import HTTPException
from fastapi.responses import HTMLResponse

from models import service_state, ConnectionRequest
from pairing import manual_pair_workers
from broadcast import broadcast_to_admin_clients


async def read_root():
    """Root endpoint"""
    return {"message": "Rendezvous Service is running. Test."}


async def admin_ui():
    """Serve the admin UI HTML page"""
    admin_html_path = Path(__file__).parent / "admin.html"
    if admin_html_path.exists():
        with open(admin_html_path, "r") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content)
    else:
        return HTMLResponse(content="<h1>Admin UI not found</h1>", status_code=404)


async def admin_chat_ui():
    """Serve the admin chat UI HTML page"""
    chat_html_path = Path(__file__).parent / "admin-chat.html"
    if chat_html_path.exists():
        with open(chat_html_path, "r") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content)
    else:
        return HTMLResponse(content="<h1>Admin Chat UI not found</h1>", status_code=404)


async def list_workers():
    """Debug endpoint for listing workers"""
    workers_info = {}
    for worker_id_key, data_val in list(service_state.connected_workers.items()):
        ws_object = data_val.get("websocket")
        is_connected = False
        if ws_object and hasattr(ws_object, 'client_state') and ws_object.client_state.value == 1:
            is_connected = True
        
        workers_info[worker_id_key] = {
            "websocket_observed_ip": data_val.get("websocket_observed_ip"),
            "websocket_observed_port": data_val.get("websocket_observed_port"),
            "stun_reported_udp_ip": data_val.get("stun_reported_udp_ip"),
            "stun_reported_udp_port": data_val.get("stun_reported_udp_port"),
            "websocket_connected": is_connected
        }
    return {
        "connected_workers_count": len(workers_info), 
        "workers": workers_info, 
        "ready_for_pairing_count": len(service_state.workers_ready_for_pairing), 
        "ready_list": service_state.workers_ready_for_pairing
    }


async def api_list_workers():
    """API endpoint for listing all connected workers with their status"""
    workers_list = []
    for worker_id, data in service_state.connected_workers.items():
        ws_object = data.get("websocket")
        is_connected = False
        if ws_object and hasattr(ws_object, 'client_state') and ws_object.client_state.value == 1:
            is_connected = True
        
        # Check if worker has valid UDP endpoint
        has_udp = bool(data.get("stun_reported_udp_ip") and data.get("stun_reported_udp_port"))
        
        # Check if worker is ready for pairing
        is_ready = worker_id in service_state.workers_ready_for_pairing
        
        workers_list.append({
            "worker_id": worker_id,
            "websocket_connected": is_connected,
            "has_udp_endpoint": has_udp,
            "ready_for_pairing": is_ready,
            "websocket_ip": data.get("websocket_observed_ip"),
            "websocket_port": data.get("websocket_observed_port"),
            "udp_ip": data.get("stun_reported_udp_ip"),
            "udp_port": data.get("stun_reported_udp_port")
        })
    
    return {
        "workers": workers_list,
        "total_count": len(workers_list),
        "connected_count": sum(1 for w in workers_list if w["websocket_connected"]),
        "ready_count": len(service_state.workers_ready_for_pairing)
    }


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