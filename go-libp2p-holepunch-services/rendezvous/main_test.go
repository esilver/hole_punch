package main

import (
	"context"
	"fmt"
	"io"
	"strings"
	"testing"
	"time"

	libp2p "github.com/libp2p/go-libp2p"
	"github.com/libp2p/go-libp2p/core/network"
	"github.com/libp2p/go-libp2p/core/peer"
	ma "github.com/multiformats/go-multiaddr"
	"github.com/stretchr/testify/require"
)

func TestCreateLibp2pHost(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Use port 0 to let the OS pick a free port for testing.
	h, err := createHost(ctx, 0)
	require.NoError(t, err, "Failed to create libp2p host")
	require.NotNil(t, h, "Created host should not be nil")

	t.Logf("Successfully created host with ID: %s", h.ID())
	t.Logf("Host listening on addresses: %v", h.Addrs())
	require.NotEmpty(t, h.Addrs(), "Host should be listening on at least one address")

	err = h.Close()
	require.NoError(t, err, "Failed to close host")
}

// Store for registered peers in tests. In main.go, this would be part of the server state.
var (
	testRegisteredPeers = make(map[peer.ID]ma.Multiaddr)
)

// simpleRegistrationHandlerForTest is a basic stream handler for testing purposes.
// It reads a message (expected to be the worker's listen address, or an empty message for simplicity) and stores the remote peer's info.
func simpleRegistrationHandlerForTest(s network.Stream) {
	defer s.Close()
	remotePeerID := s.Conn().RemotePeer()
	remoteAddr := s.Conn().RemoteMultiaddr()

	testRegisteredPeers[remotePeerID] = remoteAddr // Storing the observed address from the connection
	fmt.Printf("[Test Handler] Registered peer %s with address %s\n", remotePeerID, remoteAddr)

	// Optionally, read a message from the worker if the protocol defines one.
	// For now, just establishing the stream is enough for registration.
	// buf := make([]byte, 256)
	// n, err := s.Read(buf)
	// if err != nil && err != io.EOF {
	// 	fmt.Printf("[Test Handler] Error reading from stream: %v\n", err)
	// 	return
	// }
	// fmt.Printf("[Test Handler] Received message from %s: %s\n", remotePeerID, string(buf[:n]))

	// Send a simple ack
	_, err := s.Write([]byte("ACK"))
	if err != nil {
		fmt.Printf("[Test Handler] Error writing ACK: %v\n", err)
	}
}

func TestWorkerRegistersWithRendezvous(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// 1. Create Rendezvous Host (Server)
	rendezvousHost, err := createHost(ctx, 0)
	require.NoError(t, err, "Failed to create rendezvous host")
	defer rendezvousHost.Close()

	t.Logf("Rendezvous host created with ID: %s, Addrs: %v", rendezvousHost.ID(), rendezvousHost.Addrs())

	// Reset test store for this test
	testRegisteredPeers = make(map[peer.ID]ma.Multiaddr)
	// Set the stream handler on the rendezvous host
	rendezvousHost.SetStreamHandler(ProtocolIDForRegistration, simpleRegistrationHandlerForTest)

	// 2. Create Worker Host (Client)
	// For the worker host, we use createHost from main.go for simplicity in testing.
	workerHost, err := createHost(ctx, 0)
	require.NoError(t, err, "Failed to create worker host")
	defer workerHost.Close()
	t.Logf("Worker host created with ID: %s, Addrs: %v", workerHost.ID(), workerHost.Addrs())

	// 3. Worker Connects to Rendezvous Host
	rendezvousAddrInfo := peer.AddrInfo{
		ID:    rendezvousHost.ID(),
		Addrs: rendezvousHost.Addrs(),
	}
	err = workerHost.Connect(ctx, rendezvousAddrInfo)
	require.NoError(t, err, "Worker failed to connect to rendezvous host")
	t.Logf("Worker successfully connected to rendezvous host.")

	// 4. Worker Opens a New Stream for Registration
	stream, err := workerHost.NewStream(ctx, rendezvousHost.ID(), ProtocolIDForRegistration)
	require.NoError(t, err, "Worker failed to open new stream to rendezvous host")
	t.Logf("Worker opened registration stream to rendezvous host.")

	// Optionally, worker sends some data (e.g., its listen addrs or a specific registration payload)
	// For this test, establishing the stream might be enough, and the handler uses the connection's remote addr.
	// _, err = stream.Write([]byte("Hello from worker"))
	// require.NoError(t, err, "Worker failed to write to stream")

	// Wait for ACK or for handler to process
	ackBuf := make([]byte, 3)
	_, err = stream.Read(ackBuf)
	require.NoError(t, err, "Worker failed to read ACK from rendezvous")
	require.Equal(t, "ACK", string(ackBuf), "Worker did not receive correct ACK")
	t.Logf("Worker received ACK from rendezvous.")

	stream.Close() // Close the stream from the worker side

	// 5. Verify Rendezvous Service Recorded the Worker
	// Allow some time for the handler to execute, though with direct connect it should be quick.
	// For more robust checks, might need a channel or wait group if handler is more complex.
	_, ok := testRegisteredPeers[workerHost.ID()]
	require.True(t, ok, "Rendezvous service did not register the worker host ID")
	// We can also check if the address stored is one of the worker's, but the remote multiaddr
	// from the connection (s.Conn().RemoteMultiaddr()) will be the one seen by the rendezvous host.
	t.Logf("Successfully verified that rendezvous service registered worker %s with addr %s", workerHost.ID(), testRegisteredPeers[workerHost.ID()])
}

