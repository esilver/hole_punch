package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"

	"github.com/libp2p/go-libp2p"
	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/network"
	"github.com/libp2p/go-libp2p/core/peer"
	ws "github.com/libp2p/go-libp2p/p2p/transport/websocket"
	ma "github.com/multiformats/go-multiaddr"
)

// DefaultPort for the rendezvous service to listen on.
const DefaultPort = 40001

// ProtocolIDForRegistration is the libp2p protocol ID used by workers to register.
const ProtocolIDForRegistration = "/holepunch/rendezvous/1.0.0"

// registeredPeers stores the PeerID and last known public Multiaddr of registered workers.
// This is a simple in-memory store, not suitable for production without persistence and proper synchronization.
var registeredPeers = make(map[peer.ID]ma.Multiaddr)
var registeredPeersMutex = &sync.Mutex{}

// registrationHandler is called when a worker connects using ProtocolIDForRegistration.
func registrationHandler(s network.Stream) {
	remotePeerID := s.Conn().RemotePeer()
	remoteAddr := s.Conn().RemoteMultiaddr()

	fmt.Printf("Rendezvous: Received registration stream from %s (%s)\n", remotePeerID, remoteAddr)

	registeredPeersMutex.Lock()
	registeredPeers[remotePeerID] = remoteAddr
	registeredPeersMutex.Unlock()

	fmt.Printf("Rendezvous: Worker %s registered with address %s. Total registered: %d\n", remotePeerID, remoteAddr, len(registeredPeers))

	// If a peer discovery service URL is set, forward this registration.
	if peerAPI := os.Getenv("PEER_DISCOVERY_URL"); peerAPI != "" {
		payload := map[string]string{"id": remotePeerID.String(), "addr": remoteAddr.String()}
		if data, err := json.Marshal(payload); err == nil {
			_, err := http.Post(fmt.Sprintf("%s/peers", peerAPI), "application/json", bytes.NewReader(data))
			if err != nil {
				fmt.Printf("Rendezvous: Failed to replicate to discovery API: %v\n", err)
			}
		}
	}

	// Optional: Read any data sent by the worker on this stream.
	// For example, the worker might send its list of listen addresses.
	// buf := make([]byte, 1024)
	// n, err := s.Read(buf)
	// if err != nil && err != io.EOF {
	// 	fmt.Printf("Rendezvous: Error reading from registration stream for %s: %v\n", remotePeerID, err)
	// 	s.Reset() // Reset the stream on error
	// 	return
	// }
	// if n > 0 {
	// 	fmt.Printf("Rendezvous: Received payload from %s: %s\n", remotePeerID, string(buf[:n]))
	// 	// TODO: Process payload, e.g., update stored addresses for the peer.
	// }

	// Send an acknowledgment back to the worker.
	_, err := s.Write([]byte("ACK"))
	if err != nil {
		fmt.Printf("Rendezvous: Error writing ACK to %s: %v\n", remotePeerID, err)
		s.Reset()
		return
	}

	// It's good practice to close the stream when done if the protocol is request-response.
	// However, if the stream is meant to be kept alive for other purposes (like presence), don't close it here.
	// For a simple registration, closing after ACK is fine.
	if err := s.Close(); err != nil {
		fmt.Printf("Rendezvous: Error closing stream for %s: %v\n", remotePeerID, err)
	}
	fmt.Printf("Rendezvous: Registration for %s complete. Stream closed.\n", remotePeerID)
}

// createHost is a helper function that can be used in main.go and for testing.
// It creates a new libp2p host with a default set of options.
func createHost(ctx context.Context, listenPort int) (host.Host, error) {
	// Listen on TCP and WebSocket on the same port (Cloud Run gives us only one).
	tcpAddr := fmt.Sprintf("/ip4/0.0.0.0/tcp/%d", listenPort)
	wsAddr := fmt.Sprintf("/ip4/0.0.0.0/tcp/%d/ws", listenPort)

	h, err := libp2p.New(
		libp2p.ListenAddrStrings(tcpAddr, wsAddr),
		libp2p.Transport(ws.New),
	)
	if err != nil {
		return nil, fmt.Errorf("failed to create libp2p host: %w", err)
	}
	return h, nil
}

