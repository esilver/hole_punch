#!/bin/bash

# Configuration
COORDINATOR_URL="https://trino-p2p-service-coordinator-982092720909.us-central1.run.app"
# QUERY="SELECT * FROM tpch.sf1000.lineitem LIMIT 10"
# QUERY="SELECT * FROM system.runtime.nodes"
QUERY="SELECT count(*) FROM nation"
# Submit query
echo "Submitting query: $QUERY"
RESPONSE=$(curl -s -X POST \
  -H "X-Trino-User: admin" \
  -H "X-Trino-Schema: sf1" \
  -H "X-Trino-Catalog: tpch" \
  -H "Content-Type: text/plain" \
  -d "$QUERY" \
  ${COORDINATOR_URL}/v1/statement)

QUERY_ID=$(echo "$RESPONSE" | jq -r '.id')
echo "Query ID: $QUERY_ID"

# Debug initial response
echo "Initial response:"
echo "$RESPONSE" | jq '.stats.state, .error // empty'

# Follow nextUri until we get results
NEXT_URI=$(echo "$RESPONSE" | jq -r '.nextUri // empty')
# Convert HTTP to HTTPS for Cloud Run
NEXT_URI=$(echo "$NEXT_URI" | sed 's|http://|https://|')
ATTEMPTS=0

while [ -n "$NEXT_URI" ] && [ $ATTEMPTS -lt 10 ]; do
    echo "Fetching results (attempt $((ATTEMPTS+1)))..."
    echo "NextURI: $NEXT_URI"
    RESULT=$(curl -s -H "X-Trino-User: admin" "$NEXT_URI")
    
    # Debug: show the full result
    echo "Response state: $(echo "$RESULT" | jq -r '.stats.state // "unknown"')"
    
    # Check if query failed
    ERROR=$(echo "$RESULT" | jq '.error // empty')
    if [ -n "$ERROR" ] && [ "$ERROR" != "null" ]; then
        echo "Query failed!"
        echo "$RESULT" | jq '.error'
        break
    fi
    
    # Check if we have data
    DATA=$(echo "$RESULT" | jq '.data // empty')
    if [ -n "$DATA" ] && [ "$DATA" != "[]" ]; then
        echo "Query completed! Results:"
        echo "$RESULT" | jq '.columns, .data'
        break
    fi
    
    # Get next URI
    NEXT_URI=$(echo "$RESULT" | jq -r '.nextUri // empty')
    # Convert HTTP to HTTPS for Cloud Run
    NEXT_URI=$(echo "$NEXT_URI" | sed 's|http://|https://|')
    ATTEMPTS=$((ATTEMPTS+1))
    sleep 1
done

# If we exhausted attempts, show the last result
if [ $ATTEMPTS -ge 10 ]; then
    echo "Exhausted attempts. Last response:"
    echo "$RESULT" | jq '.'
fi