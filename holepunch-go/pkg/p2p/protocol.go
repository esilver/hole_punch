package p2p

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log"
	"net"
	"strings"
	"sync"
	"time"

	"github.com/elisilver/holepunch/pkg/models"
	"github.com/google/uuid"
	"github.com/gorilla/websocket"
)

type P2PProtocol struct {
	conn            *net.UDPConn
	state           *models.WorkerState
	messageHandlers map[string]MessageHandler
	mu              sync.RWMutex

	// For reassembling chunked chat messages
	chatReassemblySessions map[string]*chatReassemblySession
	chatMu                 sync.Mutex
}

type MessageHandler func(msg *models.P2PMessage, addr *net.UDPAddr) error

// chatReassemblySession keeps track of incoming chat message chunks so that they can be
// re-assembled into the original message before being forwarded to the UI. Each chunk
// is identified by its sequence index starting at 0.
type chatReassemblySession struct {
	totalChunks  int
	chunks       map[int]string
	fromWorkerID string
	createdAt    time.Time
}

func NewP2PProtocol(conn *net.UDPConn, state *models.WorkerState) *P2PProtocol {
	p := &P2PProtocol{
		conn:                   conn,
		state:                  state,
		messageHandlers:        make(map[string]MessageHandler),
		chatReassemblySessions: make(map[string]*chatReassemblySession),
	}

	p.registerHandlers()
	return p
}

func (p *P2PProtocol) registerHandlers() {
	p.messageHandlers["chat_message"] = p.handleChatMessage
	p.messageHandlers["p2p_keep_alive"] = p.handleKeepAlive
	p.messageHandlers["p2p_pairing_test"] = p.handlePairingTest
	p.messageHandlers["p2p_pairing_echo"] = p.handlePairingEcho
	p.messageHandlers["benchmark_chunk"] = p.handleBenchmarkChunk
	p.messageHandlers["benchmark_end"] = p.handleBenchmarkEnd
	p.messageHandlers["chat_message_chunk"] = p.handleChatMessageChunk
}

func (p *P2PProtocol) Start() {
	go p.readLoop()
}

func (p *P2PProtocol) readLoop() {
	buffer := make([]byte, 65535)

	log.Printf("P2P UDP Read: Starting readLoop for worker %s on %s", p.state.WorkerID, p.conn.LocalAddr().String())

	for {
		n, addr, err := p.conn.ReadFromUDP(buffer)
		if addr != nil {
			log.Printf("P2P UDP Read: Raw packet received from %s on %s (Worker: %s)", addr.String(), p.conn.LocalAddr().String(), p.state.WorkerID)
		}

		if err != nil {
			if strings.Contains(err.Error(), "use of closed network connection") {
				log.Printf("P2P UDP Read: Listener closed, exiting readLoop for worker %s.", p.state.WorkerID)
				return
			}
			log.Printf("P2P UDP Read: Error reading from UDP for worker %s: %v", p.state.WorkerID, err)
			continue
		}

		messageStr := string(buffer[:n])
		log.Printf("P2P UDP Read: Received %d bytes from %s. Raw content: %s", n, addr.String(), messageStr)

		if strings.Contains(messageStr, "P2P_PING_FROM_") {
			log.Printf("P2P UDP Read: Worker '%s': !!! P2P UDP Ping (legacy) received from %s !!!", p.state.WorkerID, addr.String())
			p.state.Mu.Lock()
			p.state.LastP2PMessageTime = time.Now()
			p.state.Mu.Unlock()
			continue
		}

		// Check if this might be a STUN response (from known STUN servers)
		if addr.Port == 19302 || addr.Port == 3478 {
			// This is likely a STUN response - ignore it
			log.Printf("P2P UDP Read: Ignoring STUN response from %s (%d bytes)", addr.String(), n)
			continue
		}

		var msg models.P2PMessage
		if err := json.Unmarshal(buffer[:n], &msg); err != nil {
			log.Printf("P2P UDP Read: Failed to unmarshal P2P message from %s for worker %s. Error: %v. Raw: '%s'", addr.String(), p.state.WorkerID, err, messageStr)
			continue
		}
		log.Printf("P2P UDP Read: Successfully unmarshalled message type '%s' from %s (FromWorkerID: %s) for worker %s", msg.Type, addr.String(), msg.FromWorkerID, p.state.WorkerID)

		p.state.Mu.Lock()
		p.state.LastP2PMessageTime = time.Now()
		p.state.Mu.Unlock()

		if handler, ok := p.messageHandlers[msg.Type]; ok {
			if err := handler(&msg, addr); err != nil {
				log.Printf("P2P UDP Read: Error in handler for message type '%s' from %s for worker %s: %v", msg.Type, addr.String(), p.state.WorkerID, err)
			}
		} else {
			log.Printf("P2P UDP Read: Unknown P2P message type '%s' from %s for worker %s. Raw: '%s'", msg.Type, addr.String(), p.state.WorkerID, messageStr)
		}
	}
}

