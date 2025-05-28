#!/bin/bash

COORDINATOR_URL="https://trino-p2p-service-coordinator-982092720909.us-central1.run.app"

# Function to analyze query execution in detail
analyze_query_execution() {
    local QUERY="$1"
    local DESC="$2"
    
    echo -e "\n==============================================="
    echo "ANALYZING: $DESC"
    echo "QUERY: $QUERY"
    echo "==============================================="
    
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
    
    # Follow the query execution
    NEXT_URI=$(echo "$RESPONSE" | jq -r '.nextUri // empty')
    NEXT_URI=$(echo "$NEXT_URI" | sed 's|http://|https://|')
    
    local ITERATION=0
    local ALL_STAGES=""
    
    while [ -n "$NEXT_URI" ] && [ $ITERATION -lt 20 ]; do
        ITERATION=$((ITERATION + 1))
        echo -e "\n--- Polling iteration $ITERATION ---"
        
        RESULT=$(curl -s -H "X-Trino-User: admin" "$NEXT_URI")
        
        # Extract key information
        STATE=$(echo "$RESULT" | jq -r '.stats.state // "unknown"')
        NODES=$(echo "$RESULT" | jq -r '.stats.nodes // 0')
        QUEUED=$(echo "$RESULT" | jq -r '.stats.queued // false')
        SCHEDULED=$(echo "$RESULT" | jq -r '.stats.scheduled // false')
        
        echo "State: $STATE, Nodes: $NODES, Queued: $QUEUED, Scheduled: $SCHEDULED"
        
        # Get root stage details
        ROOT_STAGE=$(echo "$RESULT" | jq '.stats.rootStage // empty')
        if [ -n "$ROOT_STAGE" ] && [ "$ROOT_STAGE" != "null" ]; then
            echo "Root Stage:"
            echo "$ROOT_STAGE" | jq '{
                stageId: .stageId,
                state: .state,
                done: .done,
                nodes: .nodes,
                coordinatorOnly: .coordinatorOnly,
                totalSplits: .totalSplits,
                completedSplits: .completedSplits,
                failedTasks: .failedTasks
            }'
            
            # Check substages
            SUBSTAGES=$(echo "$ROOT_STAGE" | jq '.subStages // []')
            if [ "$SUBSTAGES" != "[]" ]; then
                echo "Substages found:"
                echo "$SUBSTAGES" | jq '.[] | {stageId: .stageId, state: .state, nodes: .nodes}'
            fi
        fi
        
        # Check for errors
        ERROR=$(echo "$RESULT" | jq '.error // empty')
        if [ -n "$ERROR" ] && [ "$ERROR" != "null" ]; then
            echo -e "\nERROR DETAILS:"
            echo "$ERROR" | jq '{
                message: .message,
                errorCode: .errorCode,
                errorName: .errorName,
                errorType: .errorType
            }'
            
            # Extract the problematic address
            ERROR_MSG=$(echo "$ERROR" | jq -r '.message')
            if [[ "$ERROR_MSG" == *"fddf:3978"* ]]; then
                echo -e "\nIPv6 ADDRESS DETECTED IN ERROR!"
                echo "$ERROR_MSG" | grep -o '\[.*\]:[0-9]*'
            fi
            break
        fi
        
        # If query finished, show final stats
        if [[ "$STATE" == "FINISHED" ]]; then
            echo -e "\nQuery finished successfully!"
            DATA=$(echo "$RESULT" | jq '.data // empty')
            if [ -n "$DATA" ] && [ "$DATA" != "[]" ]; then
                echo "Result data: $DATA"
            fi
            break
        fi
        
        NEXT_URI=$(echo "$RESULT" | jq -r '.nextUri // empty')
        NEXT_URI=$(echo "$NEXT_URI" | sed 's|http://|https://|')
        sleep 0.5
    done
    
    # Try to get more detailed info from query endpoint
    echo -e "\n--- Attempting to get query details ---"
    QUERY_DETAILS=$(curl -s -H "X-Trino-User: admin" "${COORDINATOR_URL}/v1/query/${QUERY_ID}" 2>&1)
    if [[ "$QUERY_DETAILS" == *"{"* ]]; then
        echo "Query endpoint response:"
        echo "$QUERY_DETAILS" | jq '{
            queryId: .queryId,
            state: .state,
            memoryPool: .memoryPool,
            scheduled: .scheduled,
            self: .self,
            fieldNames: .fieldNames
        }' 2>/dev/null || echo "Could not parse query details"
    else
        echo "Query endpoint error: $QUERY_DETAILS"
    fi
}

# Test specific queries
echo "Testing queries that should be coordinator-only vs distributed..."

# Query 1: Pure computation (no tables)
analyze_query_execution "SELECT 1 + 1 as result" "Pure computation - should be coordinator-only"

# Query 2: System table
analyze_query_execution "SELECT * FROM system.runtime.nodes" "System table query"

# Query 3: Small table scan
analyze_query_execution "SELECT nationkey FROM nation WHERE nationkey = 0" "Single row lookup"

# Query 4: Aggregation
analyze_query_execution "SELECT count(*) as total FROM nation" "Simple aggregation"

# Check task endpoints
echo -e "\n==============================================="
echo "CHECKING TASK ENDPOINTS"
echo "==============================================="

# Try to access a task endpoint directly
echo "Attempting to list tasks..."
curl -s -H "X-Trino-User: admin" "${COORDINATOR_URL}/v1/task" 2>&1 | head -20