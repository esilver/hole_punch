# Trino over P2P UDP Tunnel Setup

This guide explains how to run Trino workers that communicate over the P2P UDP tunnel.

## Environment Variables

Set these environment variables to enable Trino mode:

```bash
# Enable Trino mode
export TRINO_MODE=true

# Port where local Trino worker runs
export TRINO_LOCAL_PORT=8081

# Port where HTTP proxy listens (for Trino to connect to)
export TRINO_PROXY_PORT=8080

# Worker ID of the coordinator (optional, for multi-worker setups)
export TRINO_COORDINATOR_ID=<coordinator-worker-id>
```

## Architecture

```
┌─────────────────────────┐     ┌─────────────────────────┐
│   Cloud Run Worker A    │     │   Cloud Run Worker B    │
│                         │     │                         │
│  ┌─────────────────┐    │     │    ┌─────────────────┐  │
│  │  Trino Worker   │    │     │    │  Trino Worker   │  │
│  │  localhost:8081 │    │     │    │  localhost:8081 │  │
│  └────────┬────────┘    │     │    └────────┬────────┘  │
│           │             │     │             │           │
│  ┌────────▼────────┐    │     │    ┌────────▼────────┐  │
│  │  HTTP Proxy     │◄───┼─────┼───►│  HTTP Proxy     │  │
│  │  localhost:8080 │    │ UDP │    │  localhost:8080 │  │
│  └─────────────────┘    │     │    └─────────────────┘  │
└─────────────────────────┘     └─────────────────────────┘
```

## How It Works

1. **Local Routing**: Paths like `/v1/task`, `/v1/info`, `/v1/status` are routed to the local Trino worker
2. **Remote Routing**: Other paths (like `/v1/statement`) are tunneled to the peer worker via UDP
3. **Discovery**: The proxy provides `/v1/info` and `/v1/announcement` endpoints for Trino discovery

## Trino Configuration

Configure your Trino worker to:
1. Listen on `localhost:8081` (or your configured `TRINO_LOCAL_PORT`)
2. Use `http://localhost:8080` as the discovery URI
3. Set `http-server.http.port=8081` in `config.properties`

Example `config.properties`:
```properties
coordinator=false
http-server.http.port=8081
discovery.uri=http://localhost:8080
node.id=<use-worker-id>
```

## Deployment

1. Build your container with both the holepunch worker and Trino:
   ```dockerfile
   FROM python:3.9-slim
   
   # Install Java for Trino
   RUN apt-get update && apt-get install -y openjdk-11-jre-headless
   
   # Copy holepunch files
   COPY main.py index.html requirements.txt ./
   RUN pip install -r requirements.txt
   
   # Copy Trino
   COPY trino-server /opt/trino
   
   # Start script that runs both
   COPY start.sh /
   CMD ["/start.sh"]
   ```

2. Create `start.sh`:
   ```bash
   #!/bin/bash
   # Start Trino in background
   /opt/trino/bin/launcher start
   
   # Wait for Trino to be ready
   sleep 10
   
   # Start holepunch worker
   python main.py
   ```

## Testing

1. Deploy two workers with `TRINO_MODE=true`
2. Wait for P2P connection to establish
3. Trino workers should be able to discover each other via the proxy
4. Check discovery: `curl http://localhost:8080/v1/announcement`

## Limitations

- Response size is limited to ~1200 bytes due to UDP MTU
- Large query results may need chunking (TODO)
- No SSL/TLS support over the tunnel
- Best suited for metadata exchange, not large data transfers