package handler

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"browser-agent-proxy/pkg/events"
	"browser-agent-proxy/pkg/relay"

	"github.com/sirupsen/logrus"
)

// ActionsHandler handles high-level browser action APIs.
type ActionsHandler struct {
	hub        *relay.Hub
	eventStore *events.Store
	log        *logrus.Logger
}

func NewActionsHandler(hub *relay.Hub, eventStore *events.Store, log *logrus.Logger) *ActionsHandler {
	return &ActionsHandler{hub: hub, eventStore: eventStore, log: log}
}

// Navigate POST /api/tabs/{tabId}/navigate
// Sends Page.navigate then polls for Page.loadEventFired before returning.
func (h *ActionsHandler) Navigate(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
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

	var body struct {
		URL            string `json:"url"`
		WaitForLoad    *bool  `json:"waitForLoad"`    // default true
		TimeoutSeconds int    `json:"timeoutSeconds"` // default 30
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body", "INVALID_REQUEST")
		return
	}
	if body.URL == "" {
		writeError(w, http.StatusBadRequest, "url is required", "URL_REQUIRED")
		return
	}

	waitForLoad := true
	if body.WaitForLoad != nil {
		waitForLoad = *body.WaitForLoad
	}
	timeoutSec := 30
	if body.TimeoutSeconds > 0 {
		timeoutSec = body.TimeoutSeconds
	}

	// Record time before sending navigate so we don't miss events that arrive fast
	beforeNavigate := time.Now()

	h.log.WithFields(logrus.Fields{"tabId": tabID, "url": body.URL}).Debug("[Actions] Navigating tab")

	result, err := h.hub.Send("cdp.send", map[string]interface{}{
		"tabId":  tabID,
		"method": "Page.navigate",
		"params": map[string]interface{}{"url": body.URL},
	}, timeoutSec)
	if err != nil {
		if isTabClosedError(err) {
			writeError(w, http.StatusNotFound, "tab was closed: "+err.Error(), "TAB_CLOSED")
			return
		}
		if isNotFoundError(err) {
			writeError(w, http.StatusNotFound, err.Error(), "TAB_NOT_FOUND")
			return
		}
		h.log.WithError(err).Error("[Actions] Navigate failed")
		writeError(w, http.StatusInternalServerError, err.Error(), "NAVIGATE_FAILED")
		return
	}

	// Unwrap CDP response: { result: { frameId, loaderId, ... } }
	navResult, _ := result["result"].(map[string]interface{})
	if navResult == nil {
		navResult = map[string]interface{}{}
	}

	if !waitForLoad {
		navResult["loadEventFired"] = false
		writeSuccess(w, navResult)
		return
	}

	// Poll event store for Page.loadEventFired
	deadline := time.Now().Add(time.Duration(timeoutSec) * time.Second)
	for time.Now().Before(deadline) {
		evs := h.eventStore.Query(events.QueryFilter{
			TabID:   tabID,
			Since:   beforeNavigate,
			Methods: []string{"Page.loadEventFired"},
		})
		if len(evs) > 0 {
			h.log.WithField("tabId", tabID).Debug("[Actions] Page load confirmed")
			navResult["loadEventFired"] = true
			writeSuccess(w, navResult)
			return
		}
		time.Sleep(200 * time.Millisecond)
	}

	// Timed out waiting for load — still return navigate result with a flag
	navResult["loadEventFired"] = false
	writeSuccess(w, navResult)
}

