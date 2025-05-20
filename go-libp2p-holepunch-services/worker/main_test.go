package main

import (
	"context"
	"fmt"
	"io"
	"testing"

	"github.com/libp2p/go-libp2p/core/network"
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