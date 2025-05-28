#!/bin/bash

# Configuration
COORDINATOR_URL="https://trino-p2p-service-coordinator-982092720909.us-central1.run.app"

echo "=== Testing Query Execution Patterns ==="

# Test 1: Simple SELECT that should run on coordinator only
echo -e "\n1. Testing simple SELECT (should run on coordinator only):"
RESPONSE=$(curl -s -X POST \
  -H "X-Trino-User: admin" \
  -H "Content-Type: text/plain" \
  -d "SELECT 1 as test" \
  ${COORDINATOR_URL}/v1/statement)

QUERY_ID=$(echo "$RESPONSE" | jq -r '.id')
echo "Query ID: $QUERY_ID"

# Wait a bit and check query details
sleep 3
QUERY_INFO=$(curl -s -H "X-Trino-User: admin" ${COORDINATOR_URL}/v1/query/${QUERY_ID})
echo "Query state: $(echo "$QUERY_INFO" | jq -r '.state')"
echo "Coordinator only: $(echo "$QUERY_INFO" | jq -r '.queryStats.coordinatorOnly // false')"
echo "Total nodes: $(echo "$QUERY_INFO" | jq -r '.queryStats.nodes // 0')"

# Test 2: Query that reads from a table
echo -e "\n2. Testing table scan (may require workers):"
RESPONSE=$(curl -s -X POST \
  -H "X-Trino-User: admin" \
  -H "X-Trino-Schema: sf1" \
  -H "X-Trino-Catalog: tpch" \
  -H "Content-Type: text/plain" \
  -d "SELECT * FROM nation WHERE nationkey = 0" \
  ${COORDINATOR_URL}/v1/statement)

QUERY_ID=$(echo "$RESPONSE" | jq -r '.id')
echo "Query ID: $QUERY_ID"

# Follow the query and get detailed stats
NEXT_URI=$(echo "$RESPONSE" | jq -r '.nextUri // empty')
NEXT_URI=$(echo "$NEXT_URI" | sed 's|http://|https://|')
ATTEMPTS=0

while [ -n "$NEXT_URI" ] && [ $ATTEMPTS -lt 5 ]; do
    RESULT=$(curl -s -H "X-Trino-User: admin" "$NEXT_URI")
    STATE=$(echo "$RESULT" | jq -r '.stats.state // "unknown"')
    echo "  Attempt $((ATTEMPTS+1)): State=$STATE"
    
    # If running or finished, show node info
    if [[ "$STATE" == "RUNNING" ]] || [[ "$STATE" == "FINISHED" ]]; then
        echo "  Nodes involved: $(echo "$RESULT" | jq -r '.stats.nodes // 0')"
        echo "  Root stage info:"
        echo "$RESULT" | jq '.stats.rootStage | {nodes, coordinatorOnly}'
        
        # Check for data
        DATA=$(echo "$RESULT" | jq '.data // empty')
        if [ -n "$DATA" ] && [ "$DATA" != "[]" ]; then
            echo "  Got data: $DATA"
        fi
    fi
    
    # Check for errors
    ERROR=$(echo "$RESULT" | jq '.error // empty')
    if [ -n "$ERROR" ] && [ "$ERROR" != "null" ]; then
        echo "  ERROR: $(echo "$ERROR" | jq -r '.message')"
        break
    fi
    
    NEXT_URI=$(echo "$RESULT" | jq -r '.nextUri // empty')
    NEXT_URI=$(echo "$NEXT_URI" | sed 's|http://|https://|')
    ATTEMPTS=$((ATTEMPTS+1))
    sleep 1
done

# Get final query info
sleep 1
echo -e "\nFinal query info for $QUERY_ID:"
curl -s -H "X-Trino-User: admin" ${COORDINATOR_URL}/v1/query/${QUERY_ID} | jq '{
  state: .state,
  coordinatorOnly: .queryStats.coordinatorOnly,
  totalNodes: .queryStats.nodes,
  failureInfo: .failureInfo.message
}'

# Test 3: Check which nodes are active
echo -e "\n3. Active nodes in cluster:"
curl -s -H "X-Trino-User: admin" ${COORDINATOR_URL}/v1/node | jq -r '.[] | "Node: \(.uri)"'