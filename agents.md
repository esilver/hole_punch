# Repository Agents

This document outlines the primary software "agents" or services defined within this repository and their core responsibilities. These agents work together to establish and manage peer-to-peer (P2P) connections for data exchange.

## 1. Rendezvous Service

-   **Location:** `rendezvous_service_code/`
-   **Purpose:** Acts as a central discovery and signaling mechanism for Worker services, facilitating the initial introduction between peers.
-   **Key Responsibilities:**
    -   **Worker Registration:** Accepts WebSocket connections from Worker services (`/ws/register/{worker_id}`). Upon connection, it observes the worker's public IP address (from the WebSocket connection) and stores it.
    -   **UDP Endpoint Management:** Receives and stores STUN-discovered public UDP IP addresses and ports from each worker (`update_udp_endpoint` message). It also accepts self-reported HTTP-derived public IPs (`register_public_ip`).
    -   **Peer Pairing:** Identifies pairs of registered workers that have valid UDP endpoints and active WebSocket connections.
    -   **Connection Offering:** Sends `p2p_connection_offer` messages (via WebSocket) to the selected pairs, providing each worker with the necessary UDP endpoint information and ID of its peer to initiate a P2P connection attempt.
    -   **Status & Debugging:** Provides a debug endpoint (`/debug/list_workers`) to list currently connected workers, their observed IPs, STUN-reported UDP endpoints, and WebSocket connection status.
    -   **Deregistration:** Handles explicit deregistration requests from workers (`explicit_deregister`) and cleans up upon WebSocket disconnection.
-   **Primary Technologies:**
    -   Python
    -   FastAPI (for WebSocket and HTTP endpoints)
    -   Uvicorn (ASGI server)
    -   WebSockets

## 2. Worker Service

