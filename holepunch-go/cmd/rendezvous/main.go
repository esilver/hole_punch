package main

import (
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"strconv"
	"time"

	"github.com/gorilla/websocket"
	"github.com/labstack/echo/v4"
	"github.com/labstack/echo/v4/middleware"

	"github.com/elisilver/holepunch/pkg/models"
)

var (
	state    *models.ServiceState
	upgrader = websocket.Upgrader{
		CheckOrigin:       func(r *http.Request) bool { return true },
		EnableCompression: true,
	}
)

func main() {
	// Startup banner with revision info
	log.Println("=====================================")
	log.Println("Holepunch Go Rendezvous Service v1.0")
	log.Println("Revision: Fixed port binding race condition")
	log.Println("Build Date:", time.Now().Format("2006-01-02 15:04:05"))
	log.Println("=====================================")
	
	state = models.NewServiceState()

	port := getEnvInt("PORT", 8080)

	e := echo.New()
	e.Use(middleware.Logger())
	e.Use(middleware.Recover())
	e.Use(middleware.CORS())

	// Static files
	e.Static("/", "./web/rendezvous")
	e.File("/admin", "./web/rendezvous/admin/admin.html")
	e.File("/admin-chat", "./web/rendezvous/admin/admin-chat.html")

	// WebSocket endpoints
	e.GET("/ws/register/:worker_id", handleWorkerWebSocket)
	e.GET("/ws/admin", handleAdminWebSocket)
	e.GET("/ws/chat/:admin_session_id/:worker_id", handleAdminChatWebSocket)

	// API endpoints
	e.GET("/api/workers", getWorkers)
	e.POST("/api/connect", connectWorkers)


	log.Printf("Rendezvous service starting on port %d", port)
	if err := e.Start(fmt.Sprintf(":%d", port)); err != nil {
		log.Fatalf("Failed to start server: %v", err)
	}
}

func handleWorkerWebSocket(c echo.Context) error {
	log.Printf("WebSocket upgrade request from %s for worker %s", c.Request().RemoteAddr, c.Param("worker_id"))

	ws, err := upgrader.Upgrade(c.Response(), c.Request(), nil)
	if err != nil {
		log.Printf("WebSocket upgrade failed: %v", err)
		return err
	}
	defer ws.Close()

	workerID := c.Param("worker_id")
	clientIP := c.RealIP()
	
	// Extract client port from RemoteAddr
	clientAddr := c.Request().RemoteAddr
	var clientPort int
	if _, portStr, err := net.SplitHostPort(clientAddr); err == nil {
		clientPort, _ = strconv.Atoi(portStr)
	}
	
	log.Printf("Worker %s connected from %s:%d", workerID, clientIP, clientPort)

	// Check for duplicate worker and handle it
	state.Mu.Lock()
	if oldWorker, exists := state.ConnectedWorkers[workerID]; exists {
		log.Printf("Worker %s re-connecting, closing old connection", workerID)
		if oldWorker.Websocket != nil {
			oldWorker.Websocket.WriteMessage(websocket.CloseMessage,
				websocket.FormatCloseMessage(websocket.CloseServiceRestart, "New connection from same worker ID"))
			oldWorker.Websocket.Close()
		}
		// Remove from pairing list if present
		newPairingList := []string{}
		for _, id := range state.WorkersReadyForPairing {
			if id != workerID {
				newPairingList = append(newPairingList, id)
			}
		}
		state.WorkersReadyForPairing = newPairingList
	}
	
	// Initialize worker info
	state.ConnectedWorkers[workerID] = &models.WorkerInfo{
		ID:                    workerID,
		WebsocketObservedIP:   clientIP,
		WebsocketObservedPort: clientPort,
		ConnectedAt:           time.Now(),
		LastPingAt:            time.Now(),
		Websocket:             ws,
	}
	state.Mu.Unlock()

	log.Printf("Worker %s registered", workerID)
	broadcastWorkerUpdate()
	broadcastWorkerConnected(workerID, clientIP)

	// Read and process the first message, then loop for subsequent messages
	var firstMsg models.WebSocketMessage
	if err := ws.ReadJSON(&firstMsg); err != nil {
		log.Printf("Failed to read first message from worker %s: %v", workerID, err)
		removeWorker(workerID)
		return nil // End handler for this worker
	}
	// Process the first message immediately
	handleWorkerMessage(workerID, &firstMsg)

	// Handle subsequent messages in a loop
	// N.B.: The original `go func() { ... handleWorkerMessage ...}` loop is removed
	// as messages will now be processed sequentially in the main handler goroutine after the first.
	// This also simplifies reasoning about when removeWorker is called relative to message handling.

	// Keep pong handler to handle pings from worker
	ws.SetReadDeadline(time.Now().Add(35 * time.Second)) // Set initial read deadline
	ws.SetPongHandler(func(string) error {
		ws.SetReadDeadline(time.Now().Add(35 * time.Second)) // Reset read deadline on pong
		state.Mu.Lock()
		if worker, ok := state.ConnectedWorkers[workerID]; ok {
			worker.LastPingAt = time.Now()
		}
		state.Mu.Unlock()
		return nil
	})

	// Main message reading loop
	for {
		var msg models.WebSocketMessage
		if err := ws.ReadJSON(&msg); err != nil {
			if websocket.IsUnexpectedCloseError(err, websocket.CloseGoingAway, websocket.CloseAbnormalClosure) {
				log.Printf("Worker %s read error: %v", workerID, err)
			} else {
				log.Printf("Worker %s disconnected (read deadline or other error): %v", workerID, err)
			}
			removeWorker(workerID)
			return nil
		}
		ws.SetReadDeadline(time.Now().Add(35 * time.Second)) // Reset read deadline after successful message read
		handleWorkerMessage(workerID, &msg)
	}
}

