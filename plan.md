# P2P UDP Communication with Web UI and Benchmark Tools

**Project ID:** `iceberg-eli` (Ensure your gcloud is configured for this project)

## Objective

This project demonstrates a peer-to-peer (P2P) communication system enabling two "worker" services, potentially behind NAT devices, to establish a direct UDP connection. Key features include:

1.  **Rendezvous Service:** A central server that workers connect to for registration and peer discovery.
2.  **Worker Service:**
    *   Registers with the Rendezvous service.
    *   Uses **STUN** to discover its public UDP IP address and port.
    *   Reports this UDP endpoint to the Rendezvous service.
    *   Receives connection offers for a peer from the Rendezvous service.
    *   Performs **UDP hole punching** to establish a direct P2P UDP link with the peer.
    *   Provides a **web interface (UI)** served directly by the worker, allowing users to:
        *   Send and receive chat messages with the connected peer over the P2P UDP link.
        *   Initiate and monitor a **UDP throughput benchmark** between peers.
    *   Maintains P2P connections using periodic **keep-alive messages**.
3.  **Web UI:** An HTML/JavaScript interface for chat and benchmark control, communicating with its worker backend via a local WebSocket connection.

The system is designed with cloud deployment (e.g., Google Cloud Run) in mind, where workers might be behind NAT managed by the cloud provider (like Cloud NAT when using VPC egress).

## Core Components

### 1. Worker Service (`main.py`)

*   **Unique ID:** Generates a UUID on startup.
*   **Rendezvous Client:** Connects to the Rendezvous Service via WebSocket to register, report its public HTTP-derived IP, and later its STUN-discovered UDP endpoint. Receives P2P connection offers.
*   **STUN Client:** Queries a STUN server to discover its external IP address and port for its local UDP socket. Includes retry mechanisms.
*   **P2P UDP Agent (`P2PUDPProtocol`):**
    *   Listens for incoming UDP packets from the peer on a configured internal port.
    *   Sends UDP packets (chat, benchmark data, keep-alives) to the connected peer.
    *   Handles JSON-formatted messages for chat and benchmark data.
*   **HTTP & UI WebSocket Server:**
    *   Serves the `index.html` web UI.
    *   Provides a `/health` check endpoint.
    *   Runs a WebSocket server (`/ui_ws`) for real-time communication with the local `index.html` (sending user messages to the backend, relaying P2P messages and status updates to the UI).
*   **UDP Hole Punching Logic:** Initiates UDP packet sending to a peer upon receiving a connection offer.
*   **P2P Keep-Alives:** Periodically sends UDP keep-alive messages to the connected peer.
*   **Benchmark Functionality:**
    *   Sends/receives benchmark data chunks over P2P UDP.
    *   Calculates and reports throughput.

### 2. Rendezvous Service (`rendezvous_service_code/main.py`)

*   **Worker Registration:** Accepts WebSocket connections from workers at `/ws/register/{worker_id}`.
*   **Endpoint Storage:** Stores information about each connected worker, including:
    *   WebSocket-observed IP and port.
    *   Self-reported HTTP-based public IP (optional).
    *   STUN-discovered UDP IP and port.
*   **Peer Pairing:** When at least two workers have registered their UDP endpoints, it attempts to pair them by sending each worker a `p2p_connection_offer` message containing the other's STUN UDP endpoint details.
*   **Debug Endpoint:** Provides a `/debug/list_workers` HTTP endpoint to view currently connected workers and their status.

## Prerequisites

1.  **Google Cloud SDK (gcloud):** Ensure `gcloud` is installed, authenticated, and configured with your project (`iceberg-eli`) and a default region (e.g., `us-central1`).
2.  **Docker:** Required for building and containerizing the services.
3.  **Enabled APIs (Google Cloud):** Ensure the following APIs are enabled in your project:
    *   `cloudbuild.googleapis.com`
    *   `run.googleapis.com`
    *   `artifactregistry.googleapis.com`
    *   (And `compute.googleapis.com` if managing VPC, subnets, NAT manually as prerequisite, though this plan focuses on the application logic).

## Main Project Files

The project is structured with the Worker service in the root directory and the Rendezvous service in `rendezvous_service_code/`.

*   **Worker Service (Root Directory):**
    *   `main.py`: Core Python script for the worker service.
    *   `index.html`: Web UI for chat and benchmarking.
    *   `Dockerfile.worker`: Dockerfile for building the worker service image.
    *   `requirements.txt`: Python dependencies for the worker (e.g., `websockets`, `requests`, `pystun3`).
*   **Rendezvous Service (`rendezvous_service_code/` directory):**
    *   `main.py`: Core Python script for the rendezvous service (using FastAPI).
    *   `Dockerfile.rendezvous`: Dockerfile for building the rendezvous service image.
    *   `requirements.txt`: Python dependencies for the rendezvous service (e.g., `fastapi`, `uvicorn`).
*   **Deployment & Environment:**
    *   `.envrc`: (If using direnv) For setting environment variables locally.
    *   `rebuild_and_deploy.sh`: Example shell script for automating build and deployment.

**Note on Detailed Steps & Deployment:**

This `plan.md` provides a high-level overview. The subsequent `plann_stepX.md` files in this workspace detail the iterative development steps, including more specific code snippets (though these may also need updating to match the final codebase) and deployment commands for each stage. For the most up-to-date and detailed understanding of the code, refer to the actual source files listed above.
