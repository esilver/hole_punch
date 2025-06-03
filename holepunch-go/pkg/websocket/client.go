package websocket

import (
	"fmt"
	"log"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

type Client struct {
	conn            *websocket.Conn
	url             string
	messageHandlers map[string]MessageHandler
	mu              sync.RWMutex
	pingInterval    time.Duration
	pingTimeout     time.Duration
	done            chan struct{}
	closeOnce       sync.Once
}

type MessageHandler func(payload map[string]interface{}) error

type Message struct {
	Type    string                 `json:"type"`
	Payload map[string]interface{} `json:"payload,omitempty"`
}

func NewClient(serverURL string) *Client {
	return &Client{
		url:             serverURL,
		messageHandlers: make(map[string]MessageHandler),
		pingInterval:    30 * time.Second,
		pingTimeout:     60 * time.Second,
		done:            make(chan struct{}),
	}
}

func (c *Client) RegisterHandler(msgType string, handler MessageHandler) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.messageHandlers[msgType] = handler
}

func (c *Client) Connect() error {
	log.Printf("WebSocket client attempting to connect to (raw input URL): %s", c.url)

	u, err := url.Parse(c.url)
	if err != nil {
		return fmt.Errorf("invalid URL structure for '%s': %w", c.url, err)
	}

	// Trim potential whitespace around the host.
	// url.Parse("http:// host.com /path") correctly sets u.Scheme and u.Path,
	// but u.Host becomes " host.com ". This can make it a malformed URL for the websocket dialer.
	if u.Host != "" { // Only trim if host was initially parsed
		u.Host = strings.TrimSpace(u.Host)
		if u.Host == "" {
			// If trimming an existing host string made it empty (e.g., c.url was "http:// /path")
			return fmt.Errorf("host part of URL '%s' became empty after trimming whitespace", c.url)
		}
	} else {
		// Host was empty after initial parsing.
		// This is an issue if a scheme is present (e.g. "http:///path")
		// or if no scheme and no host means it's just a path.
		// If scheme is empty AND host is empty, it might be a relative path, which is not valid for Dial.
		if u.Scheme != "" {
			return fmt.Errorf("URL '%s' has a scheme ('%s') but no host component", c.url, u.Scheme)
		}
		// If scheme is also empty, url.Parse might have put everything in Path.
		// Example: "myhost.com/ws/register" -> Scheme="", Host="", Path="myhost.com/ws/register"
		// This situation will be caught by the "unsupported scheme" error later if not handled,
		// or it means the URL is fundamentally not an absolute URL suitable for dialing.
		// For now, we rely on the scheme check below. A schemeless, hostless URL won't form a valid ws:// URL.
	}

	// Convert http/https to ws/wss
	originalScheme := u.Scheme // For logging/error messages
	switch u.Scheme {
	case "http":
		u.Scheme = "ws"
	case "https":
		u.Scheme = "wss"
	case "ws", "wss":
		// Already correct
	default:
		// This typically means no scheme was provided (e.g. "myhost.com/path")
		// or an actual unsupported scheme.
		return fmt.Errorf("unsupported or missing scheme ('%s') in URL '%s'. A scheme like http, https, ws, or wss is required.", originalScheme, c.url)
	}

	finalURLString := u.String()
	// After scheme conversion and host trimming, re-check host, as u.String() might behave unexpectedly if host is truly empty.
	// For example, if u was originally http:///foo -> ws:///foo. u.Host is still empty.
	if u.Host == "" {
		return fmt.Errorf("URL '%s' resulted in an empty host for the final WebSocket URL '%s'", c.url, finalURLString)
	}

	log.Printf("WebSocket client connecting to (processed URL): %s", finalURLString)

	header := http.Header{}
	header.Add("User-Agent", "HolePunch-Worker/1.0")

	dialer := websocket.Dialer{
		HandshakeTimeout: 10 * time.Second,
		Proxy:            nil,
		NetDialContext:   nil,
	}

	conn, _, err := dialer.Dial(u.String(), header)
	if err != nil {
		return fmt.Errorf("failed to connect: %w", err)
	}

	c.conn = conn
	c.conn.SetReadDeadline(time.Now().Add(c.pingTimeout))
	c.conn.SetPongHandler(func(string) error {
		c.conn.SetReadDeadline(time.Now().Add(c.pingTimeout))
		return nil
	})

	go c.readLoop()
	go c.pingLoop()

	return nil
}

func (c *Client) Close() error {
	var err error
	c.closeOnce.Do(func() {
		close(c.done)
		if c.conn != nil {
			err = c.conn.Close()
		}
	})
	return err
}

func (c *Client) SendMessage(msgType string, payload map[string]interface{}) error {
	msg := Message{
		Type:    msgType,
		Payload: payload,
	}

	c.mu.Lock()
	defer c.mu.Unlock()

	if c.conn == nil {
		return fmt.Errorf("not connected")
	}

	return c.conn.WriteJSON(msg)
}

func (c *Client) readLoop() {
	defer c.Close()

	for {
		var msg Message
		if err := c.conn.ReadJSON(&msg); err != nil {
			if websocket.IsUnexpectedCloseError(err, websocket.CloseGoingAway, websocket.CloseAbnormalClosure) {
				log.Printf("WebSocket read error: %v", err)
			}
			return
		}

		c.mu.RLock()
		handler, ok := c.messageHandlers[msg.Type]
		c.mu.RUnlock()

		if ok {
			if err := handler(msg.Payload); err != nil {
				log.Printf("Error handling %s message: %v", msg.Type, err)
			}
		} else {
			log.Printf("No handler for message type: %s", msg.Type)
		}
	}
}

func (c *Client) pingLoop() {
	ticker := time.NewTicker(c.pingInterval)
	defer ticker.Stop()

	for {
		select {
		case <-ticker.C:
			c.mu.Lock()
			if c.conn == nil {
				c.mu.Unlock()
				return
			}
			c.conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
			if err := c.conn.WriteMessage(websocket.PingMessage, nil); err != nil {
				c.mu.Unlock()
				log.Printf("Ping failed: %v", err)
				c.Close()
				return
			}
			c.mu.Unlock()
		case <-c.done:
			return
		}
	}
}
