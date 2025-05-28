#!/bin/bash
set -e

# Configuration
COORDINATOR_URL="${1:-http://localhost:8080}"
echo "Testing Trino at: $COORDINATOR_URL"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${BLUE}=== Trino Task Debugging Script ===${NC}"

# Step 1: Check service discovery
echo -e "\n${YELLOW}1. Checking service discovery...${NC}"
curl -s "$COORDINATOR_URL/v1/service/presto" | jq . || echo "Failed to get service discovery"

# Step 2: Check active nodes
echo -e "\n${YELLOW}2. Checking active nodes...${NC}"
curl -s "$COORDINATOR_URL/v1/node" | jq . || echo "Failed to get nodes"

# Step 3: Submit a simple query
echo -e "\n${YELLOW}3. Submitting test query...${NC}"
QUERY_RESPONSE=$(curl -s -X POST "$COORDINATOR_URL/v1/statement" \
  -H "X-Trino-User: admin" \
  -H "X-Trino-Catalog: tpch" \
  -H "X-Trino-Schema: sf1" \
  -H "Content-Type: application/json" \
  -d '"SELECT count(*) FROM nation"')

echo "$QUERY_RESPONSE" | jq .

# Extract query ID and nextUri
QUERY_ID=$(echo "$QUERY_RESPONSE" | jq -r '.id')
NEXT_URI=$(echo "$QUERY_RESPONSE" | jq -r '.nextUri')

if [ -z "$QUERY_ID" ] || [ "$QUERY_ID" == "null" ]; then
    echo -e "${RED}Failed to submit query${NC}"
    exit 1
fi

echo -e "${GREEN}Query ID: $QUERY_ID${NC}"

# Step 4: Poll query status
echo -e "\n${YELLOW}4. Polling query status...${NC}"
POLL_COUNT=0
MAX_POLLS=10

while [ ! -z "$NEXT_URI" ] && [ "$NEXT_URI" != "null" ] && [ $POLL_COUNT -lt $MAX_POLLS ]; do
    POLL_COUNT=$((POLL_COUNT + 1))
    echo -e "\n${BLUE}Poll #$POLL_COUNT:${NC}"
    
    # Get status
    STATUS_RESPONSE=$(curl -s -H "X-Trino-User: admin" "$NEXT_URI")
    
    # Extract key information
    STATE=$(echo "$STATUS_RESPONSE" | jq -r '.stats.state')
    NODES=$(echo "$STATUS_RESPONSE" | jq -r '.stats.nodes')
    QUEUED=$(echo "$STATUS_RESPONSE" | jq -r '.stats.queued')
    SCHEDULED=$(echo "$STATUS_RESPONSE" | jq -r '.stats.scheduled')
    RUNNING_SPLITS=$(echo "$STATUS_RESPONSE" | jq -r '.stats.runningSplits')
    TOTAL_SPLITS=$(echo "$STATUS_RESPONSE" | jq -r '.stats.totalSplits')
    
    echo "State: $STATE"
    echo "Nodes: $NODES"
    echo "Queued: $QUEUED"
    echo "Scheduled: $SCHEDULED"
    echo "Running Splits: $RUNNING_SPLITS"
    echo "Total Splits: $TOTAL_SPLITS"
    
    # Check if query is finished
    if [ "$STATE" == "FINISHED" ] || [ "$STATE" == "FAILED" ]; then
        echo -e "\n${GREEN}Query completed with state: $STATE${NC}"
        echo "$STATUS_RESPONSE" | jq '.data'
        break
    fi
    
    # Update nextUri
    NEXT_URI=$(echo "$STATUS_RESPONSE" | jq -r '.nextUri')
    
    # Small delay between polls
    sleep 0.5
done

# Step 5: Check query info endpoint
echo -e "\n${YELLOW}5. Checking query info...${NC}"
curl -s "$COORDINATOR_URL/v1/query/$QUERY_ID" | jq '.session, .state, .resourceGroupId, .self' || echo "Failed to get query info"

# Step 6: Monitor logs for task activity
echo -e "\n${YELLOW}6. Task Activity Summary${NC}"
echo "To monitor task activity in real-time, run these commands:"
echo -e "${BLUE}# On coordinator:${NC}"
echo "gcloud run logs read --service=trino-p2p-service-coordinator --limit=100 | grep -E '(TASK|announcement|Response body)'"
echo -e "${BLUE}# On worker:${NC}"
echo "gcloud run logs read --service=trino-p2p-service-worker --limit=100 | grep -E '(TASK|announcement|Response body)'"

# Step 7: Direct Trino diagnostics
echo -e "\n${YELLOW}7. Checking Trino internal state...${NC}"
echo -e "${BLUE}Coordinator info:${NC}"
curl -s "$COORDINATOR_URL/v1/info" | jq . || echo "Failed to get coordinator info"

echo -e "\n${BLUE}Coordinator status:${NC}"
curl -s "$COORDINATOR_URL/v1/status" | jq . || echo "Failed to get coordinator status"

# Summary
echo -e "\n${YELLOW}=== Summary ===${NC}"
if [ "$NODES" == "0" ] || [ -z "$NODES" ]; then
    echo -e "${RED}❌ Issue confirmed: nodes = 0${NC}"
    echo "Possible causes:"
    echo "1. Coordinator configuration issue (node-scheduler.include-coordinator=false)"
    echo "2. Worker announcement failures"
    echo "3. Node health check failures"
    echo "4. Task endpoint communication issues"
else
    echo -e "${GREEN}✓ Nodes detected: $NODES${NC}"
fi

echo -e "\n${YELLOW}Next debugging steps:${NC}"
echo "1. Check coordinator logs for announcement processing"
echo "2. Check worker logs for announcement attempts"
echo "3. Verify response bodies from /v1/info and /v1/status endpoints"
echo "4. Monitor for TASK REQUEST entries in logs"