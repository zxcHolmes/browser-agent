package handler

import (
	"encoding/json"
	"net/http"
	"strconv"
	"strings"

	"browser-agent-proxy/pkg/relay"

	"github.com/sirupsen/logrus"
)

// TabHandler handles Tab CRUD API requests.
type TabHandler struct {
	hub *relay.Hub
	log *logrus.Logger
}

func NewTabHandler(hub *relay.Hub, log *logrus.Logger) *TabHandler {
	return &TabHandler{hub: hub, log: log}
}

// CreateTab POST /api/tabs
func (h *TabHandler) CreateTab(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed", "METHOD_NOT_ALLOWED")
		return
	}
	if !h.hub.IsConnected() {
		writeError(w, http.StatusServiceUnavailable, "chrome extension not connected", "EXTENSION_NOT_CONNECTED")
		return
	}

	var body struct {
		URL    string                 `json:"url"`
		Active bool                   `json:"active"`
		Meta   map[string]interface{} `json:"meta"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body", "INVALID_REQUEST")
		return
	}
	if body.URL == "" {
		body.URL = "about:blank"
	}

	params := map[string]interface{}{
		"url":    body.URL,
		"active": body.Active,
		"meta":   body.Meta,
	}

	result, err := h.hub.Send("tab.create", params, 30)
	if err != nil {
		h.log.WithError(err).Error("[Tab] Create tab failed")
		writeError(w, http.StatusInternalServerError, err.Error(), "CREATE_TAB_FAILED")
		return
	}

	h.log.WithField("url", body.URL).Info("[Tab] Tab created")
	writeSuccess(w, result)
}

// ListTabs GET /api/tabs
func (h *TabHandler) ListTabs(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed", "METHOD_NOT_ALLOWED")
		return
	}
	if !h.hub.IsConnected() {
		writeError(w, http.StatusServiceUnavailable, "chrome extension not connected", "EXTENSION_NOT_CONNECTED")
		return
	}

	result, err := h.hub.Send("tab.list", map[string]interface{}{}, 10)
	if err != nil {
		h.log.WithError(err).Error("[Tab] List tabs failed")
		writeError(w, http.StatusInternalServerError, err.Error(), "LIST_TABS_FAILED")
		return
	}

	writeSuccess(w, result)
}

// GetTab GET /api/tabs/{tabId}
func (h *TabHandler) GetTab(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed", "METHOD_NOT_ALLOWED")
		return
	}

	tabID, ok := extractTabID(w, r)
	if !ok {
		return
	}

	if !h.hub.IsConnected() {
		writeError(w, http.StatusServiceUnavailable, "chrome extension not connected", "EXTENSION_NOT_CONNECTED")
		return
	}

	result, err := h.hub.Send("tab.get", map[string]interface{}{"tabId": tabID}, 10)
	if err != nil {
		if isNotFoundError(err) {
			writeError(w, http.StatusNotFound, err.Error(), "TAB_NOT_FOUND")
			return
		}
		h.log.WithError(err).Error("[Tab] Get tab failed")
		writeError(w, http.StatusInternalServerError, err.Error(), "GET_TAB_FAILED")
		return
	}

	writeSuccess(w, result)
}

// DeleteTab DELETE /api/tabs/{tabId}
func (h *TabHandler) DeleteTab(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodDelete {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed", "METHOD_NOT_ALLOWED")
		return
	}

	tabID, ok := extractTabID(w, r)
	if !ok {
		return
	}

	if !h.hub.IsConnected() {
		writeError(w, http.StatusServiceUnavailable, "chrome extension not connected", "EXTENSION_NOT_CONNECTED")
		return
	}

	result, err := h.hub.Send("tab.close", map[string]interface{}{"tabId": tabID}, 10)
	if err != nil {
		if isNotFoundError(err) {
			writeError(w, http.StatusNotFound, err.Error(), "TAB_NOT_FOUND")
			return
		}
		h.log.WithError(err).Error("[Tab] Close tab failed")
		writeError(w, http.StatusInternalServerError, err.Error(), "CLOSE_TAB_FAILED")
		return
	}

	h.log.WithField("tabId", tabID).Info("[Tab] Tab closed via API")
	writeSuccess(w, result)
}

// ─── path helpers ──────────────────────────────────────────────────────────────

func extractTabID(w http.ResponseWriter, r *http.Request) (int, bool) {
	parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
	if len(parts) < 3 {
		writeError(w, http.StatusBadRequest, "missing tabId in path", "INVALID_REQUEST")
		return 0, false
	}
	id, err := strconv.Atoi(parts[2])
	if err != nil {
		writeError(w, http.StatusBadRequest, "invalid tabId", "INVALID_TAB_ID")
		return 0, false
	}
	return id, true
}

func isNotFoundError(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	return strings.Contains(msg, "not found") || strings.Contains(msg, "not managed")
}
