package stun

import (
	"fmt"
	"log"
	"net"
	"time"

	"github.com/pion/stun"
)

type STUNResult struct {
	PublicIP   string
	PublicPort int
}

// performSTUNRequest performs a single STUN request on an existing connection
func performSTUNRequest(pConn net.PacketConn, raddr net.Addr, readTimeoutPerAttempt time.Duration) (*STUNResult, error) {
	message := stun.MustBuild(stun.TransactionID, stun.BindingRequest)

	// Send the request
	_, err := pConn.WriteTo(message.Raw, raddr)
	if err != nil {
		return nil, fmt.Errorf("failed to send STUN request to %s: %w", raddr.String(), err)
	}

	// Read response
	buf := make([]byte, 1500)
	if err := pConn.SetReadDeadline(time.Now().Add(readTimeoutPerAttempt)); err != nil {
		return nil, fmt.Errorf("failed to set read deadline for STUN response: %w", err)
	}

	n, _, err := pConn.ReadFrom(buf)
	if err != nil {
		if netErr, ok := err.(net.Error); ok && netErr.Timeout() {
			return nil, fmt.Errorf("STUN request to %s timed out after %v (local: %s): %w", raddr.String(), readTimeoutPerAttempt, pConn.LocalAddr().String(), err)
		}
		return nil, fmt.Errorf("failed to read STUN response from %s (local: %s): %w", raddr.String(), pConn.LocalAddr().String(), err)
	}

	// Parse response
	response := new(stun.Message)
	response.Raw = buf[:n]
	if err := response.Decode(); err != nil {
		return nil, fmt.Errorf("failed to decode STUN response: %w", err)
	}

	var xorAddr stun.XORMappedAddress
	if err := xorAddr.GetFrom(response); err != nil {
		var mappedAddr stun.MappedAddress
		if errMapped := mappedAddr.GetFrom(response); errMapped != nil {
			return nil, fmt.Errorf("failed to get XOR-mapped or MAPPED address from STUN response: XOR err: %v, MAPPED err: %v", err, errMapped)
		}
		log.Printf("STUN Discovery (via performSTUNRequest): Using MAPPED_ADDRESS: %s:%d", mappedAddr.IP.String(), mappedAddr.Port)
		return &STUNResult{
			PublicIP:   mappedAddr.IP.String(),
			PublicPort: mappedAddr.Port,
		}, nil
	}
	log.Printf("STUN Discovery (via performSTUNRequest): Using XOR_MAPPED_ADDRESS: %s:%d", xorAddr.IP.String(), xorAddr.Port)
	return &STUNResult{
		PublicIP:   xorAddr.IP.String(),
		PublicPort: xorAddr.Port,
	}, nil
}

// DiscoverWithRetry is the improved main function to call from your worker.
// It handles binding the localAddr ONCE and then retrying STUN transactions on that connection.
func DiscoverWithRetry(localAddrToBind string, stunHost string, stunPort int, maxAttempts int) (*STUNResult, error) {
	return DiscoverWithRetryAndTimeout(localAddrToBind, stunHost, stunPort, maxAttempts, 5*time.Second)
}

// DiscoverWithRetryAndTimeout is like DiscoverWithRetry but allows configuring the individual request timeout
func DiscoverWithRetryAndTimeout(localAddrToBind string, stunHost string, stunPort int, maxAttempts int, readTimeoutPerAttempt time.Duration) (*STUNResult, error) {
	if maxAttempts <= 0 {
		maxAttempts = 1
	}

	// Create and bind the UDP PacketConn ONCE here.
	pConn, err := net.ListenPacket("udp4", localAddrToBind)
	if err != nil {
		// This is a critical failure if we can't even bind the initial STUN socket.
		return nil, fmt.Errorf("STUN initial bind failed on %s: %w", localAddrToBind, err)
	}
	defer pConn.Close() // Ensure this is closed when DiscoverWithRetry finishes.

	log.Printf("STUN Discovery: Bound STUN client to local address %s (requested: %s)", pConn.LocalAddr().String(), localAddrToBind)

	// Resolve STUN server address
	serverAddrStr := fmt.Sprintf("%s:%d", stunHost, stunPort)
	raddr, err := net.ResolveUDPAddr("udp", serverAddrStr)
	if err != nil {
		return nil, fmt.Errorf("STUN failed to resolve server address %s: %w", serverAddrStr, err)
	}

	var lastErr error
	for attempt := 0; attempt < maxAttempts; attempt++ {
		if attempt > 0 {
			// Exponential backoff with cap
			delay := time.Duration(1<<uint(attempt-1)) * time.Second // 1s, 2s, 4s ...
			if delay > 8*time.Second {
				delay = 8 * time.Second
			} // Cap delay
			log.Printf("STUN Discovery: Retrying attempt %d/%d in %v...", attempt+1, maxAttempts, delay)
			time.Sleep(delay)
		}

		log.Printf("STUN Discovery: Attempt %d/%d to discover public endpoint using local %s for STUN server %s",
			attempt+1, maxAttempts, pConn.LocalAddr().String(), raddr.String())

		result, err := performSTUNRequest(pConn, raddr, readTimeoutPerAttempt)
		if err == nil {
			return result, nil // Success!
		}
		lastErr = err // Store the last error encountered
		log.Printf("STUN Discovery: Attempt %d/%d failed: %v", attempt+1, maxAttempts, err)
	}

	return nil, fmt.Errorf("STUN discovery failed after %d attempts. Last error: %w", maxAttempts, lastErr)
}