func (p *P2PProtocol) SendMessage(msgType string, payload map[string]interface{}, addr *net.UDPAddr) error {
	msg := models.P2PMessage{
		Type:         msgType,
		FromWorkerID: p.state.WorkerID,
		Payload:      payload,
		Timestamp:    time.Now(),
	}

	data, err := json.Marshal(msg)
	if err != nil {
		log.Printf("P2P UDP Send: Failed to marshal message type '%s' for %s: %v", msgType, addr.String(), err)
		return fmt.Errorf("failed to marshal message: %w", err)
	}

	n, err := p.conn.WriteToUDP(data, addr)
	if err != nil {
		log.Printf("P2P UDP Send: Failed to write UDP data for message type '%s' to %s: %v", msgType, addr.String(), err)
		return err
	}
	log.Printf("P2P UDP Send: Successfully sent %d bytes of type '%s' (From: %s) to %s", n, msgType, p.state.WorkerID, addr.String())
	return nil
}

func (p *P2PProtocol) handleChatMessage(msg *models.P2PMessage, addr *net.UDPAddr) error {
	content, ok := msg.Payload["content"].(string)
	if !ok {
		log.Printf("P2P Chat: Invalid chat message format from %s for worker %s. Payload: %v", addr.String(), p.state.WorkerID, msg.Payload)
		return fmt.Errorf("invalid chat message format")
	}

	log.Printf("P2P Chat: Worker %s received chat from %s (PeerWorkerID: %s): %s", p.state.WorkerID, addr.String(), msg.FromWorkerID, content)

	p.state.Mu.RLock()
	clients := make([]*websocket.Conn, len(p.state.UIWebsocketClients))
	copy(clients, p.state.UIWebsocketClients)
	p.state.Mu.RUnlock()

	uiMsg := map[string]interface{}{
		"type": "p2p_message_received",
		"payload": map[string]interface{}{
			"from_peer_id": msg.FromWorkerID,
			"content":      content,
		},
	}

	for _, client := range clients {
		if client == nil {
			log.Printf("P2P Chat: Worker %s found nil UI websocket client when trying to forward message from %s", p.state.WorkerID, msg.FromWorkerID)
			continue
		}
		if err := client.WriteJSON(uiMsg); err != nil {
			log.Printf("P2P Chat: Worker %s failed to forward chat message from %s to UI client %s: %v", p.state.WorkerID, msg.FromWorkerID, client.RemoteAddr(), err)
		} else {
			log.Printf("P2P Chat: Worker %s successfully forwarded chat message from %s to UI client %s", p.state.WorkerID, msg.FromWorkerID, client.RemoteAddr())
		}
	}

	return nil
}

func (p *P2PProtocol) handleKeepAlive(msg *models.P2PMessage, addr *net.UDPAddr) error {
	log.Printf("Received keep-alive from %s (WorkerID: %s)", addr, msg.FromWorkerID)
	return nil
}

func (p *P2PProtocol) handlePairingTest(msg *models.P2PMessage, addr *net.UDPAddr) error {
	timestampPayload, ok := msg.Payload["timestamp"].(float64)
	if !ok {
		timestampInt64, okInt := msg.Payload["timestamp"].(int64)
		if !okInt {
			return fmt.Errorf("invalid pairing test format: missing or invalid type for timestamp. Got: %T", msg.Payload["timestamp"])
		}
		timestampPayload = float64(timestampInt64)
	}

	log.Printf("Received pairing test from %s (WorkerID: %s, Timestamp: %v). Sending echo.", addr, msg.FromWorkerID, timestampPayload)

	return p.SendMessage("p2p_pairing_echo", map[string]interface{}{
		"original_timestamp": timestampPayload,
	}, addr)
}

