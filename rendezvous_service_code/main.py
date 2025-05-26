"""
Rendezvous Service - Main Application
Facilitates P2P connections between workers
"""
import asyncio
import uvicorn
import os
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

# Import modules - using absolute imports for Docker compatibility
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent))

from models import ConnectionRequest
from worker_websocket import websocket_register_worker
from admin_websocket import admin_websocket_endpoint, chat_websocket_endpoint
from api_endpoints import (
    read_root, admin_ui, admin_chat_ui, list_workers, 
    api_list_workers, connect_workers
)

# Create FastAPI app
app = FastAPI(title="Rendezvous Service")

# Add CORS middleware for API access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify allowed origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register HTTP endpoints
app.get("/")(read_root)
app.get("/admin")(admin_ui)
app.get("/admin/chat")(admin_chat_ui)
app.get("/debug/list_workers")(list_workers)
app.get("/api/workers")(api_list_workers)
app.post("/api/connect")(connect_workers)

# Register WebSocket endpoints
@app.websocket("/ws/register/{worker_id}")
async def ws_register_worker(websocket: WebSocket, worker_id: str):
    await websocket_register_worker(websocket, worker_id)

@app.websocket("/ws/admin")
async def ws_admin(websocket: WebSocket):
    await admin_websocket_endpoint(websocket)

@app.websocket("/ws/chat/{admin_session_id}/{worker_id}")
async def ws_chat(websocket: WebSocket, admin_session_id: str, worker_id: str):
    await chat_websocket_endpoint(websocket, admin_session_id, worker_id)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="trace")