func TestRendezvousServiceRuns(t *testing.T) {
	// This is a placeholder test.
	// In a real scenario, we'd start the service and check its status.
	t.Log("Placeholder test for rendezvous service. Needs actual implementation.")
	// For now, we can try to build and run the main func if it's simple enough,
	// but a proper test would involve more.
	if false { // Keep this false until main() is testable
		go main()
		// Add assertions here if main() was to run and exit cleanly for a test.
	}
}

// TestCreateHostIncludesWebsocket ensures createHost publishes at least one
// listen address that contains the `/ws` WebSocket component.
func TestCreateHostIncludesWebsocket(t *testing.T) {
	ctx := context.Background()
	h, err := createHost(ctx, 0) // random free port
	if err != nil {
		t.Fatalf("createHost returned error: %v", err)
	}
	defer h.Close()

	foundWS := false
	for _, addr := range h.Addrs() {
		if strings.Contains(addr.String(), "/ws") {
			foundWS = true
			break
		}
	}
	if !foundWS {
		t.Fatalf("expected at least one /ws address, got %v", h.Addrs())
	}
}

// TestWorkerRegistration end-to-end: spin up a rendezvous host, a worker host,
// have the worker connect & register, and assert we receive the "ACK".
func TestWorkerRegistration(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// Rendezvous host
	rendezvousHost, err := createHost(ctx, 0)
	if err != nil {
		t.Fatalf("createHost (rendezvous) error: %v", err)
	}
	defer rendezvousHost.Close()
	rendezvousHost.SetStreamHandler(ProtocolIDForRegistration, simpleRegistrationHandlerForTest)

	// Worker host
	workerHost, err := libp2p.New()
	if err != nil {
		t.Fatalf("worker host creation error: %v", err)
	}
	defer workerHost.Close()

	// Build full multiaddr for rendezvous host (we'll just pick the first addr).
	if len(rendezvousHost.Addrs()) == 0 {
		t.Fatal("rendezvous host has no listen addresses")
	}
	baseAddr := rendezvousHost.Addrs()[0]
	fullAddr := baseAddr.Encapsulate(ma.StringCast("/p2p/" + rendezvousHost.ID().String()))
	addrInfo, err := peer.AddrInfoFromP2pAddr(fullAddr)
	if err != nil {
		t.Fatalf("AddrInfoFromP2pAddr error: %v", err)
	}

	// Connect worker -> rendezvous
	if err := workerHost.Connect(ctx, *addrInfo); err != nil {
		t.Fatalf("worker connect error: %v", err)
	}

	// Open registration stream
	stream, err := workerHost.NewStream(ctx, rendezvousHost.ID(), ProtocolIDForRegistration)
	if err != nil {
		t.Fatalf("open stream error: %v", err)
	}
	defer stream.Close()

	// Read ACK (3 bytes)
	ack := make([]byte, 3)
	if _, err := io.ReadFull(stream, ack); err != nil {
		t.Fatalf("reading ACK failed: %v", err)
	}
	if string(ack) != "ACK" {
		t.Fatalf("unexpected ACK payload: %q", string(ack))
	}
}

// TestListProtocolReturnsPeers verifies that the listHandler sends registered peers.
func TestListProtocolReturnsPeers(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// Rendezvous host
	rvHost, err := createHost(ctx, 0)
	require.NoError(t, err)
	defer rvHost.Close()
	rvHost.SetStreamHandler(ProtocolIDForRegistration, registrationHandler)
	rvHost.SetStreamHandler(ProtocolIDForPeerList, listHandler)

	// worker1
	w1, err := libp2p.New()
	require.NoError(t, err)
	defer w1.Close()

	// worker2
	w2, err := libp2p.New()
	require.NoError(t, err)
	defer w2.Close()

	rvInfo := peer.AddrInfo{ID: rvHost.ID(), Addrs: rvHost.Addrs()}
	require.NoError(t, w1.Connect(ctx, rvInfo))
	s, err := w1.NewStream(ctx, rvHost.ID(), ProtocolIDForRegistration)
	require.NoError(t, err)
	io.ReadFull(s, make([]byte, 3))
	s.Close()

	require.NoError(t, w2.Connect(ctx, rvInfo))
	s2, err := w2.NewStream(ctx, rvHost.ID(), ProtocolIDForRegistration)
	require.NoError(t, err)
	io.ReadFull(s2, make([]byte, 3))
	s2.Close()

	// w1 requests list
	listStream, err := w1.NewStream(ctx, rvHost.ID(), ProtocolIDForPeerList)
	require.NoError(t, err)
	data, err := io.ReadAll(listStream)
	require.NoError(t, err)
	listStream.Close()

	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	require.GreaterOrEqual(t, len(lines), 1)
	found := false
	for _, l := range lines {
		if strings.Contains(l, w2.ID().String()) {
			found = true
		}
	}
	require.True(t, found, "expected w2 in peer list")
}
