package main

import (
	"context"
	"fmt"
	"io"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/libp2p/go-libp2p"
	"github.com/libp2p/go-libp2p/core/network"
	"github.com/libp2p/go-libp2p/core/peer"
	ws "github.com/libp2p/go-libp2p/p2p/transport/websocket"
	ma "github.com/multiformats/go-multiaddr"
	"github.com/stretchr/testify/require"
)

// Store for registered peers in tests.
var (
	testRegisteredPeers      = make(map[peer.ID]ma.Multiaddr)
	testRegisteredPeersMutex = &sync.Mutex{}
)

// TestWorkerServiceRuns remains as placeholder if desired (removed duplicate tests)

// simpleRegistrationHandlerForTest is used in tests to simulate rendezvous ACK.
// It also now registers the peer to testRegisteredPeers for the listHandler to use.
func simpleRegistrationHandlerForTest(s network.Stream) {
	remotePeerID := s.Conn().RemotePeer()
	remoteAddr := s.Conn().RemoteMultiaddr()

	testRegisteredPeersMutex.Lock()
	testRegisteredPeers[remotePeerID] = remoteAddr
	testRegisteredPeersMutex.Unlock()

	// Write ACK immediately so the client isn't blocked waiting.
	_, _ = s.Write([]byte("ACK"))
	// Drain any incoming data (optional) then close.
	io.Copy(io.Discard, s)
	_ = s.Close()
}

// listHandler for tests, mimics the rendezvous listHandler.
func listHandler(s network.Stream) {
	defer s.Close()
	requester := s.Conn().RemotePeer()
	testRegisteredPeersMutex.Lock()
	defer testRegisteredPeersMutex.Unlock()

	var peerAddrs []string
	for pid, addr := range testRegisteredPeers {
		if pid == requester {
			continue
		}
		fullAddrStr := fmt.Sprintf("%s/p2p/%s", addr.String(), pid.String())
		peerAddrs = append(peerAddrs, fullAddrStr)
	}
	_, _ = s.Write([]byte(strings.Join(peerAddrs, "\n") + "\n"))
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
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()

	// Clear the shared map for this test run
	testRegisteredPeersMutex.Lock()
	clear(testRegisteredPeers)
	testRegisteredPeersMutex.Unlock()

	rvHost, err := createWorkerHost(ctx, 0)
	require.NoError(t, err)
	defer rvHost.Close()
	rvHost.SetStreamHandler(ProtocolIDForRegistration, simpleRegistrationHandlerForTest)
	rvHost.SetStreamHandler(ProtocolIDForPeerList, listHandler)

	rvHostAddrInfo := peer.AddrInfo{
		ID:    rvHost.ID(),
		Addrs: rvHost.Addrs(),
	}

	// Create w1
	w1, err := libp2p.New(
		libp2p.ListenAddrStrings("/ip4/0.0.0.0/tcp/0", "/ip4/0.0.0.0/tcp/0/ws"),
		libp2p.Transport(ws.New),
		libp2p.EnableHolePunching(),
		libp2p.EnableAutoRelayWithStaticRelays([]peer.AddrInfo{rvHostAddrInfo}),
		libp2p.NATPortMap(),
	)
	require.NoError(t, err)
	defer w1.Close()

	// Create w2
	w2, err := libp2p.New(
		libp2p.ListenAddrStrings("/ip4/0.0.0.0/tcp/0", "/ip4/0.0.0.0/tcp/0/ws"),
		libp2p.Transport(ws.New),
		libp2p.EnableHolePunching(),
		libp2p.EnableAutoRelayWithStaticRelays([]peer.AddrInfo{rvHostAddrInfo}),
		libp2p.NATPortMap(),
	)
	require.NoError(t, err)
	defer w2.Close()

	addrStr := fmt.Sprintf("%s/p2p/%s", rvHost.Addrs()[0], rvHost.ID())

	// w1 registers. simpleRegistrationHandlerForTest will store its s.Conn().RemoteMultiaddr().
	require.NoError(t, connectAndRegisterWithRendezvous(ctx, w1, addrStr))
	// w2 registers. simpleRegistrationHandlerForTest will store its s.Conn().RemoteMultiaddr().
	require.NoError(t, connectAndRegisterWithRendezvous(ctx, w2, addrStr))

	// Now, OVERRIDE w2's entry in testRegisteredPeers with its actual listen address
	// so that listHandler provides this correct address to w1.
	var w2ListenAddrToUse ma.Multiaddr
	for _, addr := range w2.Addrs() {
		if strings.Contains(addr.String(), "/ip4/") && !strings.Contains(addr.String(), "127.0.0.1") {
			w2ListenAddrToUse = addr
			break
		}
	}
	if w2ListenAddrToUse == nil {
		for _, addr := range w2.Addrs() {
			if strings.Contains(addr.String(), "/ip4/") {
				w2ListenAddrToUse = addr
				break
			}
		}
	}
	require.NotNil(t, w2ListenAddrToUse, "w2 has no suitable IPv4 listen address")

	testRegisteredPeersMutex.Lock()
	origW2Addr := testRegisteredPeers[w2.ID()] // The one from simpleRegistrationHandlerForTest
	testRegisteredPeers[w2.ID()] = w2ListenAddrToUse
	testRegisteredPeersMutex.Unlock()
	t.Logf("Overrode w2 address for discovery. Was: %s, Now: %s", origW2Addr, w2ListenAddrToUse)

	peers, err := discoverPeersViaListProtocol(ctx, w1, addrStr)
	require.NoError(t, err)
	require.GreaterOrEqual(t, len(peers), 1, "Should discover at least w2")

	var w2Info peer.AddrInfo
	foundW2 := false
	for _, p := range peers {
		if p.ID == w2.ID() {
			w2Info = p
			foundW2 = true
			break
		}
	}
	require.True(t, foundW2, "w2 not found in discovered peers")
	require.NotEqual(t, peer.ID(""), w2Info.ID, "w2Info should be populated")
	// Check that the address for w2 in w2Info is indeed the listen address we overrode with.
	require.Len(t, w2Info.Addrs, 1, "w2Info should have one address from our map")
	require.Equal(t, w2ListenAddrToUse.String(), w2Info.Addrs[0].String(), "w2Info address mismatch")

	t.Logf("w1 (%s) attempting to connect to w2 (%s) with AddrInfo: %v", w1.ID(), w2.ID(), w2Info)
	err = w1.Connect(ctx, w2Info)
	if err != nil {
		t.Logf("w1 addrs: %v", w1.Addrs())
		t.Logf("w2 addrs: %v", w2.Addrs())
		t.Logf("w2Info used for connect: %v", w2Info.Addrs)
	}
	require.NoError(t, err)
}
