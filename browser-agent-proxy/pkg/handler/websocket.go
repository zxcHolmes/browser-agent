package handler

import (
	"net/http"
	"time"

	"browser-agent-proxy/pkg/relay"

	"github.com/gorilla/websocket"
	"github.com/sirupsen/logrus"
)

var upgrader = websocket.Upgrader{
	CheckOrigin: func(r *http.Request) bool {
		return true // Allow all origins for local development
	},
}

// WebSocketHandler handles WebSocket connections from the Chrome extension
type WebSocketHandler struct {
	hub *relay.Hub
	log *logrus.Logger
}

// NewWebSocketHandler creates a new WebSocket handler
func NewWebSocketHandler(hub *relay.Hub, log *logrus.Logger) *WebSocketHandler {
	return &WebSocketHandler{
		hub: hub,
		log: log,
	}
}

// ServeHTTP handles WebSocket upgrade and communication
func (h *WebSocketHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		h.log.WithError(err).Error("Failed to upgrade WebSocket connection")
		return
	}

	client := h.hub.RegisterClient(conn)
	defer h.hub.UnregisterClient(client)

	// Start write pump
	go h.writePump(client)

	// Read pump (blocking)
	h.readPump(client)
}

// readPump reads messages from the WebSocket connection
func (h *WebSocketHandler) readPump(client *relay.Client) {
	defer func() {
		client.Conn.Close()
		h.log.Info("WebSocket read pump stopped")
	}()

	// Increase read deadline to 5 minutes to keep connection alive longer
	readDeadline := 5 * time.Minute
	client.Conn.SetReadDeadline(time.Now().Add(readDeadline))

	client.Conn.SetPongHandler(func(string) error {
		h.log.Debug("Received pong from extension")
		client.Conn.SetReadDeadline(time.Now().Add(readDeadline))
		return nil
	})

	for {
		_, message, err := client.Conn.ReadMessage()
		if err != nil {
			if websocket.IsUnexpectedCloseError(err, websocket.CloseGoingAway, websocket.CloseAbnormalClosure) {
				h.log.WithError(err).Warn("WebSocket unexpected close")
			} else {
				h.log.WithError(err).Debug("WebSocket closed normally")
			}
			break
		}

		h.log.Debug("Received message from extension")
		if err := h.hub.HandleMessage(message); err != nil {
			h.log.WithError(err).Error("Failed to handle message")
		}
	}
}

// writePump writes messages to the WebSocket connection
func (h *WebSocketHandler) writePump(client *relay.Client) {
	// Send ping every 45 seconds (well before the 5 minute read deadline)
	ticker := time.NewTicker(45 * time.Second)
	defer func() {
		ticker.Stop()
		client.Conn.Close()
		h.log.Info("WebSocket write pump stopped")
	}()

	for {
		select {
		case message, ok := <-client.Send:
			client.Conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
			if !ok {
				h.log.Info("Send channel closed, closing connection")
				client.Conn.WriteMessage(websocket.CloseMessage, []byte{})
				return
			}

			if err := client.Conn.WriteMessage(websocket.TextMessage, message); err != nil {
				h.log.WithError(err).Error("WebSocket write error")
				return
			}
			h.log.Debug("Sent message to extension")

		case <-ticker.C:
			client.Conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
			if err := client.Conn.WriteMessage(websocket.PingMessage, nil); err != nil {
				h.log.WithError(err).Warn("Failed to send ping, closing connection")
				return
			}
			h.log.Debug("Sent ping to extension")
		}
	}
}
