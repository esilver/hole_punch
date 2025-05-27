#!/bin/bash

# Test script for Trino P2P deployment

echo "Testing Trino P2P Setup..."

# Get coordinator URL from Cloud Run
COORDINATOR_URL=$(gcloud run services describe trino-p2p-service-coordinator --region us-central1 --format 'value(status.url)' --project iceberg-eli)

if [ -z "$COORDINATOR_URL" ]; then
    echo "Error: Could not get coordinator URL"
    exit 1
fi

echo "Coordinator URL: $COORDINATOR_URL"

# Test 1: Check worker info
echo -e "\n1. Testing worker info endpoint:"
curl -s "$COORDINATOR_URL/v1/info" | jq .

# Test 2: Check discovery/announcement
echo -e "\n2. Testing discovery endpoint:"
curl -s "$COORDINATOR_URL/v1/announcement" | jq .

# Test 3: Test Trino query via HTTP API
echo -e "\n3. Testing Trino query execution:"
QUERY="SELECT 1 as test, 'Hello from Trino P2P' as message"

# Submit query
RESPONSE=$(curl -s -X POST \
    -H "X-Trino-User: test" \
    -H "X-Trino-Catalog: tpch" \
    -H "X-Trino-Schema: sf1" \
    -d "$QUERY" \
    "$COORDINATOR_URL/v1/statement")

echo "Query response:"
echo "$RESPONSE" | jq .

# Extract nextUri to get results
NEXT_URI=$(echo "$RESPONSE" | jq -r '.nextUri // empty')

if [ ! -z "$NEXT_URI" ]; then
    echo -e "\n4. Fetching query results:"
    sleep 2
    curl -s -H "X-Trino-User: test" "$NEXT_URI" | jq .
fi

# Test 5: Check system nodes
echo -e "\n5. Testing system nodes query:"
QUERY="SELECT * FROM system.runtime.nodes"
RESPONSE=$(curl -s -X POST \
    -H "X-Trino-User: test" \
    -H "X-Trino-Catalog: system" \
    -H "X-Trino-Schema: runtime" \
    -d "$QUERY" \
    "$COORDINATOR_URL/v1/statement")

echo "$RESPONSE" | jq .