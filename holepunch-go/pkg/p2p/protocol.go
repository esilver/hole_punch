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
	"github.com/gorilla/websocket"
)

type P2PProtocol struct {
	conn            *net.UDPConn
	state           *models.WorkerState
	messageHandlers map[string]MessageHandler
	mu              sync.RWMutex
}

type MessageHandler func(msg *models.P2PMessage, addr *net.UDPAddr) error

func NewP2PProtocol(conn *net.UDPConn, state *models.WorkerState) *P2PProtocol {
	p := &P2PProtocol{
		conn:            conn,
		state:           state,
		messageHandlers: make(map[string]MessageHandler),
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
		"from_peer_id": msg.FromWorkerID,
		"content":      content,
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

	uiMsg := map[string]interface{}{
		"type": "pairing_test_result",
		"payload": map[string]interface{}{
			"test_id": msg.FromWorkerID,
			"rtt_ms":  int64(rttMs), // Store as int if Python UI expects int
			"success": true,
		},
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
			StartTime: time.Now(),
			Active:    true,
		}
		p.state.BenchmarkSessions[sessionID] = session
	}

	session.BytesReceived += int64(dataSize)
	session.ChunksReceived++
	p.state.Mu.Unlock()

	return nil
}

func (p *P2PProtocol) handleBenchmarkEnd(msg *models.P2PMessage, addr *net.UDPAddr) error {
	sessionID, ok := msg.Payload["session_id"].(string)
	if !ok {
		return fmt.Errorf("invalid benchmark end format")
	}

	p.state.Mu.Lock()
	session, exists := p.state.BenchmarkSessions[sessionID]
	if exists {
		session.Active = false
		duration := time.Since(session.StartTime).Seconds()
		throughput := float64(session.BytesReceived) / duration / 1024 / 1024

		log.Printf("Benchmark %s completed: %.2f MB in %.2fs (%.2f MB/s)",
			sessionID, float64(session.BytesReceived)/1024/1024, duration, throughput)

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
