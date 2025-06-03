package stun

import (
	"errors"
	"fmt"
	"log"
	"net"
	"sync"
	"time"

	"github.com/pion/stun"
)

type STUNResult struct {
	PublicIP   string
	PublicPort int
}

// parseSTUNResponse attempts to parse a STUN binding response.
func parseSTUNResponse(rawMsg []byte) (*STUNResult, error) {
	response := new(stun.Message)
	response.Raw = rawMsg
	if err := response.Decode(); err != nil {
		return nil, fmt.Errorf("failed to decode STUN response: %w", err)
	}

	var xorAddr stun.XORMappedAddress
	if err := xorAddr.GetFrom(response); err != nil {
		var mappedAddr stun.MappedAddress
		if errMapped := mappedAddr.GetFrom(response); errMapped != nil {
			return nil, fmt.Errorf("failed to get XOR-mapped or MAPPED address from STUN response: XOR err: %v, MAPPED err: %v", err, errMapped)
		}
		log.Printf("STUN Discovery (parsed): Using MAPPED_ADDRESS: %s:%d", mappedAddr.IP.String(), mappedAddr.Port)
		return &STUNResult{
			PublicIP:   mappedAddr.IP.String(),
			PublicPort: mappedAddr.Port,
		}, nil
	}
	log.Printf("STUN Discovery (parsed): Using XOR_MAPPED_ADDRESS: %s:%d", xorAddr.IP.String(), xorAddr.Port)
	return &STUNResult{
		PublicIP:   xorAddr.IP.String(),
		PublicPort: xorAddr.Port,
	}, nil
}

