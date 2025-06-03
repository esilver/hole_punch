package models

import (
	"net"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

type WorkerInfo struct {
	ID                    string          `json:"id"`
	WebsocketObservedIP   string          `json:"websocket_observed_ip"`
	WebsocketObservedPort int             `json:"websocket_observed_port"`
	StunReportedUDPIP     string          `json:"stun_reported_udp_ip,omitempty"`
	StunReportedUDPPort   int             `json:"stun_reported_udp_port,omitempty"`
	HTTPReportedPublicIP  string          `json:"http_reported_public_ip,omitempty"`
	ConnectedAt           time.Time       `json:"connected_at"`
	LastPingAt            time.Time       `json:"last_ping_at"`
	Websocket             *websocket.Conn `json:"-"`
}

type ServiceState struct {
	ConnectedWorkers       map[string]*WorkerInfo
	WorkersReadyForPairing []string
	AdminWebsocketClients  []*websocket.Conn
	ChatSessions           map[string]map[string]*websocket.Conn
	Mu                     sync.RWMutex
}

func NewServiceState() *ServiceState {
	return &ServiceState{
		ConnectedWorkers:       make(map[string]*WorkerInfo),
		WorkersReadyForPairing: make([]string, 0),
		AdminWebsocketClients:  make([]*websocket.Conn, 0),
		ChatSessions:           make(map[string]map[string]*websocket.Conn),
	}
}

type WebSocketMessage struct {
	Type    string                 `json:"type"`
	Payload map[string]interface{} `json:"payload,omitempty"`
}

type P2PMessage struct {
	Type         string                 `json:"type"`
	FromWorkerID string                 `json:"from_worker_id"`
	Payload      map[string]interface{} `json:"payload,omitempty"`
	Timestamp    time.Time              `json:"timestamp"`
}

type WorkerState struct {
	WorkerID                 string
	OurStunDiscoveredUDPIP   string
	OurStunDiscoveredUDPPort int
	CurrentP2PPeerID         string
	CurrentP2PPeerAddr       *net.UDPAddr
	UIWebsocketClients       []*websocket.Conn
	BenchmarkSessions        map[string]*BenchmarkSession
	LastP2PMessageTime       time.Time
	P2PKeepAliveTask         chan struct{}
	Mu                       sync.RWMutex
}

type BenchmarkSession struct {
	StartTime      time.Time
	BytesReceived  int64
	ChunksReceived int
	Active         bool
}

func NewWorkerState(workerID string) *WorkerState {
	return &WorkerState{
		WorkerID:           workerID,
		UIWebsocketClients: make([]*websocket.Conn, 0),
		BenchmarkSessions:  make(map[string]*BenchmarkSession),
	}
}