func (p *P2PProtocol) handlePairingEcho(msg *models.P2PMessage, addr *net.UDPAddr) error {
	originalTimestampFloat, ok := msg.Payload["original_timestamp"].(float64)
	if !ok {
		return fmt.Errorf("invalid pairing echo format: missing or invalid original_timestamp. Got: %T", msg.Payload["original_timestamp"])
	}

	// Calculate RTT in milliseconds, matching Python's float arithmetic then convert
	rttMs := ((float64(time.Now().UnixNano()) / 1e9) - originalTimestampFloat) * 1000.0
	log.Printf("Pairing test with %s (WorkerID: %s) completed. RTT: %.2fms", addr, msg.FromWorkerID, rttMs)

	p.state.Mu.RLock()
	clients := p.state.UIWebsocketClients
	p.state.Mu.RUnlock()

	// Use message type that matches the UI expectation
	uiMsg := map[string]interface{}{
		"type":    "p2p_status_update",
		"message": fmt.Sprintf("Pairing test successful! RTT: %.2fms", rttMs),
		"peer_id": msg.FromWorkerID,
	}

	for _, client := range clients {
		if err := client.WriteJSON(uiMsg); err != nil {
			log.Printf("Failed to send pairing test result to UI: %v", err)
		}
	}

	return nil
}

func (p *P2PProtocol) handleBenchmarkChunk(msg *models.P2PMessage, addr *net.UDPAddr) error {
	sessionID, ok := msg.Payload["session_id"].(string)
	if !ok {
		return fmt.Errorf("invalid benchmark chunk format: missing session_id")
	}

	// Extract total_chunks if present
	totalChunksFromMsg := 0
	if tcVal, ok := msg.Payload["total_chunks"]; ok {
		switch v := tcVal.(type) {
		case float64:
			totalChunksFromMsg = int(v)
		case int:
			totalChunksFromMsg = v
		}
	}

	// Extract from_worker_id if present (for Python compatibility)
	fromWorkerID, _ := msg.Payload["from_worker_id"].(string)

	// Handle base64 encoded payload from Python-style implementation
	payloadB64, ok := msg.Payload["payload"].(string)
	if !ok {
		return fmt.Errorf("invalid benchmark chunk format: missing payload string")
	}
	decodedPayload, err := base64.StdEncoding.DecodeString(payloadB64)
	if err != nil {
		return fmt.Errorf("failed to decode base64 benchmark payload: %w", err)
	}
	dataSize := len(decodedPayload) // Actual size after decode

	p.state.Mu.Lock()
	session, exists := p.state.BenchmarkSessions[sessionID]
	if !exists {
		session = &models.BenchmarkSession{
			StartTime:                   time.Now(),
			Active:                      true,
			TotalChunks:                 totalChunksFromMsg, // Initialize with total_chunks from first message
			LastReportedProgressPercent: 0,
		}
		p.state.BenchmarkSessions[sessionID] = session
		log.Printf("Started new benchmark session %s from worker %s, expecting %d chunks", sessionID, fromWorkerID, totalChunksFromMsg)
	}

	// If TotalChunks wasn't set by the first message (e.g. older sender), update it.
	// This is a fallback, ideally the first message always contains it.
	if session.TotalChunks == 0 && totalChunksFromMsg > 0 {
		session.TotalChunks = totalChunksFromMsg
	}

	session.BytesReceived += int64(dataSize)
	session.ChunksReceived++

	// Report progress every ~2% via chat message
	if session.TotalChunks > 0 {
		currentProgressPercent := (session.ChunksReceived * 100) / session.TotalChunks
		// Ensure progress is capped at 100 for the last message
		if session.ChunksReceived == session.TotalChunks {
			currentProgressPercent = 100
		}

		if currentProgressPercent >= session.LastReportedProgressPercent+2 || currentProgressPercent == 100 {
			// Ensure we don't send duplicate 100% messages if the last chunk itself triggers a >2% step to 100%
			if !(session.LastReportedProgressPercent == 100 && currentProgressPercent == 100 && session.ChunksReceived < session.TotalChunks) {
				progressMsg := fmt.Sprintf("[Receiver] Benchmark progress: %d%% (%d/%d chunks received)", currentProgressPercent, session.ChunksReceived, session.TotalChunks)
				// Send chat message back to the sender (addr)
				p.SendChatMessage(progressMsg, addr) // Using existing SendChatMessage method
				session.LastReportedProgressPercent = currentProgressPercent
			}
		}
	}
	p.state.Mu.Unlock()

	return nil
}

