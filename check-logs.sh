#\!/bin/bash

echo "=== Checking Coordinator Logs for Task Routing ==="
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="trino-p2p-service-coordinator" AND ("Proxy request to worker" OR "Current P2P peer" OR "Request will be sent via UDP" OR "Sending request to peer" OR "Request path")' --limit=50 --format=json | jq -r '.[] | .textPayload // .jsonPayload.message' | grep -E "(Proxy request to worker|Current P2P peer|Request will be sent|Sending request to peer|Request path)" | tail -20

echo -e "\n=== Checking for UDP Connection Status ==="
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="trino-p2p-service-coordinator" AND ("P2P connection established" OR "Set new P2P target" OR "current_p2p_peer")' --limit=20 --format=json | jq -r '.[] | .textPayload // .jsonPayload.message' | tail -10

echo -e "\n=== Checking for Task Request Errors ==="
gcloud logging read 'resource.type="cloud_run_revision" AND (resource.labels.service_name="trino-p2p-service-coordinator" OR resource.labels.service_name="trino-p2p-service-worker") AND ("/v1/task" OR "REMOTE_TASK")' --limit=30 --format=json | jq -r '.[] | .textPayload // .jsonPayload.message' | grep -E "(v1/task|REMOTE)" | tail -15

echo -e "\n=== Checking Worker Announcement Logs ==="
gcloud logging read 'resource.type="cloud_run_revision" AND ("Rewrote announcement" OR "Announcement after rewriting")' --limit=10 --format=json | jq -r '.[] | .textPayload // .jsonPayload.message' | tail -10