// Screenshot GET /api/tabs/{tabId}/screenshot
// Returns the screenshot as image/png directly.
func (h *ActionsHandler) Screenshot(w http.ResponseWriter, r *http.Request) {
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

	// Optional: ?format=jpeg (default png)
	format := r.URL.Query().Get("format")
	if format != "jpeg" {
		format = "png"
	}

	params := map[string]interface{}{"format": format}
	if format == "jpeg" {
		qualityStr := r.URL.Query().Get("quality")
		quality := 80
		if qualityStr != "" {
			if _, err := fmt.Sscanf(qualityStr, "%d", &quality); err != nil || quality < 1 || quality > 100 {
				quality = 80
			}
		}
		params["quality"] = quality
	}

	h.log.WithFields(logrus.Fields{"tabId": tabID, "format": format}).Debug("[Actions] Taking screenshot")

	result, err := h.hub.Send("cdp.send", map[string]interface{}{
		"tabId":  tabID,
		"method": "Page.captureScreenshot",
		"params": params,
	}, 30)
	if err != nil {
		if isTabClosedError(err) {
			writeError(w, http.StatusNotFound, "tab was closed: "+err.Error(), "TAB_CLOSED")
			return
		}
		if isNotFoundError(err) {
			writeError(w, http.StatusNotFound, err.Error(), "TAB_NOT_FOUND")
			return
		}
		h.log.WithError(err).Error("[Actions] Screenshot failed")
		writeError(w, http.StatusInternalServerError, err.Error(), "SCREENSHOT_FAILED")
		return
	}

	inner, _ := result["result"].(map[string]interface{})
	if inner == nil {
		inner = result
	}
	dataB64, ok := inner["data"].(string)
	if !ok || dataB64 == "" {
		writeError(w, http.StatusInternalServerError, "screenshot returned no data", "SCREENSHOT_FAILED")
		return
	}

	imgBytes, err := base64.StdEncoding.DecodeString(dataB64)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to decode screenshot data", "SCREENSHOT_FAILED")
		return
	}

	contentType := "image/png"
	if format == "jpeg" {
		contentType = "image/jpeg"
	}
	w.Header().Set("Content-Type", contentType)
	w.WriteHeader(http.StatusOK)
	w.Write(imgBytes)
}

// Eval POST /api/tabs/{tabId}/eval
// Executes JavaScript in the tab and returns the result value.
func (h *ActionsHandler) Eval(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
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

	var body struct {
		Expression     string `json:"expression"`
		AwaitPromise   bool   `json:"awaitPromise"`
		TimeoutSeconds int    `json:"timeoutSeconds"` // default 30
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body", "INVALID_REQUEST")
		return
	}
	if body.Expression == "" {
		writeError(w, http.StatusBadRequest, "expression is required", "EXPRESSION_REQUIRED")
		return
	}
	timeoutSec := 30
	if body.TimeoutSeconds > 0 {
		timeoutSec = body.TimeoutSeconds
	}

	h.log.WithField("tabId", tabID).Debug("[Actions] Evaluating expression")

	result, err := h.hub.Send("cdp.send", map[string]interface{}{
		"tabId":  tabID,
		"method": "Runtime.evaluate",
		"params": map[string]interface{}{
			"expression":    body.Expression,
			"returnByValue": true,
			"awaitPromise":  body.AwaitPromise,
		},
	}, timeoutSec)
	if err != nil {
		if isTabClosedError(err) {
			writeError(w, http.StatusNotFound, "tab was closed: "+err.Error(), "TAB_CLOSED")
			return
		}
		if isNotFoundError(err) {
			writeError(w, http.StatusNotFound, err.Error(), "TAB_NOT_FOUND")
			return
		}
		h.log.WithError(err).Error("[Actions] Eval failed")
		writeError(w, http.StatusInternalServerError, err.Error(), "EVAL_FAILED")
		return
	}

	// hub.Send returns the extension's result field, which for cdp.send is:
	// { result: { result: { type, value }, exceptionDetails? } }
	// We unwrap both layers to get { type, value } directly.
	response := map[string]interface{}{}

	cdpResult, _ := result["result"].(map[string]interface{})
	inner, _ := cdpResult["result"].(map[string]interface{})
	if inner != nil {
		response["value"] = inner["value"]
		response["type"] = inner["type"]
		if subtype, ok := inner["subtype"]; ok {
			response["subtype"] = subtype
		}
	}

	exDetails := cdpResult["exceptionDetails"]
	if exDetails != nil {
		response["exception"] = exDetails
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusUnprocessableEntity)
		json.NewEncoder(w).Encode(map[string]interface{}{
			"success": false,
			"error":   "javascript exception",
			"code":    "JS_EXCEPTION",
			"data":    response,
		})
		return
	}

	writeSuccess(w, response)
}