func handleWorkerMessage(workerID string, msg *models.WebSocketMessage) {
	switch msg.Type {
	case "update_udp_endpoint":
		publicIP, ipOk := msg.Payload["udp_ip"].(string)

		// Simplified UDP port parsing to match Python's behavior
		var publicPort int
		portVal, portOk := msg.Payload["udp_port"]
		if !portOk {
			log.Printf("Worker %s: udp_port missing in endpoint update", workerID)
			return
		}
		switch v := portVal.(type) {
		case float64:
			publicPort = int(v)
		case int:
			publicPort = v
		default:
			log.Printf("Worker %s: invalid udp_port type: %T", workerID, v)
			return
		}

		if !ipOk || publicIP == "" {
			log.Printf("Worker %s sent invalid UDP endpoint update", workerID)
			return
		}

		state.Mu.Lock()
		if worker, ok := state.ConnectedWorkers[workerID]; ok {
			worker.StunReportedUDPIP = publicIP
			worker.StunReportedUDPPort = publicPort

			// Add to pairing list if not already there
			found := false
			for _, id := range state.WorkersReadyForPairing {
				if id == workerID {
					found = true
					break
				}
			}
			if !found {
				state.WorkersReadyForPairing = append(state.WorkersReadyForPairing, workerID)
			}
		}
		state.Mu.Unlock()

		// Send acknowledgment
		response := models.WebSocketMessage{
			Type: "udp_endpoint_ack",
			Payload: map[string]interface{}{
				"status": "received",
			},
		}

		state.Mu.RLock()
		if worker, ok := state.ConnectedWorkers[workerID]; ok {
			worker.Websocket.WriteJSON(response)
		}
		state.Mu.RUnlock()

		log.Printf("Worker %s UDP endpoint updated: %s:%d", workerID, publicIP, publicPort)
		broadcastWorkerUpdate()
		broadcastWorkerUDPUpdated(workerID, publicIP, publicPort)

		// Attempt to pair workers
		attemptToPairWorkers(workerID)

	case "echo_request":
		data := msg.Payload["data"]
		response := models.WebSocketMessage{
			Type: "echo_response",
			Payload: map[string]interface{}{
				"data": data,
			},
		}

		state.Mu.RLock()
		if worker, ok := state.ConnectedWorkers[workerID]; ok {
			worker.Websocket.WriteJSON(response)
		}
		state.Mu.RUnlock()

	case "chat_response":
		adminSessionID := msg.Payload["admin_session_id"].(string)
		content := msg.Payload["content"].(string)

		state.Mu.RLock()
		if sessions, ok := state.ChatSessions[adminSessionID]; ok {
			if adminWS, ok := sessions[workerID]; ok {
				adminWS.WriteJSON(map[string]interface{}{
					"type": "chat_message",
					"from": "worker",
					"payload": map[string]interface{}{
						"worker_id": workerID,
						"content":   content,
						"timestamp": time.Now().Format(time.RFC3339),
					},
				})
			}
		}
		state.Mu.RUnlock()
	}
}

