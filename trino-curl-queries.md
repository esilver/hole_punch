# Trino Query Examples with cURL

## Basic Setup

Set the coordinator URL:
```bash
COORDINATOR_URL="https://trino-p2p-service-coordinator-982092720909.us-central1.run.app"
```

## 1. Simple SELECT Query

Submit a basic query:
```bash
curl -X POST \
  -H "X-Trino-User: admin" \
  -H "X-Trino-Schema: sf1" \
  -H "X-Trino-Catalog: tpch" \
  -H "Content-Type: text/plain" \
  -d "SELECT 1 as test_value" \
  ${COORDINATOR_URL}/v1/statement
```

## 2. Follow Query Results

After submitting a query, you'll get a response with a `nextUri`. Follow it to get results:

```bash
# Submit query and save response
RESPONSE=$(curl -s -X POST \
  -H "X-Trino-User: admin" \
  -H "X-Trino-Schema: sf1" \
  -H "X-Trino-Catalog: tpch" \
  -H "Content-Type: text/plain" \
  -d "SELECT count(*) FROM nation" \
  ${COORDINATOR_URL}/v1/statement)

# Extract nextUri
NEXT_URI=$(echo "$RESPONSE" | jq -r '.nextUri')

# Follow the nextUri to get results
curl -H "X-Trino-User: admin" "$NEXT_URI" | jq '.'
```

## 3. Complete Query Flow Script

```bash
#!/bin/bash

# Configuration
COORDINATOR_URL="https://trino-p2p-service-coordinator-982092720909.us-central1.run.app"
QUERY="SELECT nationkey, name FROM nation LIMIT 5"

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

# Follow nextUri until we get results
NEXT_URI=$(echo "$RESPONSE" | jq -r '.nextUri // empty')
ATTEMPTS=0

while [ -n "$NEXT_URI" ] && [ $ATTEMPTS -lt 10 ]; do
    echo "Fetching results (attempt $((ATTEMPTS+1)))..."
    RESULT=$(curl -s -H "X-Trino-User: admin" "$NEXT_URI")
    
    # Check if we have data
    DATA=$(echo "$RESULT" | jq '.data // empty')
    if [ -n "$DATA" ] && [ "$DATA" != "[]" ]; then
        echo "Query completed! Results:"
        echo "$RESULT" | jq '.columns, .data'
        break
    fi
    
    # Get next URI
    NEXT_URI=$(echo "$RESULT" | jq -r '.nextUri // empty')
    ATTEMPTS=$((ATTEMPTS+1))
    sleep 1
done
```

## 4. Other Useful Queries

### Show Catalogs
```bash
curl -X POST \
  -H "X-Trino-User: admin" \
  -H "Content-Type: text/plain" \
  -d "SHOW CATALOGS" \
  ${COORDINATOR_URL}/v1/statement
```

### Show Schemas
```bash
curl -X POST \
  -H "X-Trino-User: admin" \
  -H "X-Trino-Catalog: tpch" \
  -H "Content-Type: text/plain" \
  -d "SHOW SCHEMAS" \
  ${COORDINATOR_URL}/v1/statement
```

### Show Tables
```bash
curl -X POST \
  -H "X-Trino-User: admin" \
  -H "X-Trino-Schema: sf1" \
  -H "X-Trino-Catalog: tpch" \
  -H "Content-Type: text/plain" \
  -d "SHOW TABLES" \
  ${COORDINATOR_URL}/v1/statement
```

### Check Cluster Nodes
```bash
curl -H "X-Trino-User: admin" ${COORDINATOR_URL}/v1/node | jq '.'
```

### Get Cluster Info
```bash
curl ${COORDINATOR_URL}/v1/info | jq '.'
```

## 5. Monitoring Query Progress

To monitor a running query:
```bash
# Get query info by ID
QUERY_ID="20250528_171014_00002_4wxxf"  # Replace with your query ID
curl -H "X-Trino-User: admin" \
  ${COORDINATOR_URL}/v1/query/${QUERY_ID} | jq '.'
```

## Common Response Fields

- `id`: Unique query identifier
- `state`: Query state (QUEUED, RUNNING, FINISHED, FAILED)
- `nextUri`: URI to fetch next batch of results
- `columns`: Column metadata for results
- `data`: Actual query results
- `stats`: Execution statistics (CPU time, rows processed, etc.)
- `error`: Error details if query failed

## Troubleshooting

If queries fail with `REMOTE_TASK_MISMATCH`:
- Check that the proxy is rewriting IPv6 addresses correctly
- Verify both coordinator and worker nodes are running
- Check the logs for URI rewriting messages