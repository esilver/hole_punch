# Rendezvous Service - Refactored Structure

This service facilitates P2P connections between workers and provides admin interfaces for monitoring and control.

## Project Structure

```
rendezvous_service_code/
├── main.py                 # Main FastAPI application and entry point
├── models.py              # Data models and shared state
├── worker_websocket.py    # WebSocket handler for worker connections
├── admin_websocket.py     # WebSocket handlers for admin UI and chat
├── api_endpoints.py       # REST API endpoints
├── pairing.py            # Worker pairing logic
├── broadcast.py          # Broadcasting utilities
├── admin.html            # Admin dashboard UI
├── admin-chat.html       # Admin chat interface
└── requirements.txt      # Python dependencies
```

## Module Descriptions

### main.py
- FastAPI application setup
- Route registration
- CORS configuration
- Application entry point

### models.py
- Pydantic models for API requests
- ServiceState class containing all global state
- Shared data structures

### worker_websocket.py
- Handles worker WebSocket connections
- Processes worker messages (UDP updates, chat responses)
- Manages worker registration/deregistration

### admin_websocket.py
- Admin UI WebSocket for real-time updates
- Chat WebSocket for admin-to-worker communication

### api_endpoints.py
- REST endpoints for worker listing
- Manual connection control
- Static file serving for admin UIs

### pairing.py
- Automatic worker pairing logic
- Manual pairing functionality
- Connection validation

### broadcast.py
- Utility functions for broadcasting to admin clients

## Key Features

1. **Worker Management**
   - Real-time worker status tracking
   - UDP endpoint discovery via STUN
   - Automatic and manual pairing

2. **Admin Dashboard** (`/admin`)
   - Live worker status
   - Manual connection control
   - Event logging

3. **Admin Chat** (`/admin/chat`)
   - Direct communication with workers
   - Real-time messaging
   - Message history

## API Endpoints

- `GET /` - Health check
- `GET /admin` - Admin dashboard
- `GET /admin/chat` - Chat interface
- `GET /api/workers` - List all workers
- `POST /api/connect` - Manually connect two workers
- `WS /ws/register/{worker_id}` - Worker WebSocket
- `WS /ws/admin` - Admin updates WebSocket
- `WS /ws/chat/{admin_session_id}/{worker_id}` - Chat WebSocket

## Running the Service

```bash
python main.py
```

Or with Docker:
```bash
docker build -f Dockerfile.rendezvous -t rendezvous-service .
docker run -p 8080:8080 rendezvous-service
```