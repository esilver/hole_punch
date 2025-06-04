package main

import (
	"encoding/base64"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/google/uuid"
	"github.com/gorilla/websocket"
	"github.com/labstack/echo/v4"
	"github.com/labstack/echo/v4/middleware"

	"github.com/elisilver/holepunch/pkg/models"
	"github.com/elisilver/holepunch/pkg/p2p"
	"github.com/elisilver/holepunch/pkg/stun"
	ws "github.com/elisilver/holepunch/pkg/websocket"
)

var (
	state    *models.WorkerState
	wsClient *ws.Client
	p2pProto *p2p.P2PProtocol
	udpConn  *net.UDPConn
	upgrader = websocket.Upgrader{
		CheckOrigin: func(r *http.Request) bool { return true },
	}
)

func setupUDPListener(port int) (*net.UDPConn, error) {
	// Use the specified port (matching Python's behavior)
	listenAddr := fmt.Sprintf("0.0.0.0:%d", port)

	udpAddr, err := net.ResolveUDPAddr("udp4", listenAddr)
	if err != nil {
		return nil, fmt.Errorf("resolve UDP addr: %w", err)
	}
	conn, err := net.ListenUDP("udp4", udpAddr)
	if err != nil {
		return nil, fmt.Errorf("listen UDP: %w", err)
	}

	// Set buffer sizes for better performance
	if err := conn.SetReadBuffer(1024 * 1024); err != nil { // 1MB
		log.Printf("Warning: failed to set read buffer: %v", err)
	}
	if err := conn.SetWriteBuffer(1024 * 1024); err != nil { // 1MB
		log.Printf("Warning: failed to set write buffer: %v", err)
	}

	actualAddr := conn.LocalAddr().(*net.UDPAddr)
	log.Printf("UDP listener started on %s (actual port %d, requested port %d)", actualAddr.String(), actualAddr.Port, port)
	return conn, nil
}

