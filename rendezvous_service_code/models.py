"""
Data models and shared state for the rendezvous service
"""
from pydantic import BaseModel
from typing import Dict, List, Optional
from fastapi import WebSocket


# Pydantic models for API requests
class ConnectionRequest(BaseModel):
    worker_a_id: str
    worker_b_id: str


class ChatMessage(BaseModel):
    content: str
    worker_id: str


# Global state containers
class ServiceState:
    """Container for all service state"""
    def __init__(self):
        # connected_workers structure:
        # { worker_id: { "websocket_observed_ip": ..., "websocket_observed_port": ...,
        #                "websocket": WebSocket, 
        #                "stun_reported_udp_ip": ..., "stun_reported_udp_port": ...,
        #                "http_reported_public_ip": ... # Optional
        #              }}
        self.connected_workers: Dict[str, Dict] = {}
        
        # List of worker_ids that have reported UDP info and are waiting for a peer
        self.workers_ready_for_pairing: List[str] = []
        
        # Admin UI WebSocket clients
        self.admin_websocket_clients: List[WebSocket] = []
        
        # Chat sessions: admin_id -> worker_id -> websocket
        self.chat_sessions: Dict[str, Dict[str, WebSocket]] = {}


# Create a global instance
service_state = ServiceState()