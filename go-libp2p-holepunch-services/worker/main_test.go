package main

import (
	"context"
	"fmt"
	"io"
	"testing"
	"time"

	"github.com/libp2p/go-libp2p/core/network"
	"github.com/libp2p/go-libp2p/core/peer"
	"github.com/stretchr/testify/require"
)

// TestWorkerServiceRuns remains as placeholder if desired (removed duplicate tests)

// simpleRegistrationHandlerForTest is used in tests to simulate rendezvous ACK.
func simpleRegistrationHandlerForTest(s network.Stream) {
	// Write ACK immediately so the client isn't blocked waiting.
	_, _ = s.Write([]byte("ACK"))
	// Drain any incoming data (optional) then close.
	io.Copy(io.Discard, s)
	_ = s.Close()
}

// TestWorkerConnectsAndRegisters ensures worker connects to rendezvous and gets ACK.
func TestWorkerConnectsAndRegisters(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Create rendezvous host using createWorkerHost for simplicity
	rendezvousHost, err := createWorkerHost(ctx, 0)
	require.NoError(t, err)
	defer rendezvousHost.Close()

	// set handler
	rendezvousHost.SetStreamHandler(ProtocolIDForRegistration, simpleRegistrationHandlerForTest)

	// Build multiaddr for rendezvous host (use first address)
	require.NotEmpty(t, rendezvousHost.Addrs())
	rendezvousMaddrStr := fmt.Sprintf("%s/p2p/%s", rendezvousHost.Addrs()[0], rendezvousHost.ID())

	// Create worker host
	workerHost, err := createWorkerHost(ctx, 0)
	require.NoError(t, err)
	defer workerHost.Close()

	// Call connectAndRegisterWithRendezvous
	err = connectAndRegisterWithRendezvous(ctx, workerHost, rendezvousMaddrStr)
	require.NoError(t, err)
}

// TestDiscoverPeersAndDial uses the list protocol to connect two workers via the rendezvous host.
func TestDiscoverPeersAndDial(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	rvHost, err := createWorkerHost(ctx, 0)
	require.NoError(t, err)
	defer rvHost.Close()
	rvHost.SetStreamHandler(ProtocolIDForRegistration, simpleRegistrationHandlerForTest)
	rvHost.SetStreamHandler(ProtocolIDForPeerList, listHandler)

	w1, err := createWorkerHost(ctx, 0)
	require.NoError(t, err)
	defer w1.Close()

	w2, err := createWorkerHost(ctx, 0)
	require.NoError(t, err)
	defer w2.Close()

	addrStr := fmt.Sprintf("%s/p2p/%s", rvHost.Addrs()[0], rvHost.ID())
	require.NoError(t, connectAndRegisterWithRendezvous(ctx, w1, addrStr))
	require.NoError(t, connectAndRegisterWithRendezvous(ctx, w2, addrStr))

	peers, err := discoverPeersViaListProtocol(ctx, w1, addrStr)
	require.NoError(t, err)
	require.GreaterOrEqual(t, len(peers), 1)

	// w1 should dial w2
	var info peer.AddrInfo
	for _, p := range peers {
		if p.ID == w2.ID() {
			info = p
			break
		}
	}
	require.NotEqual(t, peer.ID(""), info.ID)
	require.NoError(t, w1.Connect(ctx, info))
}
