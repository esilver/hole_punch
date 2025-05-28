#!/bin/bash
set -e

echo "Starting Trino + Holepunch P2P Worker..."

# Use provided WORKER_ID or generate one if not set
if [ -z "$WORKER_ID" ]; then
    export WORKER_ID=$(uuidgen || cat /proc/sys/kernel/random/uuid || echo "worker-$$")
fi

echo "Worker ID: $WORKER_ID"

# Update Trino configuration with dynamic values
sed -i "s/NODE_ID_PLACEHOLDER/$WORKER_ID/g" /opt/trino/etc/node.properties

# Set coordinator mode based on environment
if [ "${IS_COORDINATOR,,}" = "true" ] || [ "${IS_COORDINATOR}" = "1" ] || [ "${IS_COORDINATOR,,}" = "yes" ] || [ "${IS_COORDINATOR,,}" = "on" ]; then
    echo "This worker is the COORDINATOR"
    sed -i "s/coordinator=false/coordinator=true/g" /opt/trino/etc/config.properties
    # Coordinator needs to handle discovery
    sed -i "s/discovery-server.enabled=false/discovery-server.enabled=true/g" /opt/trino/etc/config.properties
fi

# Start Trino in the background
echo "Starting Trino server on port $TRINO_LOCAL_PORT..."
cd /opt/trino

# Create a launcher script wrapper to keep Trino running and log output
cat > /tmp/run-trino.sh << 'EOF'
#!/bin/bash
cd /opt/trino
echo "Starting Trino launcher..."
exec bin/launcher run 2>&1
EOF
chmod +x /tmp/run-trino.sh

# Start Trino in background and capture output
echo "Launching Trino process..."
/tmp/run-trino.sh > /tmp/trino.log 2>&1 &
TRINO_PID=$!
echo "Trino PID: $TRINO_PID"

# Wait for Trino to be ready
echo "Waiting for Trino to start..."
for i in {1..30}; do
    if curl -s http://localhost:$TRINO_LOCAL_PORT/v1/info > /dev/null 2>&1; then
        echo "Trino is ready!"
        break
    fi
    if ! kill -0 $TRINO_PID 2>/dev/null; then
        echo "Trino process died! Trino log:"
        cat /tmp/trino.log
        exit 1
    fi
    echo "Waiting for Trino... ($i/30)"
    sleep 2
done

# Check if Trino started successfully
if ! curl -s http://localhost:$TRINO_LOCAL_PORT/v1/info > /dev/null 2>&1; then
    echo "Trino failed to start! Last 50 lines of log:"
    tail -50 /tmp/trino.log
    exit 1
fi

echo "Trino log (last 20 lines):"
tail -20 /tmp/trino.log

# Start the holepunch P2P worker
echo "Starting Holepunch P2P worker..."
cd /app
exec python -u main.py