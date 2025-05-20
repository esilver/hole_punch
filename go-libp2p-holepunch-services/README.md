# go-libp2p Hole-Punch POC

This directory contains two small services built with Go libp2p:

* **`rendezvous`** – a central node that workers dial to announce themselves.
* **`worker`** – a client node that registers with the rendezvous service.

Both packages have unit tests (`go test ./...`), and everything is wired-up for quick local runs.

## Quick start

1. **Open a terminal & start the rendezvous service**

   ```bash
   cd go-libp2p-holepunch-services
   make rendezvous     # or: go run ./rendezvous
   ```

   It prints something like

   ```
   Listening on addresses:
     /ip4/127.0.0.1/tcp/40001/p2p/12D3KooW…
   ```

2. **Copy one of the printed multi-addresses** (the whole line!)

3. **In another terminal, start a worker**

   ```bash
   cd go-libp2p-holepunch-services
   make worker RENDEZVOUS=/ip4/127.0.0.1/tcp/40001/p2p/12D3KooW…
   ```

   The worker connects, receives an `ACK`, and stays running.

4. **Spin up more workers**

   ```bash
   make worker-multi N=3 RENDEZVOUS=/ip4/.../p2p/...
   ```

You will see each registration appear in the rendezvous terminal.

## Tests

```bash
cd go-libp2p-holepunch-services/rendezvous && go test -v
cd ../worker && go test -v
```

## Features

* `/list` protocol allows workers to discover peers directly from the rendezvous service.
* Workers use libp2p's hole punching and AutoRelay to form direct or relayed connections.
  Set `LIBP2P_RELAY_BOOTSTRAP` to a relay multiaddr if you provide one.
* Containerise each service (`Dockerfile`) and deploy to Cloud Run behind Cloud NAT.

## One-command Cloud Run deploy (experimental)

If you have the Google Cloud CLI installed and already ran `gcloud auth login`, you can push
and deploy all three demo services with a single helper script:

```bash
cd go-libp2p-holepunch-services

# region and project are positional arguments
./deploy_to_cloud_run.sh us-central1 my-gcp-project
```

What the script does:

1. Build and push container images for `peerapi`, `rendezvous`, and `worker` to
   `gcr.io/<project>/go-libp2p-holepunch-demo/…:latest`.
2. Deploy **peerapi** first, capture its public URL.
3. Deploy **rendezvous** with the env-var `PEER_DISCOVERY_URL=<peerapi-url>` so
   every registration is mirrored to the JSON API.
4. Ask you to copy the rendezvous node's libp2p **multi-address** from its
   startup logs (Cloud Run generates a fresh peer ID every time).  Paste it
   back to the script.
5. Deploy **worker** with both `RENDEZVOUS_MULTIADDR` *and*
   `RENDEZVOUS_SERVICE_URL` set.

After the final step Cloud Run hosts three fully-working services:

* peerapi      → JSON list of peers
* rendezvous → libp2p WebSocket listener (wss)
* worker      → connects, registers, then fetches the peer list

You can scale out additional workers via

```bash
gcloud run jobs create worker-copy --image gcr.io/<project>/go-libp2p-holepunch-demo/worker:latest \
  --region us-central1 --tasks 5 --set-env-vars "RENDEZVOUS_MULTIADDR=…,RENDEZVOUS_SERVICE_URL=<peerapi-url>" --execute-now
```

The script is a *convenience helper* — feel free to adjust it to your CI/CD
pipeline or replace it with Terraform. 
## Browser demo

A simple web interface lives in [`webdemo/index.html`](webdemo/index.html). Serve this directory with any static file server and open the page in two browser windows. Enter the rendezvous multiaddress printed by the service and click **Connect**. Once a second peer joins you can exchange chat messages and watch the hole punching attempts in the browser console.