// DiscoverWithRetryAndTimeout performs STUN discovery by sending a burst of requests
// and waiting for the first valid response. It retries the entire burst process.
func DiscoverWithRetryAndTimeout(localAddrToBind string, stunHost string, stunPort int, maxAttempts int, readTimeoutPerAttempt time.Duration) (*STUNResult, error) {
	const requestsPerBurst = 10 // Number of STUN requests to send in parallel per attempt

	if maxAttempts <= 0 {
		maxAttempts = 1
	}

	// Resolve STUN server address ONCE
	serverAddrStr := fmt.Sprintf("%s:%d", stunHost, stunPort)
	raddr, err := net.ResolveUDPAddr("udp", serverAddrStr)
	if err != nil {
		return nil, fmt.Errorf("STUN failed to resolve server address %s: %w", serverAddrStr, err)
	}
	// For simplicity, this example uses a single STUN server.
	// To use multiple, resolve them into a slice []*net.UDPAddr
	// and round-robin in the send loop.

	var lastOverallErr error
	for attempt := 0; attempt < maxAttempts; attempt++ {
		if attempt > 0 {
			delay := time.Duration(1<<uint(attempt-1)) * time.Second
			if delay > 8*time.Second {
				delay = 8 * time.Second
			}
			log.Printf("STUN Discovery: Retrying burst attempt %d/%d in %v...", attempt+1, maxAttempts, delay)
			time.Sleep(delay)
		}

		log.Printf("STUN Discovery: Burst attempt %d/%d to %s (local bind: %s)", attempt+1, maxAttempts, raddr.String(), localAddrToBind)

		// Create and bind the UDP PacketConn for EACH burst attempt.
		// This ensures a fresh port if the previous one is in a weird state,
		// though it's less efficient than reusing.
		// For maximum robustness in varied environments, rebinding can sometimes help.
		// The original `DiscoverWithRetry` bound once; this is a slight change.
		pConn, err := net.ListenPacket("udp4", localAddrToBind)
		if err != nil {
			log.Printf("STUN Discovery: Burst attempt %d/%d failed to bind to %s: %v", attempt+1, maxAttempts, localAddrToBind, err)
			lastOverallErr = fmt.Errorf("STUN initial bind failed on %s: %w", localAddrToBind, err)
			continue // Try next attempt with backoff
		}

		// successful will be true if this attempt got a STUN result
		successful := false
		var resultToReturn *STUNResult

		// Scope guard for pConn
		func() {
			defer pConn.Close()

			log.Printf("STUN Discovery: Bound STUN client to local address %s (requested: %s) for burst %d", pConn.LocalAddr().String(), localAddrToBind, attempt+1)

			var wg sync.WaitGroup
			resultChan := make(chan *STUNResult, 1)       // Buffered to allow sender to exit
			errChan := make(chan error, requestsPerBurst) // Collect send errors

			// Send a burst of requests
			for i := 0; i < requestsPerBurst; i++ {
				message := stun.MustBuild(stun.TransactionID, stun.BindingRequest) // New TxnID for each
				_, sendErr := pConn.WriteTo(message.Raw, raddr)
				if sendErr != nil {
					// Non-fatal for a single send, just log and collect
					errChan <- fmt.Errorf("failed to send STUN request %d in burst to %s: %w", i+1, raddr.String(), sendErr)
				}
			}
			close(errChan) // Signal that all send errors are collected

			// Log all send errors, if any
			for sendErr := range errChan {
				log.Printf("STUN Discovery (Send Error): %v", sendErr)
			}

			// Listener goroutine for this burst
			wg.Add(1)
			go func() {
				defer wg.Done()
				buf := make([]byte, 1500)
				for {
					// Set deadline for each ReadFrom attempt
					if err := pConn.SetReadDeadline(time.Now().Add(readTimeoutPerAttempt)); err != nil {
						// If we can't set deadline, it's a critical issue for this listener
						log.Printf("STUN Discovery: Failed to set read deadline for burst attempt %d: %v", attempt+1, err)
						return
					}

					n, _, readErr := pConn.ReadFrom(buf)
					if readErr != nil {
						if netErr, ok := readErr.(net.Error); ok && netErr.Timeout() {
							log.Printf("STUN Discovery: Read timeout for burst attempt %d on %s after %v", attempt+1, pConn.LocalAddr().String(), readTimeoutPerAttempt)
						} else if !errors.Is(readErr, net.ErrClosed) { // Don't log if conn is intentionally closed
							log.Printf("STUN Discovery: Read error for burst attempt %d on %s: %v", attempt+1, pConn.LocalAddr().String(), readErr)
						}
						return // Exit goroutine on read error or timeout
					}

					parsedResult, parseErr := parseSTUNResponse(buf[:n])
					if parseErr == nil && parsedResult != nil {
						select {
						case resultChan <- parsedResult:
							log.Printf("STUN Discovery: Successful response received for burst %d", attempt+1)
							return // Got a result, exit goroutine
						default:
							// Channel already received a result, this one is a duplicate
							log.Printf("STUN Discovery: Duplicate successful response received for burst %d", attempt+1)
						}
					} else if parseErr != nil {
						log.Printf("STUN Discovery: Failed to parse STUN response during burst %d: %v", attempt+1, parseErr)
						// Continue trying to read other potential responses in the burst
					}
				}
			}()

			// Wait for a result or timeout for the listener goroutine
			select {
			case res := <-resultChan:
				// Success for this burst attempt!
				pConn.Close()        // Explicitly close now, ensure listener goroutine exits
				wg.Wait()            // Wait for listener goroutine to finish
				lastOverallErr = nil // Clear last error
				successful = true
				resultToReturn = res
				// Outer function will handle the successful return
			case <-time.After(readTimeoutPerAttempt + 1*time.Second): // Timeout for the entire burst receive phase
				log.Printf("STUN Discovery: Burst attempt %d/%d timed out waiting for a valid response after %v.", attempt+1, maxAttempts, readTimeoutPerAttempt)
				lastOverallErr = fmt.Errorf("burst attempt %d timed out after %v", attempt+1, readTimeoutPerAttempt)
			}

			// If we reached here, this burst attempt failed or select timed out. Close conn and wait for goroutine.
			pConn.Close() // Ensure listener goroutine sees ErrClosed and exits
			wg.Wait()     // Wait for listener goroutine to finish
		}() // End of pConn scope guard

		if successful {
			return resultToReturn, nil
		}
		// Continue to the next attempt if not successful and lastOverallErr is not nil
	}

	if lastOverallErr != nil {
		return nil, fmt.Errorf("STUN discovery failed after %d burst attempts. Last error: %w", maxAttempts, lastOverallErr)
	}
	// Should not be reached if maxAttempts > 0
	return nil, fmt.Errorf("STUN discovery failed after %d burst attempts (unknown error state)", maxAttempts)
}

// DiscoverWithRetry is a convenience wrapper for DiscoverWithRetryAndTimeout
// using a default read timeout of 5 seconds per attempt.
func DiscoverWithRetry(localAddrToBind string, stunHost string, stunPort int, maxAttempts int) (*STUNResult, error) {
	return DiscoverWithRetryAndTimeout(localAddrToBind, stunHost, stunPort, maxAttempts, 5*time.Second)
}
