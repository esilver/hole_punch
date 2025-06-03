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
	// Use ephemeral port (0) to avoid "address already in use" errors
	// when multiple instances are running
	listenAddr := "0.0.0.0:0"
	
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

	// Give the HTTP server a moment to start
	time.Sleep(100 * time.Millisecond)

	// Now setup UDP and STUN discovery after HTTP server is running
	go func() {
		log.Printf("Starting UDP setup and STUN discovery...")

		// Setup UDP listener FIRST with ephemeral port
		var err error
		udpConn, err = setupUDPListener(udpPort)
		if err != nil {
			log.Printf("Failed to setup UDP listener: %v", err)
			// Continue anyway - we might still be able to connect to rendezvous
		} else {
			// Get the actual port we're listening on
			actualAddr := udpConn.LocalAddr().(*net.UDPAddr)
			actualPort := actualAddr.Port
			log.Printf("UDP listener established on ephemeral port %d", actualPort)
			
			// Initialize P2P protocol
			p2pProto = p2p.NewP2PProtocol(udpConn, state)
			go p2pProto.Start()
			
			// Now perform STUN discovery on the actual ephemeral port
			stunHost := getEnvStr("STUN_HOST", "stun.l.google.com")
			stunPort := getEnvInt("STUN_PORT", 19302)

			type stunResult struct {
				result *stun.STUNResult
				err    error
			}
			stunChan := make(chan stunResult, 1)

			go func() {
				log.Printf("Attempting STUN discovery on ephemeral port %d with %s:%d", actualPort, stunHost, stunPort)
				
				// Use ephemeral port for STUN discovery too
				localAddr := fmt.Sprintf(":%d", 0)
				
				result, err := stun.DiscoverWithRetry(localAddr, stunHost, stunPort, 3)
				stunChan <- stunResult{result: result, err: err}
			}()

			// Wait for STUN with overall timeout
			select {
			case res := <-stunChan:
				if res.err != nil {
					log.Printf("STUN discovery failed: %v", res.err)
					log.Printf("Continuing without STUN endpoint (like Python implementation)")
					// Leave OurStunDiscoveredUDPIP and OurStunDiscoveredUDPPort empty
				} else {
					log.Printf("STUN discovery successful: %s:%d", res.result.PublicIP, res.result.PublicPort)
					state.Mu.Lock()
					state.OurStunDiscoveredUDPIP = res.result.PublicIP
					state.OurStunDiscoveredUDPPort = res.result.PublicPort
					state.Mu.Unlock()
				}

			case <-time.After(10 * time.Second):
				log.Printf("STUN discovery timed out after 10 seconds")
				log.Printf("Continuing without STUN endpoint (like Python implementation)")
				// Leave OurStunDiscoveredUDPIP and OurStunDiscoveredUDPPort empty
			}
		}

		// Connect to rendezvous service after STUN discovery
		connectToRendezvous(workerID, rendezvousURL)
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
	retryCount := 0
	maxRetries := 60 // Try for up to 5 minutes

	for retryCount < maxRetries {
		log.Printf("Worker %s: Attempting to connect to rendezvous service (attempt %d/%d)...", workerID, retryCount+1, maxRetries)
		wsClient = ws.NewClient(rendezvousURL + "/ws/register/" + workerID)
		setupWebSocketHandlers()

		if err := wsClient.Connect(); err != nil {
			retryCount++
			log.Printf("Worker %s: Failed to connect to rendezvous service: %v", workerID, err)
			log.Printf("Worker %s: Will retry in 5 seconds... (attempt %d/%d)", workerID, retryCount, maxRetries)
			time.Sleep(5 * time.Second)
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

	if retryCount >= maxRetries {
		log.Printf("Worker %s: Failed to connect to rendezvous service after %d attempts. Continuing anyway...", workerID, maxRetries)
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

		// Start keep-alive and pairing test only if p2pProto is initialized
		if p2pProto != nil {
			p2pProto.StartKeepAlive(peerAddr)
			p2pProto.InitiatePairingTest(peerID, peerAddr)
		} else {
			log.Printf("Worker '%s': P2P protocol not yet initialized, skipping keep-alive and pairing test", state.WorkerID)
		}
	}()

	// Notify UI
	state.Mu.RLock()
	clients := state.UIWebsocketClients
	state.Mu.RUnlock()

	uiMsg := map[string]interface{}{
		"type": "p2p_connection_established",
		"payload": map[string]interface{}{
			"peer_id":   peerID,
			"peer_addr": peerAddr.String(),
		},
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
		case "send_admin_response":
			payload := msg["payload"].(map[string]interface{})
			if wsClient != nil {
				wsClient.SendMessage("chat_response", map[string]interface{}{
					"worker_id":        state.WorkerID,
					"admin_session_id": payload["admin_session_id"],
					"content":          payload["content"],
				})
			} else {
				log.Printf("Cannot send admin response - not connected to rendezvous")
			}
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
			err := p2pProto.SendMessage("chat_message", map[string]interface{}{"content": content}, peerAddr)
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

			reqChunkSize := 1024
			reqNumChunks := sizeKb

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
				data := make([]byte, reqChunkSize)
				for i := 0; i < reqNumChunks; i++ {
					payloadBase64 := base64.StdEncoding.EncodeToString(data)
					p2pProto.SendMessage("benchmark_chunk", map[string]interface{}{
						"session_id":     sessionID,
						"seq":            i,
						"payload":        payloadBase64,
						"from_worker_id": state.WorkerID,
					}, peerAddrBenchmark)

				}
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