func handleAdminWebSocket(c echo.Context) error {
	ws, err := upgrader.Upgrade(c.Response(), c.Request(), nil)
	if err != nil {
		return err
	}
	defer ws.Close()

	state.Mu.Lock()
	state.AdminWebsocketClients = append(state.AdminWebsocketClients, ws)
	state.Mu.Unlock()

	// Send initial connection message like Python
	ws.WriteJSON(map[string]interface{}{
		"type":    "connected",
		"message": "Connected to admin WebSocket",
	})

	// Keep connection alive and handle text-based ping
	for {
		messageType, p, err := ws.ReadMessage()
		if err != nil {
			removeAdminClient(ws)
			return nil
		}
		// Handle text-based ping from admin UI
		if messageType == websocket.TextMessage && string(p) == "ping" {
			// Send JSON pong response like Python does
			if err := ws.WriteJSON(map[string]interface{}{"type": "pong"}); err != nil {
				log.Printf("Admin pong failed: %v", err)
				removeAdminClient(ws)
				return nil
			}
		}
	}
}

func handleAdminChatWebSocket(c echo.Context) error {
	workerID := c.Param("worker_id")
	adminSessionID := c.Param("admin_session_id")

	ws, err := upgrader.Upgrade(c.Response(), c.Request(), nil)
	if err != nil {
		return err
	}
	defer ws.Close()

	state.Mu.Lock()
	if state.ChatSessions[adminSessionID] == nil {
		state.ChatSessions[adminSessionID] = make(map[string]*websocket.Conn)
	}
	state.ChatSessions[adminSessionID][workerID] = ws
	state.Mu.Unlock()

	// Send initial connection confirmation
	if err := ws.WriteJSON(map[string]interface{}{
		"type":      "chat_connected",
		"worker_id": workerID,
		"message":   fmt.Sprintf("Connected to worker %s for chat", workerID),
	}); err != nil {
		log.Printf("Failed to send chat_connected to admin %s for worker %s: %v", adminSessionID, workerID, err)
		return err
	}

	// Handle messages from admin
	for {
		var msg map[string]interface{}
		if err := ws.ReadJSON(&msg); err != nil {
			state.Mu.Lock()
			delete(state.ChatSessions[adminSessionID], workerID)
			state.Mu.Unlock()
			return nil
		}

		if msg["type"] == "chat_message" {
			content := msg["content"].(string)

			state.Mu.RLock()
			if worker, ok := state.ConnectedWorkers[workerID]; ok {
				worker.Websocket.WriteJSON(models.WebSocketMessage{
					Type: "admin_chat_message",
					Payload: map[string]interface{}{
						"admin_session_id": adminSessionID,
						"content":          content,
					},
				})
			}
			state.Mu.RUnlock()

			// Echo the message back to admin
			if err := ws.WriteJSON(map[string]interface{}{
				"type":      "chat_message",
				"from":      "admin",
				"content":   content,
				"timestamp": float64(time.Now().UnixNano()) / 1e9,
			}); err != nil {
				log.Printf("Failed to echo chat message to admin %s for worker %s: %v", adminSessionID, workerID, err)
			}
		}
	}
}

func getWorkers(c echo.Context) error {
	workersList, counts := getWorkersInfo()
	return c.JSON(200, map[string]interface{}{
		"workers":         workersList,
		"total_count":     counts["total"],
		"connected_count": counts["connected"],
		"ready_count":     counts["ready"],
	})
}

func connectWorkers(c echo.Context) error {
	var req struct {
		WorkerAID string `json:"worker_a_id"`
		WorkerBID string `json:"worker_b_id"`
	}

	if err := c.Bind(&req); err != nil {
		return c.JSON(400, map[string]string{"error": "Invalid request"})
	}

	if err := pairWorkers(req.WorkerAID, req.WorkerBID); err != nil {
		return c.JSON(400, map[string]string{"error": err.Error()})
	}

	return c.JSON(200, map[string]string{"status": "success", "message": "Workers connected"})
}