-   **Location:** Root directory (e.g., `main.py`, `quic_tunnel.py`, `p2p_protocol.py`, etc.)
-   **Purpose:** Represents an individual peer in the P2P network. It discovers its public network address, registers with the Rendezvous Service, connects with other peers, and facilitates data exchange via UDP and QUIC. It embodies several "sub-agent" functionalities.
-   **Key Responsibilities & Sub-Agents:**

    ### 2.1. Rendezvous Client Agent
    -   **Purpose:** Manages communication and registration with the Rendezvous Service.
    -   **Functionality:**
        -   Generates a unique `worker_id`.
        -   Connects to the Rendezvous Service via WebSocket using the URL provided by the `RENDEZVOUS_SERVICE_URL` environment variable.
        -   Sends its self-identified HTTP public IP (obtained from a service like `ipify.org`) to the Rendezvous Service.
        -   Sends its STUN-discovered public UDP IP and port to the Rendezvous Service.
        -   Listens for `p2p_connection_offer` messages from the Rendezvous Service to obtain peer details.
        -   Handles WebSocket keep-alives (pings/pongs) and implements reconnection logic.
        -   Can send an `explicit_deregister` message before shutting down.

    ### 2.2. STUN/NAT Traversal Agent
    -   **Purpose:** Discovers the worker's public-facing UDP endpoint as seen from the internet (especially when behind NAT).
    -   **Functionality:**
        -   Creates a local UDP socket (typically bound to `0.0.0.0` on `INTERNAL_UDP_PORT`).
        -   Queries a public STUN server (e.g., `stun.l.google.com:19302`, configurable via `STUN_HOST`/`STUN_PORT` env vars) to determine its external (NAT-translated) IP address and port for the UDP Fsocket.
        -   Provides this discovered UDP endpoint to the "Rendezvous Client Agent" for registration.
        -   Can periodically re-check its STUN mapping and update the Rendezvous service if it changes.

    ### 2.3. P2P UDP Agent (Hole Punching & Basic Data)
    -   **Purpose:** Establishes direct UDP communication links with peer workers and handles basic UDP-based data exchange.
    -   **Functionality:**
        -   Listens for incoming UDP datagrams on the `INTERNAL_UDP_PORT` (the same port whose external mapping was discovered via STUN). This is managed by `P2PUDPProtocol`.
        -   Upon receiving a `p2p_connection_offer`, initiates UDP hole punching by sending a burst of UDP "ping" messages (`P2P_HOLE_PUNCH_PING_FROM_`) to the peer's STUN-discovered public UDP endpoint.
        -   Processes incoming P2P UDP messages, differentiating between plain JSON chat messages (`chat_message`), P2P ping requests/responses (`p2p_ping_request`, `p2p_ping_response`), and raw binary data for benchmarks.
        -   Sends outgoing P2P UDP chat messages and benchmark data directly to the peer's NAT address.
        -   Manages multiple benchmark sessions simultaneously based on peer address.

    ### 2.4. QUIC Agent (P2P Tunneling)
    -   **Purpose:** Establishes a secure and reliable QUIC tunnel over the established P2P UDP connection for advanced data exchange.
    -   **Functionality:**
        -   Determines its role (client or server) for the QUIC handshake based on a comparison of worker IDs with its peer.
        -   **Client Role:** Initiates a QUIC connection to the peer's public UDP endpoint.
        -   **Server Role:** Listens for incoming QUIC Initial packets on its main UDP port and establishes a QUIC connection.
        -   Manages the QUIC handshake process, including certificate handling (uses `cert.pem`/`key.pem` or generates self-signed certificates if they are not present, primarily for the server role).
        -   Handles QUIC connection events: `HandshakeCompleted`, `StreamDataReceived`, `ConnectionTerminated`, `PingAcknowledged`.
        -   Manages QUIC streams for sending and receiving application-specific data (e.g., `QUIC_CHAT_MESSAGE`, `QUIC_ECHO_REQUEST`).
        -   Uses `aioquic`'s internal PING frames for keep-alive over the QUIC connection.
        -   Notifies the UI about QUIC connection status changes.
        -   Can gracefully close the QUIC tunnel.

    ### 2.5. Web UI Server Agent
    -   **Purpose:** Provides an interactive web interface for users to monitor worker status, send/receive P2P messages (UDP and QUIC), and trigger administrative actions.
    -   **Functionality:**
        -   Serves the `index.html` file at the root path (`/`).
        -   Hosts a WebSocket server at the `/ui_ws` path on the main HTTP port (`$PORT`).
        -   Communicates with the connected browser client (JavaScript in `index.html`):
            -   Sends initialization info (`init_info`) to the UI, including the worker ID and current P2P peer ID.
            -   Receives commands from the UI such as `send_p2p_message`, `send_quic_message`, `p2p_ping_command`, `quic_echo_command`, `start_benchmark_send`, `restart_worker_request`, `redo_stun_request`.
            -   Broadcasts received P2P data (UDP chat, QUIC chat, P2P ping results, QUIC echo results), P2P status updates, QUIC status updates, and benchmark progress/results to all connected UI clients.
        -   Provides a `/health` endpoint for Cloud Run health checks.

    ### 2.6. Benchmark Agent
    -   **Purpose:** Measures P2P UDP throughput between worker instances.
    -   **Functionality:**
        -   **Sender Side:** Upon UI command (`start_benchmark_send`), downloads a specified file (from `BENCHMARK_GCS_URL`). Chunks the file and sends it over the P2P UDP connection to the peer. Reports progress and completion to the UI.
        -   **Receiver Side (within P2P UDP Agent):** Receives benchmark data chunks, tracks received bytes and chunks, calculates throughput, and reports status (via broadcast to its UI clients, which the initiating sender's UI also listens to indirectly if the peer is also open in a browser). Uses a special end-marker packet (0xFFFFFFFF) to signify completion.

-   **Primary Technologies:**
    -   Python
    -   `asyncio` (core concurrency model)
    -   `websockets` (for Rendezvous client and local UI server)
    -   `aioquic` (for QUIC tunneling)
    -   `pystun3` (for STUN NAT traversal)
    -   `requests` (for fetching public IP via ipify.org)
    -   `aiohttp` (for asynchronous GCS file download for benchmarks)
    -   `cryptography` (for QUIC certificate handling)
    -   JSON (for WebSocket and some P2P UDP messaging)
    -   HTML, JavaScript (for the client-side UI in `index.html`)

## Interactions Flow (Simplified)

1.  Multiple **Worker Services** start.
2.  Each Worker performs STUN to find its public UDP endpoint.
3.  Each Worker connects to the **Rendezvous Service** via WebSocket and registers its ID and UDP endpoint.
4.  The Rendezvous Service pairs two available Workers and sends each a `p2p_connection_offer` with the other's details.
5.  The Workers use the received peer UDP info to initiate UDP hole punching (sending PINGs).
6.  Once the UDP path is believed to be open:
    *   One Worker (determined by ID comparison) acts as the QUIC client and initiates a QUIC handshake to the peer.
    *   The other Worker acts as the QUIC server.
7.  If the QUIC handshake succeeds, a secure `QuicTunnel` is established.
8.  Users can interact via the Web UI (`index.html`) served by each Worker:
    *   Send chat messages over basic P2P UDP or over the QUIC tunnel.
    *   Initiate UDP benchmarks.
    *   Perform administrative actions like restarting the worker or re-triggering STUN.