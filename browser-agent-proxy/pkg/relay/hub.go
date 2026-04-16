package relay

import (
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
	Conn *websocket.Conn
	Send chan []byte
}

// pendingRequest tracks an in-flight command waiting for a response.
type pendingRequest struct {
	id       int
	response chan *extensionResponse
	timer    *time.Timer
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
	h.mu.Lock()
	defer h.mu.Unlock()

	if h.client != nil {
		h.log.Warn("[Hub] Replacing existing extension client")
		h.client.Conn.Close()
	}

	c := &Client{
		Conn: conn,
		Send: make(chan []byte, 256),
	}
	h.client = c
	h.log.Info("[Hub] Chrome extension client registered")
	return c
}

// UnregisterClient removes the client if it matches the registered one.
func (h *Hub) UnregisterClient(c *Client) {
	h.mu.Lock()
	defer h.mu.Unlock()

	if h.client != c {
		return
	}
	h.client = nil
	close(c.Send)
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
	h.mu.Lock()
	if h.client == nil {
		h.mu.Unlock()
		return nil, fmt.Errorf("chrome extension not connected")
	}

	id := h.nextID
	h.nextID++

	respCh := make(chan *extensionResponse, 1)
	timer := time.AfterFunc(time.Duration(timeoutSec)*time.Second, func() {
		h.mu.Lock()
		if req, ok := h.pendingReq[id]; ok {
			delete(h.pendingReq, id)
			h.mu.Unlock()
			req.response <- &extensionResponse{Error: "command timeout"}
		} else {
			h.mu.Unlock()
		}
	})

	h.pendingReq[id] = &pendingRequest{id: id, response: respCh, timer: timer}

	msg, err := json.Marshal(map[string]interface{}{
		"id":     id,
		"method": method,
		"params": params,
	})
	if err != nil {
		delete(h.pendingReq, id)
		h.mu.Unlock()
		timer.Stop()
		return nil, fmt.Errorf("marshal command: %w", err)
	}

	client := h.client
	h.mu.Unlock()

	select {
	case client.Send <- msg:
	case <-time.After(5 * time.Second):
		h.mu.Lock()
		delete(h.pendingReq, id)
		h.mu.Unlock()
		timer.Stop()
		return nil, fmt.Errorf("send queue full or client gone")
	}

	h.log.WithFields(logrus.Fields{"id": id, "method": method}).Debug("[Hub] Command sent")

	resp := <-respCh
	if resp == nil {
		return nil, fmt.Errorf("response channel closed")
	}
	if resp.Error != "" {
		return nil, fmt.Errorf("%s", resp.Error)
	}
	return resp.Result, nil
}

// ─── Incoming messages from extension ─────────────────────────────────────────

// HandleMessage processes a raw WebSocket message from the extension.
func (h *Hub) HandleMessage(data []byte) error {
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
		return h.handleResponse(int(idFloat), raw)
	}

	h.log.WithField("method", method).Debug("[Hub] Unhandled message")
	return nil
}

func (h *Hub) handleResponse(id int, raw map[string]interface{}) error {
	h.mu.Lock()
	req, ok := h.pendingReq[id]
	if ok {
		delete(h.pendingReq, id)
		req.timer.Stop()
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

	ev := &events.Event{
		TabID:       tabID,
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
