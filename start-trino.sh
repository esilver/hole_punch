#!/bin/bash
set -e

echo "Starting Trino + Holepunch P2P Worker..."

# Generate unique node ID if not set
if [ -z "$WORKER_ID" ]; then
    export WORKER_ID=$(uuidgen || cat /proc/sys/kernel/random/uuid || echo "worker-$$")
fi

echo "Worker ID: $WORKER_ID"

# Update Trino configuration with dynamic values
sed -i "s/NODE_ID_PLACEHOLDER/$WORKER_ID/g" /opt/trino/etc/node.properties

# Set coordinator mode based on environment
if [ "$TRINO_COORDINATOR_ID" = "$WORKER_ID" ]; then
    echo "This worker is the COORDINATOR"
    sed -i "s/coordinator=false/coordinator=true/g" /opt/trino/etc/config.properties
    # Coordinator needs to handle discovery
    sed -i "s/discovery-server.enabled=false/discovery-server.enabled=true/g" /opt/trino/etc/config.properties
fi

# Start Trino in the background
echo "Starting Trino server on port $TRINO_LOCAL_PORT..."
cd /opt/trino
bin/launcher start

# Wait for Trino to be ready
echo "Waiting for Trino to start..."
for i in {1..30}; do
    if curl -s http://localhost:$TRINO_LOCAL_PORT/v1/info > /dev/null; then
        echo "Trino is ready!"
        break
    fi
    echo "Waiting for Trino... ($i/30)"
    sleep 2
done

# Start the holepunch P2P worker
echo "Starting Holepunch P2P worker..."
cd /app
exec python -u main.py