func main() {
	// Startup banner with revision info
	log.Println("=====================================")
	log.Println("Holepunch Go Worker v1.0")
	log.Println("Revision: Fixed port binding race condition")
	log.Println("Build Date:", time.Now().Format("2006-01-02 15:04:05"))
	log.Println("=====================================")

	workerID := uuid.New().String()
	state = models.NewWorkerState(workerID)

	log.Printf("Worker starting with ID: %s", workerID)

	rendezvousURL := os.Getenv("RENDEZVOUS_SERVICE_URL")
	if rendezvousURL == "" {
		log.Fatal("RENDEZVOUS_SERVICE_URL environment variable is required")
	}

	port := getEnvInt("PORT", 8080)
	udpPort := getEnvInt("INTERNAL_UDP_PORT", 8081)

	// Setup HTTP server first to ensure Cloud Run health checks pass
	e := echo.New()
	e.Use(middleware.Logger())
	e.Use(middleware.Recover())
	e.Use(middleware.CORS())

	e.Static("/", "web/worker")
	e.GET("/ui_ws", handleUIWebSocket)
	e.GET("/health", func(c echo.Context) error {
		return c.String(http.StatusOK, "OK")
	})

	// Start HTTP server in goroutine
	go func() {
		log.Printf("Worker UI starting on port %d", port)
		if err := e.Start(fmt.Sprintf(":%d", port)); err != nil && err != http.ErrServerClosed {
			log.Fatalf("Failed to start HTTP server: %v", err)
		}
	}()

	// HTTP server starts concurrently, no need to wait

	// Now setup UDP and STUN discovery after HTTP server is running
	go func() {
		log.Printf("Starting STUN discovery phase first...")

		stunHost := getEnvStr("STUN_HOST", "stun.l.google.com")
		stunPort := getEnvInt("STUN_PORT", 19302)
		// STUN discovery will attempt to bind locally to udpPort (INTERNAL_UDP_PORT)
		localStunBindAddr := fmt.Sprintf(":%d", udpPort)

		log.Printf("Attempting STUN discovery: STUN client will bind locally to %s, targeting STUN server %s:%d", localStunBindAddr, stunHost, stunPort)

		// Call STUN discovery synchronously with longer individual timeout (10s instead of 5s)
		stunResult, stunErr := stun.DiscoverWithRetryAndTimeout(localStunBindAddr, stunHost, stunPort, 5, 10*time.Second)

		if stunErr != nil {
			log.Printf("STUN discovery failed: %v", stunErr)
			log.Printf("Continuing without STUN endpoint information.")
			// state.OurStunDiscoveredUDPIP and OurStunDiscoveredUDPPort will remain empty/zero
		} else {
			log.Printf("STUN discovery successful. Public UDP endpoint: %s:%d", stunResult.PublicIP, stunResult.PublicPort)
			state.Mu.Lock()
			state.OurStunDiscoveredUDPIP = stunResult.PublicIP
			state.OurStunDiscoveredUDPPort = stunResult.PublicPort
			state.Mu.Unlock()
		}

		// STUN discovery phase is now complete. The temporary listener used by it
		// should have been closed by its 'defer pConn.Close()'.

		log.Printf("STUN phase finished. Attempting to set up main P2P UDP listener on port %d...", udpPort)

		const (
			initialListenerDelay   = 1 * time.Second // Initial delay before first bind attempt for P2P listener (increased from 250ms)
			maxListenerRetries     = 5               // Number of retries for P2P listener bind
			listenerRetryDelayStep = 1 * time.Second // Base delay step for subsequent retries (increased from 300ms)
		)

		time.Sleep(initialListenerDelay) // Wait a bit for the OS to fully release the port used by STUN

		var errSetupListener error
		for i := 0; i < maxListenerRetries; i++ {
			if i > 0 { // If this is a retry
				// Increasing delay for subsequent retries
				currentRetryDelay := listenerRetryDelayStep * time.Duration(i)
				log.Printf("Retrying P2P UDP listener setup for port %d in %v (attempt %d/%d)...", udpPort, currentRetryDelay, i+1, maxListenerRetries)
				time.Sleep(currentRetryDelay)
			}

			udpConn, errSetupListener = setupUDPListener(udpPort) // Attempt to bind to the fixed udpPort
			if errSetupListener == nil {
				// Success!
				// The actual port might be different if setupUDPListener was changed to ephemeral,
				// but with the change above, it should be the requested udpPort.
				// Verify if udpConn.LocalAddr() indeed matches the requested udpPort.
				mainListenerAddr := udpConn.LocalAddr().(*net.UDPAddr)
				log.Printf("Main P2P UDP listener established on %s (requested port %d) after %d attempt(s)", mainListenerAddr.String(), udpPort, i+1)

				p2pProto = p2p.NewP2PProtocol(udpConn, state)
				go p2pProto.Start() // Start the P2P read loop
				break               // Exit retry loop
			}

			// Enhanced error logging and detection
			log.Printf("P2P Listener Setup attempt %d/%d raw error: [%T] %v", i+1, maxListenerRetries, errSetupListener, errSetupListener)

			isAddrInUseError := false

			// Check if error is "address already in use" with multiple fallback approaches
			if opErr, ok := errSetupListener.(*net.OpError); ok {
				log.Printf("P2P Listener Setup attempt %d/%d OpError: [%T] %v", i+1, maxListenerRetries, opErr.Err, opErr.Err)
				if sysErr, ok := opErr.Err.(*os.SyscallError); ok {
					log.Printf("P2P Listener Setup attempt %d/%d SyscallError: %v", i+1, maxListenerRetries, sysErr.Err)
					// Check if the error contains "address already in use"
					if strings.Contains(strings.ToLower(sysErr.Error()), "address already in use") {
						isAddrInUseError = true
					}
				} else if strings.Contains(strings.ToLower(opErr.Err.Error()), "address already in use") {
					// Fallback if not a SyscallError but net.OpError message contains it
					isAddrInUseError = true
				}
			} else if strings.Contains(strings.ToLower(errSetupListener.Error()), "address already in use") {
				// Fallback for other error types that might wrap it
				isAddrInUseError = true
			}

			if isAddrInUseError {
				log.Printf("Attempt %d/%d to bind P2P UDP listener on port %d identified as 'address already in use': %v. Will retry if attempts remain.", i+1, maxListenerRetries, udpPort, errSetupListener)
				if i == maxListenerRetries-1 {
					log.Printf("CRITICAL: All %d retries to bind P2P UDP listener on port %d failed with 'address already in use'. Last error: %v", maxListenerRetries, udpPort, errSetupListener)
				} else {
					continue // Go to the next retry iteration
				}
			}

			// If error is not "address already in use" or it was the last retry for it
			log.Printf("CRITICAL: Failed to setup main P2P UDP listener on port %d with a non-recoverable or final error after %d attempt(s): %v", udpPort, i+1, errSetupListener)
			break // Exit loop on non-address-in-use error OR if it was the last retry for it
		}

		if udpConn == nil { // Check if listener setup ultimately failed
			log.Printf("CRITICAL: Main P2P UDP listener could not be established on port %d.", udpPort)
			// Ensure p2pProto is not used if udpConn is nil
			p2pProto = nil
		}

		// Connect to rendezvous service after STUN discovery and UDP setup attempt
		connectToRendezvous(workerID, rendezvousURL)

		// Start P2P keep-alive goroutine
		go startP2PKeepAlive()
	}()

	// Handle shutdown
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, syscall.SIGINT, syscall.SIGTERM)
	<-sigChan
	log.Println("Shutting down...")
	if wsClient != nil {
		wsClient.Close()
	}
	if udpConn != nil {
		udpConn.Close()
	}
	e.Close()
}