func pairWorkers(workerID1, workerID2 string) error {
	state.Mu.RLock()
	worker1, ok1 := state.ConnectedWorkers[workerID1]
	worker2, ok2 := state.ConnectedWorkers[workerID2]
	state.Mu.RUnlock()

	if !ok1 || !ok2 {
		return fmt.Errorf("one or both workers not found")
	}

	if worker1.StunReportedUDPIP == "" || worker2.StunReportedUDPIP == "" {
		return fmt.Errorf("UDP endpoints not ready")
	}

	// Send connection offers to both workers
	offer1 := models.WebSocketMessage{
		Type: "p2p_connection_offer",
		Payload: map[string]interface{}{
			"peer_worker_id": workerID2,
			"peer_udp_ip":    worker2.StunReportedUDPIP,
			"peer_udp_port":  worker2.StunReportedUDPPort,
		},
	}

	offer2 := models.WebSocketMessage{
		Type: "p2p_connection_offer",
		Payload: map[string]interface{}{
			"peer_worker_id": workerID1,
			"peer_udp_ip":    worker1.StunReportedUDPIP,
			"peer_udp_port":  worker1.StunReportedUDPPort,
		},
	}

	if err := worker1.Websocket.WriteJSON(offer1); err != nil {
		return fmt.Errorf("failed to send offer to worker1: %w", err)
	}

	if err := worker2.Websocket.WriteJSON(offer2); err != nil {
		return fmt.Errorf("failed to send offer to worker2: %w", err)
	}

	log.Printf("Paired workers %s and %s", workerID1, workerID2)
	broadcastConnectionInitiated(workerID1, workerID2)
	return nil
}

func removeWorker(workerID string) {
	state.Mu.Lock()
	delete(state.ConnectedWorkers, workerID)

	// Remove from pairing list
	newList := make([]string, 0)
	for _, id := range state.WorkersReadyForPairing {
		if id != workerID {
			newList = append(newList, id)
		}
	}
	state.WorkersReadyForPairing = newList
	state.Mu.Unlock()

	broadcastWorkerUpdate()
	broadcastWorkerDisconnected(workerID)
}

func removeAdminClient(ws *websocket.Conn) {
	state.Mu.Lock()
	newList := make([]*websocket.Conn, 0)
	for _, client := range state.AdminWebsocketClients {
		if client != ws {
			newList = append(newList, client)
		}
	}
	state.AdminWebsocketClients = newList
	state.Mu.Unlock()
}

func attemptToPairWorkers(newWorkerID string) {
	state.Mu.Lock()
	defer state.Mu.Unlock()

	// Build extra_candidates: all connected workers with UDP info not in ready list
	extraCandidates := []string{}
	for id, worker := range state.ConnectedWorkers {
		if id == newWorkerID {
			continue // Skip the new worker itself
		}
		if worker.Websocket != nil && worker.StunReportedUDPIP != "" && worker.StunReportedUDPPort > 0 {
			// Check if not already in WorkersReadyForPairing
			inReadyList := false
			for _, readyID := range state.WorkersReadyForPairing {
				if readyID == id {
					inReadyList = true
					break
				}
			}
			if !inReadyList {
				extraCandidates = append(extraCandidates, id)
			}
		}
	}

	// Combine WorkersReadyForPairing with extraCandidates for candidate pool
	candidatePool := make([]string, len(state.WorkersReadyForPairing))
	copy(candidatePool, state.WorkersReadyForPairing)
	candidatePool = append(candidatePool, extraCandidates...)

	// Check if we have at least 2 candidates for pairing
	if len(candidatePool) < 2 {
		log.Printf("Not enough workers available for pairing (%d total candidates). Waiting for more.", len(candidatePool))
		return
	}

	// Find two workers to pair from the candidate pool
	var worker1ID, worker2ID string
	for _, id := range candidatePool {
		if worker1ID == "" {
			worker1ID = id
		} else if worker2ID == "" {
			worker2ID = id
			break
		}
	}

	// Validate both workers
	worker1, ok1 := state.ConnectedWorkers[worker1ID]
	worker2, ok2 := state.ConnectedWorkers[worker2ID]

	if !ok1 || !ok2 || worker1.StunReportedUDPIP == "" || worker2.StunReportedUDPIP == "" {
		log.Printf("Workers not ready for pairing (missing data)")
		return
	}

	// Remove from ready list (if they were in it)
	newReadyList := []string{}
	for _, id := range state.WorkersReadyForPairing {
		if id != worker1ID && id != worker2ID {
			newReadyList = append(newReadyList, id)
		}
	}
	state.WorkersReadyForPairing = newReadyList

	log.Printf("Pairing workers %s and %s", worker1ID, worker2ID)

	// Send connection offers
	offer1 := models.WebSocketMessage{
		Type: "p2p_connection_offer",
		Payload: map[string]interface{}{
			"peer_worker_id": worker2ID,
			"peer_udp_ip":    worker2.StunReportedUDPIP,
			"peer_udp_port":  worker2.StunReportedUDPPort,
		},
	}

	offer2 := models.WebSocketMessage{
		Type: "p2p_connection_offer",
		Payload: map[string]interface{}{
			"peer_worker_id": worker1ID,
			"peer_udp_ip":    worker1.StunReportedUDPIP,
			"peer_udp_port":  worker1.StunReportedUDPPort,
		},
	}

	if err := worker1.Websocket.WriteJSON(offer1); err != nil {
		log.Printf("Failed to send offer to worker %s: %v", worker1ID, err)
	}

	if err := worker2.Websocket.WriteJSON(offer2); err != nil {
		log.Printf("Failed to send offer to worker %s: %v", worker2ID, err)
	}
	
	// Broadcast pairing event
	broadcastConnectionInitiated(worker1ID, worker2ID)
}