func main() {
	port := DefaultPort
	if pStr := os.Getenv("PORT"); pStr != "" {
		if p, err := strconv.Atoi(pStr); err == nil {
			port = p
		}
	}
	fmt.Printf("Rendezvous service starting... Listening on port %d\n", port)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	h, err := createHost(ctx, port)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error creating host: %v\n", err)
		os.Exit(1)
	}

	fmt.Printf("Host created with ID: %s\n", h.ID().String())
	fmt.Println("Listening on addresses:")
	for _, addr := range h.Addrs() {
		fmt.Printf("  %s/p2p/%s\n", addr, h.ID().String())
	}

	// Set the stream handler for worker registrations
	h.SetStreamHandler(ProtocolIDForRegistration, registrationHandler)
	fmt.Printf("Set stream handler for protocol: %s\n", ProtocolIDForRegistration)

	// -------------------------------------------------------------------
	// HTTP Peer-Discovery API
	// -------------------------------------------------------------------
	// Running an HTTP server on the SAME port as the libp2p listeners leads
	// to 400 Bad Request responses because the websocket transport's upgrade
	// handler rejects non-websocket traffic. To avoid this conflict, we expose
	// the discovery API on a **separate** HTTP port. By default this is 8080
	// or you can override with HTTP_PORT env-var.

	// Optional internal HTTP peer list API (useful for local dev). Only started
	// when ENABLE_INTERNAL_HTTP="1" (default "0" to prevent Cloud Run port clashes).
	if os.Getenv("ENABLE_INTERNAL_HTTP") == "1" {
		httpPort := 8080
		if hpStr := os.Getenv("HTTP_PORT"); hpStr != "" {
			if hp, err := strconv.Atoi(hpStr); err == nil {
				httpPort = hp
			}
		}

		// Register handlers on the default mux.
		http.HandleFunc("/peers", func(w http.ResponseWriter, r *http.Request) {
			registeredPeersMutex.Lock()
			defer registeredPeersMutex.Unlock()
			w.Header().Set("Content-Type", "application/json")
			if err := json.NewEncoder(w).Encode(registeredPeers); err != nil {
				http.Error(w, err.Error(), http.StatusInternalServerError)
				return
			}
		})

		http.HandleFunc("/peers/", func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodDelete {
				http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
				return
			}
			idStr := strings.TrimPrefix(r.URL.Path, "/peers/")
			pid, err := peer.Decode(idStr)
			if err != nil {
				http.Error(w, "Invalid peer ID", http.StatusBadRequest)
				return
			}
			registeredPeersMutex.Lock()
			delete(registeredPeers, pid)
			registeredPeersMutex.Unlock()
			// Propagate deletion to peer discovery service if configured.
			if peerAPI := os.Getenv("PEER_DISCOVERY_URL"); peerAPI != "" {
				req, _ := http.NewRequest(http.MethodDelete, fmt.Sprintf("%s/peers/%s", peerAPI, pid.String()), nil)
				if _, err := http.DefaultClient.Do(req); err != nil {
					fmt.Printf("Rendezvous: Failed to propagate delete to discovery API: %v\n", err)
				}
			}
			w.WriteHeader(http.StatusNoContent)
			fmt.Printf("Rendezvous: Peer %s deleted via API\n", pid)
		})

		// Start the HTTP server in a goroutine so it doesn't block.
		go func() {
			addr := fmt.Sprintf(":%d", httpPort)
			fmt.Printf("Rendezvous: INTERNAL HTTP peer API listening on %s (endpoints: /peers, /peers/{id})\n", addr)
			if err := http.ListenAndServe(addr, nil); err != nil {
				fmt.Fprintf(os.Stderr, "Rendezvous HTTP server error: %v\n", err)
			}
		}()
	}

	// TODO: Implement rendezvous logic here.
	// For now, the service will just start, print its addresses, and wait for a signal.

	fmt.Println("Rendezvous service is running. Press Ctrl+C to stop.")

	// Wait for a SIGINT (Ctrl+C) or SIGTERM signal to gracefully shutdown.
	ch := make(chan os.Signal, 1)
	signal.Notify(ch, syscall.SIGINT, syscall.SIGTERM)
	<-ch
	fmt.Println("\nReceived signal, shutting down...")

	if err := h.Close(); err != nil {
		fmt.Fprintf(os.Stderr, "Error closing host: %v\n", err)
	}
	fmt.Println("Rendezvous service stopped.")
}

// getPeerAddr is a utility function that can be moved to a common package later.
func getPeerAddr(p host.Host) (ma.Multiaddr, error) {
	addrs := p.Addrs()
	if len(addrs) == 0 {
		return nil, fmt.Errorf("host has no listen addresses")
	}
	// Prefer non-loopback, public addresses if available, but for now, just pick the first one.
	// More sophisticated logic might be needed for NAT traversal.
	// Example: /ip4/192.168.1.23/tcp/4001/p2p/Qm... or /ip4/0.0.0.0/tcp/4001/p2p/Qm... which resolves to all interfaces.
	// We need to append /p2p/<peerID> to the address.
	fullAddr, err := ma.NewMultiaddr(fmt.Sprintf("%s/p2p/%s", addrs[0].String(), p.ID().String()))
	if err != nil {
		return nil, fmt.Errorf("failed to create full multiaddr: %w", err)
	}
	return fullAddr, nil
}