func connectToRendezvous(workerID, rendezvousURL string) {
	for {
		log.Printf("Worker %s: Attempting to connect to rendezvous service...", workerID)
		wsClient = ws.NewClient(rendezvousURL + "/ws/register/" + workerID)
		setupWebSocketHandlers()

		if err := wsClient.Connect(); err != nil {
			log.Printf("Worker %s: Failed to connect to rendezvous service: %v", workerID, err)
			log.Printf("Worker %s: Will retry in 10 seconds...", workerID)
			time.Sleep(10 * time.Second)
			continue
		}

		log.Printf("Worker %s: Connected to rendezvous service successfully", workerID)

		// Send initial STUN endpoint if available
		state.Mu.RLock()
		stunIP := state.OurStunDiscoveredUDPIP
		stunPort := state.OurStunDiscoveredUDPPort
		state.Mu.RUnlock()

		if stunIP != "" && stunPort > 0 {
			log.Printf("Worker %s: Sending initial STUN endpoint %s:%d to rendezvous", workerID, stunIP, stunPort)
			wsClient.SendMessage("update_udp_endpoint", map[string]interface{}{
				"udp_ip":   stunIP,
				"udp_port": stunPort,
			})
		}

		// Connection established, exit retry loop
		break
	}
}

func setupWebSocketHandlers() {
	wsClient.RegisterHandler("p2p_connection_offer", handleP2PConnectionOffer)
	wsClient.RegisterHandler("udp_endpoint_ack", handleUDPEndpointAck)
	wsClient.RegisterHandler("admin_chat_message", handleAdminChatMessage)
	wsClient.RegisterHandler("echo_response", handleEchoResponse)
}

func handleP2PConnectionOffer(payload map[string]interface{}) error {
	peerID := payload["peer_worker_id"].(string)
	peerIP := payload["peer_udp_ip"].(string)
	peerPort := int(payload["peer_udp_port"].(float64))

	log.Printf("Received P2P connection offer from %s at %s:%d", peerID, peerIP, peerPort)

	peerAddr, err := net.ResolveUDPAddr("udp4", fmt.Sprintf("%s:%d", peerIP, peerPort))
	if err != nil {
		return fmt.Errorf("failed to resolve peer address: %w", err)
	}
	log.Printf("Worker '%s': Resolved peer address for P2P connection to: %s", state.WorkerID, peerAddr.String())

	state.Mu.Lock()
	state.CurrentP2PPeerID = peerID
	state.CurrentP2PPeerAddr = peerAddr
	state.Mu.Unlock()

	// Send hole-punching packets
	go func() {
		// Check if UDP is ready
		if udpConn == nil {
			log.Printf("Worker '%s': Received P2P connection offer but UDP not yet initialized", state.WorkerID)
			return
		}

		log.Printf("Worker '%s': Starting hole-punch sequence to %s (3 pings @ 500ms interval)", state.WorkerID, peerAddr.String())
		numPings := 3
		pingInterval := 500 * time.Millisecond

		for i := 0; i < numPings; i++ {
			message_content := fmt.Sprintf("P2P_HOLE_PUNCH_PING_FROM_%s_NUM_%d", state.WorkerID, i+1)
			n, err := udpConn.WriteToUDP([]byte(message_content), peerAddr)
			if err != nil {
				log.Printf("Worker '%s': Error sending UDP Hole Punch PING %d: %v", state.WorkerID, i+1, err)
			} else {
				log.Printf("Worker '%s': Sent UDP Hole Punch PING %d to %s (%d bytes)", state.WorkerID, i+1, peerAddr.String(), n)
			}
			if i < numPings-1 { // Don't sleep after the last ping
				time.Sleep(pingInterval)
			}
		}

		// Initiate pairing test if we have lower ID
		if p2pProto != nil && state.WorkerID < peerID {
			log.Printf("Worker '%s': Initiating pairing test with %s (we have lower ID)", state.WorkerID, peerID)
			p2pProto.InitiatePairingTest(peerID, peerAddr)
		}
	}()

	// Notify UI
	state.Mu.RLock()
	clients := state.UIWebsocketClients
	state.Mu.RUnlock()

	uiMsg := map[string]interface{}{
		"type":    "p2p_status_update",
		"message": "P2P connection established",
		"peer_id": peerID,
	}

	for _, client := range clients {
		client.WriteJSON(uiMsg)
	}

	return nil
}

