package relay

import (
	"context"
	"encoding/json"
	"fmt"
	"sync"
	"time"

	"browser-agent-proxy/pkg/events"

	"github.com/gorilla/websocket"
	"github.com/sirupsen/logrus"
)

// Hub manages the WebSocket connection to the Chrome extension and
// routes commands / events between the REST API layer and the extension.
type Hub struct {
	log        *logrus.Logger
	events     *events.Store
	mu         sync.RWMutex
	client     *Client
	pendingReq map[int]*pendingRequest
	nextID     int
}

// Client holds the active WebSocket connection from the extension.
type Client struct {
	Conn     *websocket.Conn
	Send     chan []byte
	Done     chan struct{}
	stopOnce sync.Once
}

func (c *Client) stop() {
	c.stopOnce.Do(func() {
		close(c.Done)
		if c.Conn != nil {
			c.Conn.Close()
		}
	})
}

// pendingRequest tracks an in-flight command waiting for a response.
type pendingRequest struct {
	id       int
	response chan *extensionResponse
	client   *Client
}

// extensionResponse is the decoded reply from the extension.
type extensionResponse struct {
	Result map[string]interface{}
	Error  string
}

// NewHub creates a Hub. The caller should call Run() before using it.
func NewHub(log *logrus.Logger, eventStore *events.Store) *Hub {
	return &Hub{
		log:        log,
		events:     eventStore,
		pendingReq: make(map[int]*pendingRequest),
		nextID:     1,
	}
}

// ─── Client registration ───────────────────────────────────────────────────────

// RegisterClient registers the extension WebSocket connection.
// Any previous connection is closed first.
func (h *Hub) RegisterClient(conn *websocket.Conn) *Client {
	c := &Client{Conn: conn, Send: make(chan []byte, 256), Done: make(chan struct{})}
	h.mu.Lock()
	old := h.client
	h.client = c
	h.mu.Unlock()
	if old != nil {
		old.stop()
	}
	h.log.Info("[Hub] Chrome extension client registered")
	return c
}

// The send queue is never closed: concurrent senders select on Done instead.
func (h *Hub) UnregisterClient(c *Client) {
	h.mu.Lock()
	if h.client == c {
		h.client = nil
	}
	h.mu.Unlock()
	c.stop()
	h.log.Info("[Hub] Chrome extension client unregistered")
}

// IsConnected returns true when an extension client is connected.
func (h *Hub) IsConnected() bool {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return h.client != nil
}

// ─── Send command to extension ─────────────────────────────────────────────────

// Send sends a command to the extension and blocks until a response arrives
// or the timeout elapses.
func (h *Hub) Send(method string, params map[string]interface{}, timeoutSec int) (map[string]interface{}, error) {
	return h.SendContext(context.Background(), method, params, timeoutSec)
}

// SendContext never retries a dispatched command: on timeout its outcome is unknown.
func (h *Hub) SendContext(ctx context.Context, method string, params map[string]interface{}, timeoutSec int) (map[string]interface{}, error) {
	if timeoutSec <= 0 {
		timeoutSec = 30
	}
	ctx, cancel := context.WithTimeout(ctx, time.Duration(timeoutSec)*time.Second)
	defer cancel()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	h.mu.Lock()
	client := h.client
	if client == nil {
		h.mu.Unlock()
		return nil, fmt.Errorf("chrome extension not connected")
	}
	id := h.nextID
	h.nextID++
	req := &pendingRequest{id: id, response: make(chan *extensionResponse, 1), client: client}
	h.pendingReq[id] = req
	h.mu.Unlock()
	defer func() { h.mu.Lock(); delete(h.pendingReq, id); h.mu.Unlock() }()
	msg, err := json.Marshal(map[string]interface{}{"id": id, "method": method, "params": params})
	if err != nil {
		return nil, fmt.Errorf("marshal command: %w", err)
	}
	queueTimer := time.NewTimer(5 * time.Second)
	defer queueTimer.Stop()
	select {
	case <-client.Done:
		return nil, fmt.Errorf("extension disconnected before dispatch")
	case <-ctx.Done():
		return nil, ctx.Err()
	default:
	}
	select {
	case client.Send <- msg:
	case <-client.Done:
		return nil, fmt.Errorf("extension disconnected before dispatch")
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-queueTimer.C:
		return nil, fmt.Errorf("send queue full")
	}
	select {
	case resp := <-req.response:
		if resp.Error != "" {
			return nil, fmt.Errorf("%s", resp.Error)
		}
		return resp.Result, nil
	case <-client.Done:
		return nil, fmt.Errorf("extension disconnected; execution outcome unknown")
	case <-ctx.Done():
		// Best effort cancellation stops waits and prevents later tool steps;
		// an already dispatched browser input may still have taken effect.
		cancelMsg, _ := json.Marshal(map[string]interface{}{"method": "browser.cancel", "params": map[string]interface{}{"requestId": id}})
		select {
		case client.Send <- cancelMsg:
		default:
		}
		return nil, fmt.Errorf("command interrupted; execution outcome unknown: %w", ctx.Err())
	}
}

