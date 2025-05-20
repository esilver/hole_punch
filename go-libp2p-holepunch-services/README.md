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

## Next steps

* Implement `/list` protocol so workers can discover each other.
* Enable AutoNAT / AutoRelay and attempt direct or relayed connections.
* Containerise each service (`Dockerfile`) and deploy to Cloud Run behind Cloud NAT. 