func handleUDPEndpointAck(payload map[string]interface{}) error {
	log.Println("UDP endpoint acknowledged by rendezvous service")
	return nil
}

func handleAdminChatMessage(payload map[string]interface{}) error {
	content := payload["content"].(string)
	adminSessionID := payload["admin_session_id"].(string)

	log.Printf("Admin chat from %s: %s", adminSessionID, content)

	// Forward to UI
	state.Mu.RLock()
	clients := state.UIWebsocketClients
	state.Mu.RUnlock()

	uiMsg := map[string]interface{}{
		"type": "admin_chat_received",
		"payload": map[string]interface{}{
			"admin_session_id": adminSessionID,
			"content":          content,
		},
	}

	for _, client := range clients {
		client.WriteJSON(uiMsg)
	}

	// Send auto-reply back to rendezvous
	go func() {
		responsePayload := map[string]interface{}{
			"admin_session_id": adminSessionID,
			"content":          fmt.Sprintf("Worker %s received: %s", state.WorkerID[:8], content),
		}
		if wsClient != nil {
			err := wsClient.SendMessage("chat_response", responsePayload)
			if err != nil {
				log.Printf("Failed to send chat_response to rendezvous: %v", err)
			}
		} else {
			log.Printf("Cannot send chat_response - not connected to rendezvous")
		}
	}()

	return nil
}

func handleEchoResponse(payload map[string]interface{}) error {
	log.Printf("Echo response: %v", payload)
	return nil
}