// ─── Incoming messages from extension ─────────────────────────────────────────

// HandleMessage processes a raw WebSocket message from the extension.
func (h *Hub) HandleMessage(data []byte) error {
	return h.HandleClientMessage(nil, data)
}

// Bind replies and events to the connection that sent them.
func (h *Hub) HandleClientMessage(client *Client, data []byte) error {
	if client != nil {
		h.mu.RLock()
		active := h.client == client
		h.mu.RUnlock()
		if !active {
			return nil
		}
	}
	var raw map[string]interface{}
	if err := json.Unmarshal(data, &raw); err != nil {
		return fmt.Errorf("unmarshal: %w", err)
	}

	method, _ := raw["method"].(string)

	// pong reply to our ping
	if method == "pong" {
		h.log.Debug("[Hub] Pong received")
		return nil
	}

	// event pushed from extension: { method:"event", params:{...} }
	if method == "event" {
		return h.handleEvent(raw)
	}

	// response to a command we sent: { id, result } or { id, error }
	if idFloat, ok := raw["id"].(float64); ok {
		return h.handleResponse(client, int(idFloat), raw)
	}

	h.log.WithField("method", method).Debug("[Hub] Unhandled message")
	return nil
}

func (h *Hub) handleResponse(client *Client, id int, raw map[string]interface{}) error {
	h.mu.Lock()
	req, ok := h.pendingReq[id]
	if ok && client != nil && req.client != client {
		ok = false
	}
	if ok {
		delete(h.pendingReq, id)
	}
	h.mu.Unlock()

	if !ok {
		h.log.WithField("id", id).Warn("[Hub] Response for unknown request id")
		return nil
	}

	resp := &extensionResponse{}
	if result, ok := raw["result"].(map[string]interface{}); ok {
		resp.Result = result
	}
	if errMsg, ok := raw["error"].(string); ok {
		resp.Error = errMsg
	}

	h.log.WithField("id", id).Debug("[Hub] Response received")
	req.response <- resp
	return nil
}

func (h *Hub) handleEvent(raw map[string]interface{}) error {
	params, _ := raw["params"].(map[string]interface{})
	if params == nil {
		return nil
	}

	tabIDFloat, _ := params["tabId"].(float64)
	tabID := int(tabIDFloat)
	eventMethod, _ := params["eventMethod"].(string)
	eventParams, _ := params["eventParams"].(map[string]interface{})
	tsFloat, _ := params["timestamp"].(float64)
	sessionID, _ := params["sessionId"].(string)

	ev := &events.Event{
		TabID:       tabID,
		SessionID:   sessionID,
		EventMethod: eventMethod,
		EventParams: eventParams,
		Timestamp:   int64(tsFloat),
		ReceivedAt:  time.Now(),
	}
	h.events.Append(ev)

	h.log.WithFields(logrus.Fields{
		"tabId": tabID,
		"event": eventMethod,
	}).Debug("[Hub] Event stored")
	return nil
}
