package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/signal"
	"syscall"

	"github.com/libp2p/go-libp2p"
	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/peer"
	ws "github.com/libp2p/go-libp2p/p2p/transport/websocket"
	ma "github.com/multiformats/go-multiaddr"
)

// ProtocolIDForRegistration is the libp2p protocol ID used by workers to register with the rendezvous service.
// This should match the one defined in the rendezvous service.
const ProtocolIDForRegistration = "/holepunch/rendezvous/1.0.0"

// connectAndRegisterWithRendezvous connects the worker host to the rendezvous service at the given
// multiaddress string and performs a simple registration handshake (expecting an "ACK" response).
func connectAndRegisterWithRendezvous(ctx context.Context, h host.Host, rendezvousAddrStr string) error {
	// Parse multiaddr
	rendezvousMaddr, err := ma.NewMultiaddr(rendezvousAddrStr)
	if err != nil {
		return fmt.Errorf("invalid rendezvous multiaddr: %w", err)
	}

	addrInfo, err := peer.AddrInfoFromP2pAddr(rendezvousMaddr)
	if err != nil {
		return fmt.Errorf("failed to extract AddrInfo: %w", err)
	}

	// Connect
	if err := h.Connect(ctx, *addrInfo); err != nil {
		return fmt.Errorf("failed to connect to rendezvous: %w", err)
	}

	// Open stream
	stream, err := h.NewStream(ctx, addrInfo.ID, ProtocolIDForRegistration)
	if err != nil {
		return fmt.Errorf("failed to open registration stream: %w", err)
	}
	defer stream.Close()

	// Optionally send payload (not required)
	//_, _ = stream.Write([]byte("REGISTER"))

	// Wait for ACK (blocking read up to 3 bytes)
	ack := make([]byte, 3)
	if _, err := io.ReadFull(stream, ack); err != nil {
		return fmt.Errorf("failed to read ACK from rendezvous: %w", err)
	}
	if string(ack) != "ACK" {
		return fmt.Errorf("unexpected ACK response: %s", string(ack))
	}
	return nil
}

// createWorkerHost creates a new libp2p host, typically configured as a client.
// listenPort < 0 means no specific listen address by default.
// listenPort 0 means listen on a random OS-chosen port.
// listenPort > 0 means listen on that specific port.
func createWorkerHost(ctx context.Context, listenPort int) (host.Host, error) {
	var listenAddrs []string
	if listenPort >= 0 { // Use >=0 to allow explicit port 0 for random OS-chosen port, or a specific port
		tcpAddr := fmt.Sprintf("/ip4/0.0.0.0/tcp/%d", listenPort)
		wsAddr := fmt.Sprintf("/ip4/0.0.0.0/tcp/%d/ws", listenPort)
		listenAddrs = append(listenAddrs, tcpAddr, wsAddr)
	}

	h, err := libp2p.New(
		libp2p.ListenAddrStrings(listenAddrs...),
		libp2p.Transport(ws.New),
	)
	if err != nil {
		return nil, fmt.Errorf("failed to create libp2p host: %w", err)
	}
	return h, nil
}

