#!/bin/bash

COORDINATOR_URL="https://trino-p2p-service-coordinator-982092720909.us-central1.run.app"

# Function to follow a query to completion
follow_query() {
    local QUERY="$1"
    local DESC="$2"
    
    echo -e "\n=== $DESC ==="
    echo "Query: $QUERY"
    
    # Submit query
    RESPONSE=$(curl -s -X POST \
      -H "X-Trino-User: admin" \
      -H "X-Trino-Schema: sf1" \
      -H "X-Trino-Catalog: tpch" \
      -H "Content-Type: text/plain" \
      -d "$QUERY" \
      ${COORDINATOR_URL}/v1/statement)
    
    QUERY_ID=$(echo "$RESPONSE" | jq -r '.id')
    echo "Query ID: $QUERY_ID"
    
    # Follow nextUri
    NEXT_URI=$(echo "$RESPONSE" | jq -r '.nextUri // empty')
    NEXT_URI=$(echo "$NEXT_URI" | sed 's|http://|https://|')
    
    local FINAL_STATE=""
    local NODES_USED=0
    local IS_COORDINATOR_ONLY="unknown"
    local ERROR_MSG=""
    
    while [ -n "$NEXT_URI" ]; do
        RESULT=$(curl -s -H "X-Trino-User: admin" "$NEXT_URI")
        STATE=$(echo "$RESULT" | jq -r '.stats.state // "unknown"')
        FINAL_STATE=$STATE
        
        # Extract stats
        NODES_USED=$(echo "$RESULT" | jq -r '.stats.nodes // 0')
        
        # Check root stage for coordinator-only info
        ROOT_STAGE=$(echo "$RESULT" | jq '.stats.rootStage // empty')
        if [ -n "$ROOT_STAGE" ] && [ "$ROOT_STAGE" != "null" ]; then
            IS_COORDINATOR_ONLY=$(echo "$ROOT_STAGE" | jq -r '.coordinatorOnly // "false"')
        fi
        
        # Check for error
        ERROR=$(echo "$RESULT" | jq '.error // empty')
        if [ -n "$ERROR" ] && [ "$ERROR" != "null" ]; then
            ERROR_MSG=$(echo "$ERROR" | jq -r '.message')
            break
        fi
        
        # Check if query completed
        if [[ "$STATE" == "FINISHED" ]] || [[ "$STATE" == "FAILED" ]]; then
            break
        fi
        
        NEXT_URI=$(echo "$RESULT" | jq -r '.nextUri // empty')
        NEXT_URI=$(echo "$NEXT_URI" | sed 's|http://|https://|')
        sleep 0.5
    done
    
    # Print results
    echo "Final State: $FINAL_STATE"
    echo "Nodes Used: $NODES_USED"
    echo "Coordinator Only: $IS_COORDINATOR_ONLY"
    if [ -n "$ERROR_MSG" ]; then
        echo "Error: $ERROR_MSG"
    fi
    
    # Get detailed query info via the info URI
    INFO_URI="https://trino-p2p-service-coordinator-982092720909.us-central1.run.app/ui/api/query/${QUERY_ID}"
    echo -e "\nDetailed query info from UI API:"
    QUERY_DETAILS=$(curl -s -H "X-Trino-User: admin" "$INFO_URI" 2>/dev/null)
    if [ $? -eq 0 ] && [ -n "$QUERY_DETAILS" ]; then
        echo "$QUERY_DETAILS" | jq '{
            state: .state,
            scheduled: .scheduled,
            totalTasks: .queryStats.totalTasks,
            runningTasks: .queryStats.runningTasks,
            completedTasks: .queryStats.completedTasks
        }' 2>/dev/null || echo "Could not parse query details"
    fi
}

# Test different query types
follow_query "SELECT 1" "Simple SELECT (should be coordinator-only)"
follow_query "VALUES 1, 2, 3" "VALUES query (should be coordinator-only)"
follow_query "SELECT count(*) FROM nation" "Aggregation query (may need workers)"
follow_query "SELECT * FROM nation LIMIT 1" "Table scan with LIMIT (may need workers)"

# Show current cluster state
echo -e "\n=== Current Cluster Nodes ==="
curl -s -H "X-Trino-User: admin" ${COORDINATOR_URL}/v1/node | jq -r '.[] | "Node: \(.uri) (Failures: \(.recentFailureRatio))"'