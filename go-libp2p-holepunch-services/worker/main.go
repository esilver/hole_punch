package main

import (
	"context"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"github.com/libp2p/go-libp2p"
	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/peer"
	tptquic "github.com/libp2p/go-libp2p/p2p/transport/quic"
	tptudp "github.com/libp2p/go-libp2p/p2p/transport/udp"
	tptws "github.com/libp2p/go-libp2p/p2p/transport/websocket"
	ma "github.com/multiformats/go-multiaddr"
)

// ProtocolIDForRegistration is the libp2p protocol ID used by workers to register with the rendezvous service.
// This should match the one defined in the rendezvous service.
const ProtocolIDForRegistration = "/holepunch/rendezvous/1.0.0"

// ProtocolIDForPeerList matches the rendezvous service's peer list protocol.
const ProtocolIDForPeerList = "/holepunch/list/1.0.0"

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

// discoverPeersViaListProtocol queries the rendezvous service for a list of peers
// using the libp2p peer list protocol.
func discoverPeersViaListProtocol(ctx context.Context, h host.Host, rendezvousAddrStr string) ([]peer.AddrInfo, error) {
	rendezvousMaddr, err := ma.NewMultiaddr(rendezvousAddrStr)
	if err != nil {
		return nil, fmt.Errorf("invalid rendezvous multiaddr: %w", err)
	}
	addrInfo, err := peer.AddrInfoFromP2pAddr(rendezvousMaddr)
	if err != nil {
		return nil, fmt.Errorf("failed to extract AddrInfo: %w", err)
	}

	s, err := h.NewStream(ctx, addrInfo.ID, ProtocolIDForPeerList)
	if err != nil {
		return nil, fmt.Errorf("failed to open list stream: %w", err)
	}
	defer s.Close()

	data, err := io.ReadAll(s)
	if err != nil {
		return nil, fmt.Errorf("failed reading list response: %w", err)
	}

	var peers []peer.AddrInfo
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	for _, line := range lines {
		if line == "" {
			continue
		}
		maddr, err := ma.NewMultiaddr(line)
		if err != nil {
			continue
		}
		info, err := peer.AddrInfoFromP2pAddr(maddr)
		if err != nil {
			continue
		}
		peers = append(peers, *info)
	}
	return peers, nil
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
		udpAddr := fmt.Sprintf("/ip4/0.0.0.0/udp/%d", listenPort)
		quicAddr := fmt.Sprintf("/ip4/0.0.0.0/udp/%d/quic-v1", listenPort)
		listenAddrs = append(listenAddrs, tcpAddr, wsAddr, udpAddr, quicAddr)
	}

	h, err := libp2p.New(
		libp2p.ListenAddrStrings(listenAddrs...),
		libp2p.Transport(tptws.New),
		libp2p.Transport(tptquic.NewTransport),
		libp2p.Transport(tptudp.NewUDPTransport),
		libp2p.EnableHolePunching(),
		libp2p.NATPortMap(),
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
			fmt.Fprintf(os.Stderr, "Error connecting to rendezvous:%v\n", err)
		} else {
			fmt.Println("Worker successfully registered with rendezvous.")

			peers, err := discoverPeersViaListProtocol(ctx, h, *rendezvousAddrStr)
			if err != nil {
				fmt.Fprintf(os.Stderr, "Peer discovery error: %v\n", err)
			} else {
				for _, info := range peers {
					if info.ID == h.ID() {
						continue
					}
					if err := h.Connect(ctx, info); err == nil {
						fmt.Printf("Connected to peer %s\n", info.ID)
					} else {
						fmt.Printf("Failed to connect to peer %s: %v\n", info.ID, err)
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
