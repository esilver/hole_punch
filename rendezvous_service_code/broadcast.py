"""
Broadcasting utilities for admin clients
"""
import json
from typing import Dict, Any

from models import service_state


async def broadcast_to_admin_clients(message: Dict[str, Any]):
    """Send a message to all connected admin UI WebSocket clients"""
    if service_state.admin_websocket_clients:
        # Create a copy to avoid modification during iteration
        clients_copy = service_state.admin_websocket_clients.copy()
        for client in clients_copy:
            try:
                await client.send_text(json.dumps(message))
            except Exception as e:
                print(f"Error broadcasting to admin client: {e}")
                # Remove disconnected client
                if client in service_state.admin_websocket_clients:
                    service_state.admin_websocket_clients.remove(client)