func (p *P2PProtocol) handleBenchmarkEnd(msg *models.P2PMessage, addr *net.UDPAddr) error {
	sessionID, ok := msg.Payload["session_id"].(string)
	if !ok {
		return fmt.Errorf("invalid benchmark end format")
	}

	// Extract total_chunks if present (for Python compatibility)
	var totalChunks int
	if tcVal, ok := msg.Payload["total_chunks"]; ok {
		switch v := tcVal.(type) {
		case float64:
			totalChunks = int(v)
		case int:
			totalChunks = v
		}
	}

	// Extract from_worker_id if present (for Python compatibility)
	fromWorkerID, _ := msg.Payload["from_worker_id"].(string)

	p.state.Mu.Lock()
	session, exists := p.state.BenchmarkSessions[sessionID]
	if exists {
		session.Active = false
		duration := time.Since(session.StartTime).Seconds()
		throughput := float64(session.BytesReceived) / duration / 1024 / 1024

		log.Printf("Benchmark %s completed: %.2f MB in %.2fs (%.2f MB/s) from worker %s (expected %d chunks, received %d)",
			sessionID, float64(session.BytesReceived)/1024/1024, duration, throughput,
			fromWorkerID, totalChunks, session.ChunksReceived)

		// Send results to UI
		p.state.Mu.Unlock() // Unlock before sending to UI to avoid deadlock

		// Notify UI clients about benchmark completion
		p.state.Mu.RLock()
		clients := p.state.UIWebsocketClients
		p.state.Mu.RUnlock()

		// Compose user-friendly summary message for UI convenience
		summary := fmt.Sprintf("Benchmark completed: %.2f MB in %.2fs (%.2f MB/s)",
			float64(session.BytesReceived)/1024/1024, duration, throughput)

		uiMsg := map[string]interface{}{
			"type":    "benchmark_status",
			"message": summary,
			"payload": map[string]interface{}{
				"status":          "completed",
				"session_id":      sessionID,
				"from_worker_id":  fromWorkerID,
				"bytes_received":  session.BytesReceived,
				"duration_sec":    duration,
				"throughput_mbps": throughput,
				"chunks_received": session.ChunksReceived,
				"total_chunks":    totalChunks,
			},
		}

		for _, client := range clients {
			if err := client.WriteJSON(uiMsg); err != nil {
				log.Printf("Failed to send benchmark results to UI: %v", err)
			}
		}

		// Clean up session
		p.state.Mu.Lock()
		delete(p.state.BenchmarkSessions, sessionID)
	}
	p.state.Mu.Unlock()

	return nil
}

func (p *P2PProtocol) StartKeepAlive(peerAddr *net.UDPAddr) {
	p.state.Mu.Lock()
	if p.state.P2PKeepAliveTask != nil {
		close(p.state.P2PKeepAliveTask)
	}
	p.state.P2PKeepAliveTask = make(chan struct{})
	task := p.state.P2PKeepAliveTask
	p.state.Mu.Unlock()

	go func() {
		ticker := time.NewTicker(15 * time.Second)
		defer ticker.Stop()

		for {
			select {
			case <-ticker.C:
				if err := p.SendMessage("p2p_keep_alive", map[string]interface{}{}, peerAddr); err != nil {
					log.Printf("Failed to send keep-alive: %v", err)
				}
			case <-task:
				return
			}
		}
	}()
}