func broadcastWorkerUpdate() {
	workers, counts := getWorkersInfo()
	
	// Create payload structure that matches the UI expectation
	payload := map[string]interface{}{
		"workers":         workers,
		"total_count":     counts["total"],
		"connected_count": counts["connected"],
		"ready_count":     counts["ready"],
	}
	
	msg := map[string]interface{}{
		"type":    "workers_update",
		"payload": payload,
	}

	state.Mu.RLock()
	clients := state.AdminWebsocketClients
	state.Mu.RUnlock()

	for _, client := range clients {
		client.WriteJSON(msg)
	}
}

func broadcastToAdmins(message map[string]interface{}) {
	state.Mu.RLock()
	clients := make([]*websocket.Conn, len(state.AdminWebsocketClients))
	copy(clients, state.AdminWebsocketClients)
	state.Mu.RUnlock()

	for _, client := range clients {
		if err := client.WriteJSON(message); err != nil {
			log.Printf("Failed to broadcast to admin client: %v", err)
		}
	}
}

func broadcastWorkerConnected(workerID string, websocketIP string) {
	state.Mu.RLock()
	totalWorkers := len(state.ConnectedWorkers)
	state.Mu.RUnlock()

	broadcastToAdmins(map[string]interface{}{
		"type": "worker_connected",
		"worker_id": workerID,
		"websocket_ip": websocketIP,
		"total_workers": totalWorkers,
	})
}

func broadcastWorkerDisconnected(workerID string) {
	state.Mu.RLock()
	totalWorkers := len(state.ConnectedWorkers)
	state.Mu.RUnlock()

	broadcastToAdmins(map[string]interface{}{
		"type": "worker_disconnected",
		"worker_id": workerID,
		"total_workers": totalWorkers,
	})
}

func broadcastWorkerUDPUpdated(workerID string, udpIP string, udpPort int) {
	broadcastToAdmins(map[string]interface{}{
		"type": "worker_udp_updated",
		"worker_id": workerID,
		"udp_ip": udpIP,
		"udp_port": udpPort,
	})
}

func broadcastConnectionInitiated(workerAID, workerBID string) {
	broadcastToAdmins(map[string]interface{}{
		"type": "connection_initiated",
		"worker_a_id": workerAID,
		"worker_b_id": workerBID,
		"timestamp": time.Now().Format(time.RFC3339),
	})
}

func getWorkersInfo() ([]map[string]interface{}, map[string]int) {
	state.Mu.RLock()
	defer state.Mu.RUnlock()

	workers := make([]map[string]interface{}, 0)
	readyCount := 0
	
	for id, worker := range state.ConnectedWorkers {
		// Check if worker is in ready for pairing list
		isReady := false
		for _, readyID := range state.WorkersReadyForPairing {
			if readyID == id {
				isReady = true
				readyCount++
				break
			}
		}
		
		workers = append(workers, map[string]interface{}{
			"worker_id":           id,
			"websocket_connected": worker.Websocket != nil,
			"has_udp_endpoint":    worker.StunReportedUDPIP != "" && worker.StunReportedUDPPort > 0,
			"ready_for_pairing":   isReady,
			"websocket_ip":        worker.WebsocketObservedIP,
			"websocket_port":      worker.WebsocketObservedPort,
			"public_ip":           worker.HTTPReportedPublicIP,  // Added for Python admin UI compatibility
			"udp_ip":              worker.StunReportedUDPIP,    // Python compatibility (without 'stun_' prefix)
			"udp_port":            worker.StunReportedUDPPort,  // Python compatibility (without 'stun_' prefix)
		})
	}
	
	counts := map[string]int{
		"total":     len(workers),
		"connected": len(workers),
		"ready":     readyCount,
	}
	
	return workers, counts
}

func getEnvInt(key string, defaultValue int) int {
	if v := os.Getenv(key); v != "" {
		if i, err := strconv.Atoi(v); err == nil {
			return i
		}
	}
	return defaultValue
}
