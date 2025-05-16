# Cloud Run with Cloud NAT: Public IP Observation Proof-of-Concept

**Project ID:** `iceberg-eli`

## Objective

This proof-of-concept demonstrates that a Google Cloud Run service, when configured with Direct VPC Egress to route all outbound traffic through a Cloud NAT gateway, will have its outbound connections to the public internet appear as originating from one of the public IP addresses assigned to the Cloud NAT gateway.

We will deploy a simple Python web application to Cloud Run. This application will call an external IP echo service and log the public IP address it observes. We will then verify this IP against the public IPs assigned to our Cloud NAT gateway.

## Prerequisites

1. **Google Cloud SDK (gcloud):** Ensure you have `gcloud` installed and authenticated.

   * Installation: [https://cloud.google.com/sdk/docs/install](https://cloud.google.com/sdk/docs/install)
   * Login: `gcloud auth login`
   * Set project: `gcloud config set project iceberg-eli`
2. **Docker:** While Cloud Build is used for building the image (Step 3), having Docker installed locally can be useful for testing.

   * Installation: [https://docs.docker.com/get-docker/](https://docs.docker.com/get-docker/)
3. **APIs Enabled:** Ensure the following APIs are enabled in your `iceberg-eli` project.

   ```bash
   gcloud services enable \
       cloudbuild.googleapis.com \
       run.googleapis.com \
       compute.googleapis.com \
       artifactregistry.googleapis.com \
       iam.googleapis.com
   ```

## Files

Create a directory for this project and place the following two files inside it:

**1. `main.py`:**

```python
import os
import requests
from flask import Flask

app = Flask(__name__)

# Using a well-known public IP echo service.
IP_ECHO_SERVICE_URL = "https://api.ipify.org"

@app.route('/')
def get_my_public_ip():
    try:
        print(f"Cloud Run service 'ip-worker-service' (project: iceberg-eli) making request to: {IP_ECHO_SERVICE_URL}")
        response = requests.get(IP_ECHO_SERVICE_URL, timeout=10)
        response.raise_for_status()  # Raise an exception for HTTP errors
        
        public_ip = response.text.strip()
        
        log_message = f"The IP echo service reported this service's public IP as: {public_ip}"
        print(log_message)
        
        verification_note = (
            "VERIFICATION STEP: In the Google Cloud Console for project 'iceberg-eli', navigate to 'VPC Network' -> 'Cloud NAT'. "
            "Select the 'ip-worker-nat' gateway in the 'us-central1' region. "
            "Confirm that the IP address reported above is listed under 'NAT IP addresses' for that gateway."
        )
        print(verification_note)
        
        return f"{log_message}<br><br>{verification_note.replace(chr(10), '<br>')}", 200

    except requests.exceptions.RequestException as e:
        error_message = f"Error connecting to IP echo service: {e}"
        print(error_message)
        return error_message, 500

if __name__ == "__main__":
    if not os.environ.get("K_SERVICE"):
        port = int(os.environ.get("PORT", 8080))
        app.run(host='0.0.0.0', port=port, debug=True)
```

**2. `Dockerfile`:**

```dockerfile
FROM python:3.9-slim

WORKDIR /app

RUN pip install --no-cache-dir Flask requests gunicorn

COPY main.py .

EXPOSE 8080

ENV PORT 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "main:app"]
```

## Step 1: Prepare Your Google Cloud Environment

We'll set up the networking resources in the `us-central1` region for project `iceberg-eli`.

1. **Define Shell Variables (Optional, for convenience):**

   ```bash
   export PROJECT_ID="iceberg-eli"
   export REGION="us-central1"
   export VPC_NETWORK_NAME="ip-worker-vpc"
   export SUBNET_NAME="ip-worker-subnet"
   export SUBNET_CIDR="10.10.1.0/24"
   export ROUTER_NAME="ip-worker-router"
   export NAT_NAME="ip-worker-nat"
   export AR_REPO_NAME="ip-worker-repo"
   export CLOUD_RUN_SERVICE_NAME="ip-worker-service"

   gcloud config set project $PROJECT_ID
   gcloud config set compute/region $REGION
   ```

2. **Create VPC Network:**

   ```bash
   gcloud compute networks create $VPC_NETWORK_NAME \
       --project=$PROJECT_ID \
       --subnet-mode=custom \
       --mtu=1460 \
       --bgp-routing-mode=regional
   ```

3. **Create VPC Subnet:**

   ```bash
   gcloud compute networks subnets create $SUBNET_NAME \
       --project=$PROJECT_ID \
       --range=$SUBNET_CIDR \
       --network=$VPC_NETWORK_NAME \
       --region=$REGION
   ```

4. **Create Cloud Router:**

   ```bash
   gcloud compute routers create $ROUTER_NAME \
       --project=$PROJECT_ID \
       --network=$VPC_NETWORK_NAME \
       --region=$REGION
   ```

5. **Create Cloud NAT Gateway:**

   ```bash
   gcloud compute nats create $NAT_NAME \
       --project=$PROJECT_ID \
       --router=$ROUTER_NAME \
       --region=$REGION \
       --nat-custom-subnet-ip-ranges=$SUBNET_NAME \
       --nat-external-ip-pool=""
   ```

## Step 2: Build and Push the Docker Image

1. **Create Artifact Registry Repository:**

   ```bash
   gcloud artifacts repositories create $AR_REPO_NAME \
       --project=$PROJECT_ID \
       --repository-format=docker \
       --location=$REGION \
       --description="Docker repository for IP worker PoC"
   ```

2. **Grant Cloud Build permissions:**

   ```bash
   export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
   gcloud projects add-iam-policy-binding $PROJECT_ID \
       --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
       --role="roles/artifactregistry.writer"
   ```

3. **Build and Submit Image:**

   ```bash
   gcloud builds submit --region=$REGION --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO_NAME}/${CLOUD_RUN_SERVICE_NAME}:latest
   ```

## Step 3: Deploy to Cloud Run

```bash
gcloud run deploy $CLOUD_RUN_SERVICE_NAME \
    --project=$PROJECT_ID \
    --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO_NAME}/${CLOUD_RUN_SERVICE_NAME}:latest \
    --platform=managed \
    --region=$REGION \
    --allow-unauthenticated \
    --vpc-egress=all-traffic \
    --network=$VPC_NETWORK_NAME \
    --subnet=$SUBNET_NAME \
    --max-instances=1
```

## Step 4: Test and Verify

1. **Get Service URL:**

   ```bash
   gcloud run services describe $CLOUD_RUN_SERVICE_NAME --project=$PROJECT_ID --platform=managed --region=$REGION --format='value(status.url)'
   ```

2. **Invoke Service:**

   ```bash
   SERVICE_URL=$(gcloud run services describe $CLOUD_RUN_SERVICE_NAME --project=$PROJECT_ID --platform=managed --region=$REGION --format='value(status.url)')
   echo "Accessing service at: $SERVICE_URL"
   curl $SERVICE_URL
   ```

3. **Check Logs and Verify NAT IP:**

   * Go to Cloud Console: Cloud Run -> `ip-worker-service` -> Logs
   * Output will include observed IP
   * Go to VPC Network -> Cloud NAT -> `ip-worker-nat` and confirm observed IP is listed

## Step 5: Cleanup (Optional)

```bash
gcloud run services delete $CLOUD_RUN_SERVICE_NAME --project=$PROJECT_ID --platform=managed --region=$REGION --quiet
gcloud compute nats delete $NAT_NAME --router=$ROUTER_NAME --project=$PROJECT_ID --region=$REGION --quiet
gcloud compute routers delete $ROUTER_NAME --project=$PROJECT_ID --region=$REGION --quiet
gcloud artifacts repositories delete $AR_REPO_NAME --project=$PROJECT_ID --location=$REGION --quiet

# Optional, be cautious
# gcloud compute networks subnets delete $SUBNET_NAME --project=$PROJECT_ID --region=$REGION --quiet
# gcloud compute networks delete $VPC_NETWORK_NAME --project=$PROJECT_ID --quiet
```

*Always double-check before deleting shared resources.*