func main() {
	// Command-line flags
	rendezvousAddrStr := flag.String("rendezvous", "", "Rendezvous server multiaddress (can alternatively be provided via RENDEZVOUS_MULTIADDR or RENDEZVOUS_ADDR env vars)")
	listenPort := flag.Int("listen-port", 0, "Port for the worker to listen on (0 for random, -1 for none)")
	flag.Parse()

	// ------------------------------------------------------------
	// Fallback to environment variables if flag not supplied.
	// This makes Cloud Run deployments simpler because you can now
	// pass the multiaddr with `--set-env-vars RENDEZVOUS_MULTIADDR=...`
	// instead of supplying container args.
	// ------------------------------------------------------------
	if *rendezvousAddrStr == "" {
		if envAddr := os.Getenv("RENDEZVOUS_MULTIADDR"); envAddr != "" {
			*rendezvousAddrStr = envAddr
		} else if envAddr2 := os.Getenv("RENDEZVOUS_ADDR"); envAddr2 != "" {
			*rendezvousAddrStr = envAddr2
		}
	}

	fmt.Println("Worker service starting...")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	h, err := createWorkerHost(ctx, *listenPort)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error creating worker host: %v\n", err)
		os.Exit(1)
	}

	fmt.Printf("Worker host created with ID: %s\n", h.ID().String())
	if len(h.Addrs()) > 0 {
		fmt.Println("Worker listening on addresses:")
		for _, addr := range h.Addrs() {
			fmt.Printf("  %s/p2p/%s\n", addr, h.ID().String())
		}
	} else {
		fmt.Println("Worker host not configured to listen on specific addresses.")
	}

	if *rendezvousAddrStr == "" {
		fmt.Println("No rendezvous server address provided. Worker will idle.")
		// TODO: In a real scenario, might exit or have other behavior
	} else {
		fmt.Printf("Rendezvous server: %s\n", *rendezvousAddrStr)

		err = connectAndRegisterWithRendezvous(ctx, h, *rendezvousAddrStr)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error connecting to rendezvous: %v\n", err)
			// Not exiting here, as we might still want the worker to run for other tests.
		} else {
			// Successfully registered, now try to discover peers
			fmt.Println("Worker successfully registered with rendezvous.")
			rendezvousHTTPURL := os.Getenv("RENDEZVOUS_SERVICE_URL")
			if rendezvousHTTPURL == "" {
				fmt.Println("RENDEZVOUS_SERVICE_URL not set, skipping peer discovery.")
			} else {
				peersURL := rendezvousHTTPURL + "/peers"
				fmt.Printf("Discovering peers from: %s\n", peersURL)
				resp, err := http.Get(peersURL)
				if err != nil {
					fmt.Fprintf(os.Stderr, "Error fetching peers: %v\n", err)
				} else {
					defer resp.Body.Close()
					if resp.StatusCode != http.StatusOK {
						bodyBytes, _ := io.ReadAll(resp.Body)
						fmt.Fprintf(os.Stderr, "Error fetching peers: status %s, body: %s\n", resp.Status, string(bodyBytes))
					} else {
						// The rendezvous service returns a map[peer.ID]ma.Multiaddr
						// For the client, it's easier to decode into map[string]string (peer.ID string to multiaddr string)
						// and then convert as needed.
						// var discoveredPeers map[string]string //peer.ID can't be a JSON map key directly in simple decoding
						// The actual structure is map[peer.ID]ma.Multiaddr, so we'll decode to map[string]string
						// and then process. The NEXT_STEPS guide implies a `peerList` which is an array/slice.
						// Let's assume for now the /peers endpoint returns a structure that can be decoded into `peerList`.
						// The rendezvous implementation returns map[peer.ID]ma.Multiaddr.
						// For simplicity with the NEXT_STEPS.md snippet, we'll assume it provides a list of AddrInfo or similar.
						// However, the actual rendezvous code returns a map.
						// Let's adapt to what rendezvous *actually* provides.
						// The registeredPeers map in rendezvous is map[peer.ID]ma.Multiaddr.
						// JSON encoding of this will be map[string]string (peer.ID.String() -> ma.Multiaddr.String())
						var peersMap map[string]string
						if err := json.NewDecoder(resp.Body).Decode(&peersMap); err != nil {
							fmt.Fprintf(os.Stderr, "Error decoding peers response: %v\n", err)
						} else {
							fmt.Printf("Discovered %d peers:\n", len(peersMap))
							for pidStr, addrStr := range peersMap {
								fmt.Printf("  Peer ID: %s, Addr: %s\n", pidStr, addrStr)
								// Here you could try to convert pidStr to peer.ID and addrStr to ma.Multiaddr
								// and then potentially to peer.AddrInfo for dialing, as suggested by step 2.2.
							}
							// The NEXT_STEPS.md snippet for worker (section 1.3) is:
							// peersResp, _ := http.Get(os.Getenv("RENDEZVOUS_SERVICE_URL") + "/peers")
							// This implies the response might be directly usable or further processed.
							// For step 1.3, just getting the response is shown.
							// We've added printing the decoded peers.
						}
					}
				}
			}
		}
	}

	// Start health server for Cloud Run
	startHealthServer()

	fmt.Println("Worker service is running. Press Ctrl+C to stop.")
	ch := make(chan os.Signal, 1)
	signal.Notify(ch, syscall.SIGINT, syscall.SIGTERM)
	<-ch
	fmt.Println("\nReceived signal, shutting down worker...")

	if err := h.Close(); err != nil {
		fmt.Fprintf(os.Stderr, "Error closing worker host: %v\n", err)
	}
	fmt.Println("Worker service stopped.")
}

// startHealthServer launches a tiny HTTP server that always returns 200 OK.
// Cloud Run pings this port (from $PORT env, default 8080) to determine readiness.
func startHealthServer() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}
	go func() {
		http.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte("OK"))
		})
		if err := http.ListenAndServe(":"+port, nil); err != nil {
			fmt.Fprintf(os.Stderr, "health server error: %v\n", err)
		}
	}()
}
