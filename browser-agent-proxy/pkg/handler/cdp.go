package handler

import (
	"encoding/json"
	"net/http"
	"strings"

	"browser-agent-proxy/pkg/relay"

	"github.com/sirupsen/logrus"
)

// CDPHandler handles CDP command forwarding.
type CDPHandler struct {
	hub *relay.Hub
	log *logrus.Logger
}

func NewCDPHandler(hub *relay.Hub, log *logrus.Logger) *CDPHandler {
	return &CDPHandler{hub: hub, log: log}
}

// Send POST /api/cdp
func (h *CDPHandler) Send(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed", "METHOD_NOT_ALLOWED")
		return
	}
	if !h.hub.IsConnected() {
		writeError(w, http.StatusServiceUnavailable, "chrome extension not connected", "EXTENSION_NOT_CONNECTED")
		return
	}

	var body struct {
		TabID   int                    `json:"tabId"`
		Method  string                 `json:"method"`
		Params  map[string]interface{} `json:"params"`
		Timeout int                    `json:"timeout"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body", "INVALID_REQUEST")
		return
	}
	if body.TabID == 0 {
		writeError(w, http.StatusBadRequest, "tabId is required", "TAB_ID_REQUIRED")
		return
	}
	if body.Method == "" {
		writeError(w, http.StatusBadRequest, "method is required", "METHOD_REQUIRED")
		return
	}
	if body.Timeout <= 0 {
		body.Timeout = 30
	}

	params := map[string]interface{}{
		"tabId":  body.TabID,
		"method": body.Method,
		"params": body.Params,
	}

	h.log.WithFields(logrus.Fields{
		"tabId":  body.TabID,
		"method": body.Method,
	}).Debug("[CDP] Forwarding command")

	result, err := h.hub.Send("cdp.send", params, body.Timeout)
	if err != nil {
		if isTabClosedError(err) {
			writeError(w, http.StatusNotFound, "tab was closed or not found: "+err.Error(), "TAB_CLOSED")
			return
		}
		if isNotFoundError(err) {
			writeError(w, http.StatusNotFound, err.Error(), "TAB_NOT_FOUND")
			return
		}
		h.log.WithError(err).Error("[CDP] Command failed")
		writeError(w, http.StatusInternalServerError, err.Error(), "CDP_COMMAND_FAILED")
		return
	}

	writeSuccess(w, result)
}

func isTabClosedError(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	return strings.Contains(msg, "target closed") ||
		strings.Contains(msg, "detached") ||
		strings.Contains(msg, "No such target")
}