func (p *P2PProtocol) InitiatePairingTest(peerID string, peerAddr *net.UDPAddr) {
	if p.state.WorkerID < peerID {
		log.Printf("Initiating pairing test with %s (Peer UDP: %s)", peerID, peerAddr.String())

		if err := p.SendMessage("p2p_pairing_test", map[string]interface{}{
			"timestamp": float64(time.Now().UnixNano()) / 1e9, // Python's time.time() returns a float
		}, peerAddr); err != nil {
			log.Printf("Failed to send pairing test: %v", err)
		}
	}
}

// SendChatMessage will transparently split large chat messages into multiple UDP
// datagrams so that each individual packet stays well below typical MTU limits (~1.2KB).
// For small messages (\<= maxChunkSize runes) a regular single "chat_message" is sent.
func (p *P2PProtocol) SendChatMessage(content string, addr *net.UDPAddr) error {
	const maxChunkSize = 900 // runes; keeps payload < ~1.2KB once JSON encoded

	// Fast-path for short messages
	if len([]rune(content)) <= maxChunkSize {
		return p.SendMessage("chat_message", map[string]interface{}{"content": content}, addr)
	}

	sessionID := uuid.New().String()
	runes := []rune(content)
	total := (len(runes) + maxChunkSize - 1) / maxChunkSize

	for i := 0; i < total; i++ {
		start := i * maxChunkSize
		end := start + maxChunkSize
		if end > len(runes) {
			end = len(runes)
		}

		chunk := string(runes[start:end])

		payload := map[string]interface{}{
			"session_id":    sessionID,
			"seq":           i,
			"total":         total,
			"content_chunk": chunk,
		}

		if err := p.SendMessage("chat_message_chunk", payload, addr); err != nil {
			return err
		}

		// Small delay to reduce likelihood of UDP queue drops when many chunks
		time.Sleep(5 * time.Millisecond)
	}

	return nil
}

// handleChatMessageChunk collects chunks and forwards re-assembled chat messages to the UI.
func (p *P2PProtocol) handleChatMessageChunk(msg *models.P2PMessage, addr *net.UDPAddr) error {
	payload := msg.Payload

	sessionID, ok := payload["session_id"].(string)
	if !ok {
		return fmt.Errorf("chat_message_chunk missing session_id")
	}

	// seq & total may arrive as float64 decoded from JSON numbers
	seqNumber := 0
	if seqRaw, ok := payload["seq"]; ok {
		switch v := seqRaw.(type) {
		case float64:
			seqNumber = int(v)
		case int:
			seqNumber = v
		}
	}

	totalChunks := 0
	if totalRaw, ok := payload["total"]; ok {
		switch v := totalRaw.(type) {
		case float64:
			totalChunks = int(v)
		case int:
			totalChunks = v
		}
	}

	chunkContent, ok := payload["content_chunk"].(string)
	if !ok {
		return fmt.Errorf("chat_message_chunk missing content_chunk")
	}

	p.chatMu.Lock()
	session, exists := p.chatReassemblySessions[sessionID]
	if !exists {
		session = &chatReassemblySession{
			totalChunks:  totalChunks,
			chunks:       make(map[int]string),
			fromWorkerID: msg.FromWorkerID,
			createdAt:    time.Now(),
		}
		p.chatReassemblySessions[sessionID] = session
	}

	session.chunks[seqNumber] = chunkContent

	// If we now have all chunks, reassemble
	if session.totalChunks > 0 && len(session.chunks) == session.totalChunks {
		var builder strings.Builder
		for i := 0; i < session.totalChunks; i++ {
			if part, ok := session.chunks[i]; ok {
				builder.WriteString(part)
			} else {
				// Missing chunk – should not happen, but bail out safely
				p.chatMu.Unlock()
				return fmt.Errorf("missing chunk %d in session %s", i, sessionID)
			}
		}

		fullContent := builder.String()
		// Clean up
		delete(p.chatReassemblySessions, sessionID)
		p.chatMu.Unlock()

		// Forward as regular chat message to UI for consistency
		fakeMsg := models.P2PMessage{
			Type:         "chat_message",
			FromWorkerID: session.fromWorkerID,
			Payload:      map[string]interface{}{"content": fullContent},
		}
		return p.handleChatMessage(&fakeMsg, addr)
	}

	p.chatMu.Unlock()
	return nil
}
