package main

import (
	"context"
	"database/sql" // For type assertion if bertydb.DB is a wrapper around *sql.DB
	"fmt"
	"io" // For io.Closer
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time" // For graceful shutdown timeout
	// "time" // No longer explicitly needed for this simplified version

	"github.com/libp2p/go-libp2p"
	host "github.com/libp2p/go-libp2p/core/host"
	// "github.com/libp2p/go-libp2p/core/peer" // Not directly used now
	"github.com/libp2p/go-libp2p/p2p/protocol/circuitv2/relay"
	// Old rendezvous import: "github.com/libp2p/go-libp2p/p2p/protocol/rendezvous"
	ma "github.com/multiformats/go-multiaddr"

	bertyrendezvous "github.com/berty/go-libp2p-rendezvous"
	bertydb "github.com/berty/go-libp2p-rendezvous/db/sqlite"
)

const (
	libp2pPort      = 7777
	httpPortEnv     = "PORT"
	defaultHttpPort = "8080"
)

var globalHost host.Host

func main() {
	ctx, cancelMain := context.WithCancel(context.Background())
	defer cancelMain()

	listenAddrStr := fmt.Sprintf("/ip4/0.0.0.0/udp/%d/quic-v1", libp2pPort)

	// Pass main context to libp2p.New if an appropriate option exists in the version used.
	// For now, assuming New() doesn't require it directly or manages its own context for basic setup.
	node, err := libp2p.New(
		libp2p.ListenAddrStrings(listenAddrStr),
	)
	if err != nil {
		log.Fatalf("Failed to create libp2p host: %v", err)
	}
	globalHost = node
	defer node.Close()

	log.Printf("Libp2p Host created with ID: %s", node.ID().String())
	log.Println("Listening for libp2p connections on:")
	for _, addr := range node.Addrs() {
		log.Printf("  %s/p2p/%s", addr, node.ID().String())
	}

	// --- Rendezvous Service Setup (using Berty's implementation) ---
	dbInstance, err := bertydb.New(ctx, ":memory:") // Use New() and pass context
	if err != nil {
		log.Fatalf("Failed to open in-memory database for rendezvous: %v", err)
	}
	if closer, ok := dbInstance.(io.Closer); ok {
		defer closer.Close()
		log.Println("Scheduled rendezvous DB close via io.Closer.")
	} else {
		// Attempt a more specific type assertion if SQLiteDB has a direct Close or a way to get *sql.DB
		if specificDb, ok := dbInstance.(interface{ GetDB() *sql.DB }); ok {
			actualSQLDB := specificDb.GetDB()
			if actualSQLDB != nil {
				defer actualSQLDB.Close()
				log.Println("Scheduled rendezvous DB close via GetDB().")
			}
		} else {
			log.Println("Rendezvous DB does not implement io.Closer or GetDB() for specific *sql.DB Close. Manual check of bertydb recommended if leaks occur.")
		}
	}

	_ = bertyrendezvous.NewRendezvousService(node, dbInstance) // Assign to blank identifier
	log.Println("Berty Libp2p Rendezvous service started.")

	// --- Circuit Relay (Optional but good for connect-ability) ---
	_, err = relay.New(node)
	if err != nil {
		log.Printf("Failed to instantiate circuit relay: %v", err)
	} else {
		log.Println("Libp2p Circuit Relay V2 service started.")
	}

	// --- HTTP Server to Announce Multiaddrs ---
	httpListenPort := os.Getenv(httpPortEnv)
	if httpListenPort == "" {
		httpListenPort = defaultHttpPort
	}

	httpMux := http.NewServeMux()
	httpMux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		if globalHost == nil {
			http.Error(w, "Libp2p host not initialized yet", http.StatusInternalServerError)
			return
		}
		fmt.Fprintf(w, "<h1>Berty Libp2p Rendezvous Server</h1>")
		fmt.Fprintf(w, "<p>Host ID: %s</p>", globalHost.ID().String())
		fmt.Fprintf(w, "<p>Internally listening on (clients need to replace 0.0.0.0 with the server's public IP/DNS and correct port if NATed/proxied):</p><ul>")
		for _, addr := range globalHost.Addrs() {
			if !strings.Contains(addr.String(), "127.0.0.1") {
				fullAddr := fmt.Sprintf("%s/p2p/%s", addr.String(), globalHost.ID().String())
				fmt.Fprintf(w, "<li>%s</li>", fullAddr)
			}
		}
		fmt.Fprintf(w, "</ul>")
		fmt.Fprintf(w, "<p><b>Note for clients:</b> You will need to construct the publicly reachable multiaddress. "+
			"If this server is behind NAT/proxy (like Cloud Run), the IP address above (0.0.0.0) must be replaced "+
			"with the server's public IP address and the correct public port for the QUIC listener (e.g., /ip4/YOUR_PUBLIC_IP/udp/%d/quic-v1/p2p/%s).", libp2pPort, globalHost.ID().String())
	})

	httpServer := &http.Server{
		Addr:    ":" + httpListenPort,
		Handler: httpMux,
	}

	go func() {
		log.Printf("HTTP server starting on port %s", httpListenPort)
		if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("HTTP server failed: %v", err)
		}
	}()

	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)
	<-stop // Wait for SIGINT

	log.Println("Shutting down server...")

	shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer shutdownCancel()

	if err := httpServer.Shutdown(shutdownCtx); err != nil {
		log.Printf("HTTP server Shutdown Failed: %+v", err)
	}
	
	// node.Close() is deferred
	log.Println("Server gracefully stopped.")
}

// getPublicMultiaddrs function remains the same as it's a general helper
// and not directly tied to the rendezvous implementation details beyond needing the host.
func getPublicMultiaddrs(h host.Host, publicIPOrDNS string, publicPort int) ([]ma.Multiaddr, error) {
	var publiclyReachableAddrs []ma.Multiaddr
	for _, baseAddr := range h.Addrs() {
		if strings.Contains(baseAddr.String(), "/ip4/0.0.0.0/") || strings.Contains(baseAddr.String(), "/ip6/::/"){
			protocolStack := ""
			if strings.Contains(baseAddr.String(), "/udp/") && strings.Contains(baseAddr.String(), "/quic-v1") {
				protocolStack = fmt.Sprintf("/udp/%d/quic-v1", publicPort)
			} else if strings.Contains(baseAddr.String(), "/tcp/") {
				protocolStack = fmt.Sprintf("/tcp/%d", publicPort) 
			}

			if protocolStack != "" {
				var newAddrStr string
				if strings.Contains(publicIPOrDNS, ":") && !strings.Contains(publicIPOrDNS, ".") {
					newAddrStr = fmt.Sprintf("/ip6/%s%s/p2p/%s", publicIPOrDNS, protocolStack, h.ID().String())
				} else {
					newAddrStr = fmt.Sprintf("/ip4/%s%s/p2p/%s", publicIPOrDNS, protocolStack, h.ID().String())
				}
				
				mAddr, err := ma.NewMultiaddr(newAddrStr)
				if err == nil {
					publiclyReachableAddrs = append(publiclyReachableAddrs, mAddr)
				} else {
					log.Printf("Error creating multiaddr from %s: %v", newAddrStr, err)
				}
			}
		}
	}
	if len(publiclyReachableAddrs) == 0 {
		return nil, fmt.Errorf("could not determine any publicly reachable multiaddrs for host %s with public IP/DNS %s and port %d", h.ID(), publicIPOrDNS, publicPort)
	}
	return publiclyReachableAddrs, nil
} 