func handleUIWebSocket(c echo.Context) error {
	ws, err := upgrader.Upgrade(c.Response(), c.Request(), nil)
	if err != nil {
		return err
	}
	defer ws.Close()

	state.Mu.Lock()
	state.UIWebsocketClients = append(state.UIWebsocketClients, ws)
	state.Mu.Unlock()

	// Send initial state
	ws.WriteJSON(map[string]interface{}{
		"type": "init_info",
		"payload": map[string]interface{}{
			"worker_id":   state.WorkerID,
			"p2p_peer_id": state.CurrentP2PPeerID,
		},
	})

	// Read messages from UI
	for {
		var msg map[string]interface{}
		if err := ws.ReadJSON(&msg); err != nil {
			break
		}

		switch msg["type"] {
		case "send_p2p_message":
			payload, ok := msg["payload"].(map[string]interface{})
			if !ok {
				log.Println("Invalid send_p2p_message payload")
				break
			}
			content, ok := payload["content"].(string)
			if !ok {
				log.Println("Invalid content in send_p2p_message")
				break
			}

			state.Mu.RLock()
			peerAddr := state.CurrentP2PPeerAddr
			state.Mu.RUnlock()

			if peerAddr == nil {
				log.Println("UI tried to send P2P message, but no peer connected")
				ws.WriteJSON(map[string]interface{}{
					"type":    "error",
					"message": "No P2P connection",
				})
				break
			}
			if p2pProto == nil {
				log.Println("UI tried to send P2P message, but P2P protocol not initialized")
				ws.WriteJSON(map[string]interface{}{
					"type":    "error",
					"message": "P2P protocol not ready",
				})
				break
			}
			err := p2pProto.SendChatMessage(content, peerAddr)
			if err != nil {
				log.Printf("Error sending P2P message via UI command: %v", err)
				ws.WriteJSON(map[string]interface{}{
					"type":    "error",
					"message": fmt.Sprintf("Failed to send: %v", err),
				})
			}
		case "start_benchmark_send":
			payload, ok := msg["payload"].(map[string]interface{})
			if !ok {
				log.Println("Invalid start_benchmark_send payload")
				break
			}

			sizeKbFloat, ok := payload["size_kb"].(float64)
			if !ok {
				log.Println("Invalid size_kb in start_benchmark_send")
				break
			}
			sizeKb := int(sizeKbFloat)

			// 512-byte payloads keep us under MTU but we still want to send the exact
			// size requested by the UI (size_kb × 1024 bytes).
			const reqChunkSize = 512
			reqNumChunks := (sizeKb * 1024) / reqChunkSize

			state.Mu.RLock()
			peerAddrBenchmark := state.CurrentP2PPeerAddr
			state.Mu.RUnlock()

			if peerAddrBenchmark == nil {
				log.Println("UI tried to start benchmark, but no peer connected")
				ws.WriteJSON(map[string]interface{}{
					"type":    "error",
					"message": "No P2P connection",
				})
				break
			}

			if p2pProto == nil {
				log.Println("UI tried to start benchmark, but P2P protocol not initialized")
				ws.WriteJSON(map[string]interface{}{
					"type":    "error",
					"message": "P2P protocol not ready",
				})
				break
			}

			sessionID := uuid.New().String()
			go func() {
				log.Printf("Starting benchmark send: session=%s, chunks=%d, chunk_size=%d, to=%s",
					sessionID, reqNumChunks, reqChunkSize, peerAddrBenchmark)

				data := make([]byte, reqChunkSize)
				startTime := time.Now()
				for i := 0; i < reqNumChunks; i++ {
					payloadBase64 := base64.StdEncoding.EncodeToString(data)
					p2pProto.SendMessage("benchmark_chunk", map[string]interface{}{
						"session_id":     sessionID,
						"seq":            i,
						"payload":        payloadBase64,
						"from_worker_id": state.WorkerID,
					}, peerAddrBenchmark)
				}

				duration := time.Since(startTime)
				throughputMbps := float64(reqNumChunks*reqChunkSize*8) / duration.Seconds() / 1e6
				log.Printf("Benchmark send completed: session=%s, duration=%v, throughput=%.2f Mbps",
					sessionID, duration, throughputMbps)

				p2pProto.SendMessage("benchmark_end", map[string]interface{}{
					"session_id":     sessionID,
					"total_chunks":   reqNumChunks,
					"from_worker_id": state.WorkerID,
				}, peerAddrBenchmark)
			}()
			ws.WriteJSON(map[string]interface{}{
				"type":    "benchmark_status",
				"message": "Benchmark send initiated by UI command",
			})
		default:
			log.Printf("Unknown UI WebSocket message type: %s", msg["type"])
		}
	}

	// Remove from clients
	state.Mu.Lock()
	for i, client := range state.UIWebsocketClients {
		if client == ws {
			state.UIWebsocketClients = append(
				state.UIWebsocketClients[:i],
				state.UIWebsocketClients[i+1:]...,
			)
			break
		}
	}
	state.Mu.Unlock()

	return nil
}

func getEnvInt(key string, defaultValue int) int {
	if v := os.Getenv(key); v != "" {
		if i, err := strconv.Atoi(v); err == nil {
			return i
		}
	}
	return defaultValue
}

func getEnvStr(key, defaultValue string) string {
	if v := os.Getenv(key); v != "" {
		return strings.TrimSpace(v)
	}
	return defaultValue
}

func startP2PKeepAlive() {
	log.Printf("Starting P2P keep-alive monitor")
	// Monitor for changes in P2P peer and start/stop keep-alive accordingly
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()

	var lastPeerAddr *net.UDPAddr

	for range ticker.C {
		state.Mu.RLock()
		currentPeerAddr := state.CurrentP2PPeerAddr
		state.Mu.RUnlock()

		// If peer changed, update keep-alive
		if (currentPeerAddr == nil && lastPeerAddr != nil) ||
			(currentPeerAddr != nil && lastPeerAddr == nil) ||
			(currentPeerAddr != nil && lastPeerAddr != nil && currentPeerAddr.String() != lastPeerAddr.String()) {

			if currentPeerAddr != nil && p2pProto != nil {
				log.Printf("Starting keep-alive for new peer: %s", currentPeerAddr.String())
				p2pProto.StartKeepAlive(currentPeerAddr)
			}
			lastPeerAddr = currentPeerAddr
		}
	}
}
