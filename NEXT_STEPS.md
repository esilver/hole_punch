# Next Steps for go-libp2p-holepunch-services

> This guide assumes you have already deployed the demo (`deploy_demo_cloud_run.sh`) and verified that workers can register with the rendezvous service.
>
> Each section below is independent – jump straight to the part you care about.
>
> Wherever lines start with `$` they are intended to be run in your **local** shell; commands starting with `gcloud`/`kubectl`/etc. assume you have authenticated and set the correct project.

---

## 1  Expose a Discovery API

### 1.1  Why?
* Today the rendezvous node only writes registrations to an in-memory map and logs the event – other peers have no way to discover who else is online.
* A tiny HTTP + libp2p handler lets any client query the list of peers, making the service useful beyond a demo.

### 1.2  Implementation
1. **Add an HTTP server** to `rendezvous/main.go` right after `startHealthServer()` (or copy that helper):
   ```go
   http.HandleFunc("/peers", func(w http.ResponseWriter, _ *http.Request) {
       registeredPeersMutex.Lock()
       defer registeredPeersMutex.Unlock()
       json.NewEncoder(w).Encode(registeredPeers)
   })
   // optional delete
   http.HandleFunc("/peers/", func(w http.ResponseWriter, r *http.Request) {
       idStr := strings.TrimPrefix(r.URL.Path, "/peers/")
       pid, err := peer.Decode(idStr)
       if err != nil { http.Error(w, err.Error(), 400); return }
       registeredPeersMutex.Lock(); delete(registeredPeers, pid); registeredPeersMutex.Unlock()
       w.WriteHeader(http.StatusNoContent)
   })
   ```
2. **Re-deploy** rendezvous. Pull the URL:
   ```bash
   RV_URL=$(gcloud run services describe rendezvous --region us-central1 \ 
                --format='value(status.url)')
   curl "$RV_URL/peers"
   ```
3. **Add helper to worker** (`worker/main.go`) right after registration:
   ```go
   peersResp, _ := http.Get(os.Getenv("RENDEZVOUS_SERVICE_URL") + "/peers")
   ```

---

## 2  Peer-to-Peer Connections & Hole-Punching

### 2.1  Enable hole-punching features
```go
import (
    ph "github.com/libp2p/go-libp2p/p2p/protocol/holepunch"
)

h, _ := libp2p.New(
    libp2p.EnableHolePunching(),   // activates AutoRelay + NatManager
    libp2p.EnableAutoRelay(),
    libp2p.NATPortMap(),
)
ph.Log.Debug = true // detailed traces
```
*Keep WebSocket transport because Cloud Run only accepts TCP/HTTPS.*

### 2.2  Discover another peer & dial
```go
for _, info := range peerList {    // from /peers endpoint
    if info.ID == h.ID() {continue}
    if err := h.Connect(ctx, info); err == nil {
        fmt.Println("Direct connection established!")
        break
    }
}
```
Watch logs for `holepunch:` lines – they confirm fallback-to-relay and punch success.

### 2.3  Run a relay (optional)
If most peers sit behind NATs you'll need a public relay. Start a VM:
```bash
curl -fsSL https://raw.githubusercontent.com/libp2p/go-libp2p/master/cmd/relay/relay.go | go run - -p 4001
```
Point workers at the relay via `LIBP2P_RELAY_BOOTSTRAP=<multiaddr>` or `AutoRelay` will discover it automatically if the relay advertises itself.

---

## 3  Add QUIC Transport (when not on Cloud Run)

Cloud Run blocks UDP. If you migrate to GKE Autopilot, Compute Engine, or bare-metal:
```go
import quic "github.com/libp2p/go-libp2p/p2p/transport/quic"

libp2p.New(
  libp2p.Transport(quic.NewTransport),
  libp2p.ListenAddrStrings("/ip4/0.0.0.0/udp/4001/quic-v1"),
)
```
*QUIC + hole-punching performs better across NATs than TCP.*

---

## 4  Security & Authentication

| Problem                              | Solution                                                        |
|--------------------------------------|-----------------------------------------------------------------|
| Anyone can register fake peer IDs    | Sign challenge with peer's private key (verify in handler)      |
| Public endpoint reachable by anyone  | Put Cloud Run behind Cloud Load-Balancer with Cloud Armor rules |
| Replay attacks                       | Include timestamp/nonce in the signed payload                   |

Skeleton for signed registration payload:
```go
msg := []byte(time.Now().UTC().Format(time.RFC3339))
signature, _ := h.Peerstore().PrivKey(h.ID()).Sign(msg)
stream.Write(append(signature, msg...))
```
Verify in `registrationHandler`.

---

## 5  Observability

1. **Metrics** – add Prometheus exporter:
   ```go
   import prom "github.com/prometheus/client_golang/prometheus/promhttp"
   http.Handle("/metrics", prom.Handler())
   ```
   • scrape via Cloud Run → Cloud Monitoring custom metrics
2. **Tracing** – use OpenTelemetry SDK (`go.opentelemetry.io/otel`) and export to Cloud Trace.
3. **Structured logs** – replace `fmt.Printf` with `zap` or `zerolog` JSON logs (Cloud Logging parses them automatically).

---

## 6  CI / CD Pipeline in Cloud Build

`cloudbuild.yaml` (root):
```yaml
steps:
- name: golang:1.22
  entrypoint: bash
  args: ["-c", "go test ./..."]
- name: gcr.io/cloud-builders/docker
  args: ["build", "-t", "${_REGION}-docker.pkg.dev/$PROJECT_ID/rendezvous-repo/rendezvous:$SHORT_SHA", "go-libp2p-holepunch-services/rendezvous"]
- name: gcr.io/cloud-builders/gcloud
  args: ["run", "deploy", "rendezvous", "--image", "${_REGION}-docker.pkg.dev/$PROJECT_ID/rendezvous-repo/rendezvous:$SHORT_SHA", "--region", "${_REGION}", "--platform=managed", "--allow-unauthenticated"]
substitutions:
  _REGION: us-central1
```
Trigger on GitHub `main` push; repeat for worker.

---

## 7  Web Demo (browser ↔ browser)

1. **Compile a js-libp2p bundle** with WebSocket + WebRTC transports.
2. Serve a static HTML page from Cloud Run `/` (same container or Cloud Storage).
3. Browser A clicks "Host", registers.
4. Browser B clicks "Join", fetches `/peers`, dials peer-ID.
5. Exchange chat messages; show connection state (direct / relay / hole-punched).

See the [js-libp2p examples](https://github.com/libp2p/js-libp2p/tree/master/examples/chat) – swap the hard-coded relay with the rendezvous URL you built above.

---

### Got questions?
Open an issue or ping @your-github-handle – happy hacking! 