package stun

import (
	"fmt"
	"net"
	"time"

	"github.com/pion/stun"
)

type STUNResult struct {
	PublicIP   string
	PublicPort int
}

// DiscoverWithConn uses an existing UDP connection for STUN discovery
// NOTE: This function is not fully implemented for reliable use with shared connections
// due to challenges with managing read deadlines on a connection that might be concurrently used.
func DiscoverWithConn(conn *net.UDPConn, stunHost string, stunPort int) (*STUNResult, error) {
	serverAddrStr := fmt.Sprintf("%s:%d", stunHost, stunPort)

	raddr, err := net.ResolveUDPAddr("udp", serverAddrStr)
	if err != nil {
		return nil, fmt.Errorf("failed to resolve STUN server address %s: %w", serverAddrStr, err)
	}

	message := stun.MustBuild(stun.TransactionID, stun.BindingRequest)

	_, err = conn.WriteToUDP(message.Raw, raddr)
	if err != nil {
		return nil, fmt.Errorf("failed to send STUN request via existing conn: %w", err)
	}

	// Reading response here is problematic on a shared conn without a dedicated response queue
	// or way to distinguish STUN responses from other P2P data.
	// For now, this function remains more of a placeholder for a more complex implementation.
	return nil, fmt.Errorf("STUN discovery with shared connection not yet reliably implemented for response reading")
}

// DiscoverPublicEndpointWithTimeout performs STUN discovery with a configurable read timeout
func DiscoverPublicEndpointWithTimeout(localAddr string, stunHost string, stunPort int, readTimeout time.Duration) (*STUNResult, error) {
	serverAddrStr := fmt.Sprintf("%s:%d", stunHost, stunPort)

	// Resolve STUN server address
	raddr, err := net.ResolveUDPAddr("udp", serverAddrStr)
	if err != nil {
		return nil, fmt.Errorf("failed to resolve STUN server address %s: %w", serverAddrStr, err)
	}

	// Log resolved address for debugging
	// log.Printf("STUN: Resolved server address %s to %s", serverAddrStr, raddr.String())

	// Create a UDP packet listener.
	// If localAddr is like ":8081", it will try to bind to that local port.
	// If localAddr is like ":0", it will bind to an ephemeral port.
	pConn, err := net.ListenPacket("udp4", localAddr)
	if err != nil {
		return nil, fmt.Errorf("failed to listen on UDP port %s: %w", localAddr, err)
	}
	defer pConn.Close()

	// Log local address for debugging
	localSocketAddr := pConn.LocalAddr().String()
	// log.Printf("STUN: Created UDP listener on %s", localSocketAddr)

	// Build STUN binding request
	message := stun.MustBuild(stun.TransactionID, stun.BindingRequest)

	// Send the request
	_, err = pConn.WriteTo(message.Raw, raddr)
	if err != nil {
		return nil, fmt.Errorf("failed to send STUN request to %s: %w", raddr.String(), err)
	}

	// Read response
	buf := make([]byte, 1500)
	// Set a deadline for the read. net.PacketConn has SetReadDeadline.
	if err := pConn.SetReadDeadline(time.Now().Add(readTimeout)); err != nil {
		return nil, fmt.Errorf("failed to set read deadline for STUN response: %w", err)
	}

	n, _, err := pConn.ReadFrom(buf)
	if err != nil {
		// Check if it's a timeout error
		if netErr, ok := err.(net.Error); ok && netErr.Timeout() {
			return nil, fmt.Errorf("STUN request to %s timed out after %v (sent from %s): %w", raddr.String(), readTimeout, localSocketAddr, err)
		}
		return nil, fmt.Errorf("failed to read STUN response from %s (local %s): %w", raddr.String(), localSocketAddr, err)
	}

	// Log response details for debugging
	// log.Printf("STUN: Received %d bytes response", n)

	// Parse response
	response := new(stun.Message)
	response.Raw = buf[:n]
	if err := response.Decode(); err != nil {
		return nil, fmt.Errorf("failed to decode STUN response: %w", err)
	}

	// Extract XOR-mapped address
	var xorAddr stun.XORMappedAddress
	if err := xorAddr.GetFrom(response); err != nil {
		// Fallback: Try MAPPED_ADDRESS if XOR_MAPPED_ADDRESS fails (some older STUN servers or certain NATs)
		var mappedAddr stun.MappedAddress
		if errMapped := mappedAddr.GetFrom(response); errMapped != nil {
			return nil, fmt.Errorf("failed to get XOR-mapped or MAPPED address from STUN response: XOR err: %v, MAPPED err: %v", err, errMapped)
		}
		// log.Printf("STUN: Using MAPPED_ADDRESS: %s:%d", mappedAddr.IP.String(), mappedAddr.Port)
		return &STUNResult{
			PublicIP:   mappedAddr.IP.String(),
			PublicPort: mappedAddr.Port,
		}, nil
	}

	// log.Printf("STUN: Using XOR_MAPPED_ADDRESS: %s:%d", xorAddr.IP.String(), xorAddr.Port)
	return &STUNResult{
		PublicIP:   xorAddr.IP.String(),
		PublicPort: xorAddr.Port,
	}, nil
}

// DiscoverPublicEndpoint performs STUN discovery with default 10 second read timeout
func DiscoverPublicEndpoint(localAddr string, stunHost string, stunPort int) (*STUNResult, error) {
	return DiscoverPublicEndpointWithTimeout(localAddr, stunHost, stunPort, 10*time.Second)
}

// DiscoverWithRetryTimeout performs STUN discovery with retries and configurable read timeout
func DiscoverWithRetryTimeout(localAddr string, stunHost string, stunPort int, maxRetries int, readTimeout time.Duration) (*STUNResult, error) {
	var lastErr error

	for i := 0; i < maxRetries; i++ {
		if i > 0 {
			// Exponential backoff for retries, e.g., 1s, 2s, 4s
			delay := time.Duration(1<<uint(i-1)) * time.Second
			// log.Printf("STUN: Retry attempt %d/%d in %v...", i+1, maxRetries, delay)
			time.Sleep(delay)
		}

		// log.Printf("STUN: Attempt %d/%d to discover public endpoint", i+1, maxRetries)
		result, err := DiscoverPublicEndpointWithTimeout(localAddr, stunHost, stunPort, readTimeout)
		if err == nil {
			return result, nil
		}
		lastErr = err
		// log.Printf("STUN: Attempt %d/%d failed: %v", i+1, maxRetries, err)
	}

	return nil, fmt.Errorf("failed after %d retries: %w", maxRetries, lastErr)
}

// DiscoverWithRetry performs STUN discovery with retries using default 10 second read timeout
func DiscoverWithRetry(localAddr string, stunHost string, stunPort int, maxRetries int) (*STUNResult, error) {
	return DiscoverWithRetryTimeout(localAddr, stunHost, stunPort, maxRetries, 10*time.Second)
}
