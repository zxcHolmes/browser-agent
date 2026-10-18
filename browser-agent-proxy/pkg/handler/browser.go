package handler

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"

	"browser-agent-proxy/pkg/relay"
)

// BrowserHandler exposes the same semantic actions used by the extension Agent.
// The extension owns DOM details; Go validates requests and coordinates callers.
type BrowserHandler struct{ hub *relay.Hub }

func NewBrowserHandler(hub *relay.Hub) *BrowserHandler { return &BrowserHandler{hub: hub} }

var browserActions = map[string]bool{
	"snapshot": true, "click": true, "fill": true, "insert_text": true, "press": true,
	"select_option": true, "set_checked": true, "wait_for": true,
	"upload_files": true, "set_tab_policy": true, "list_frames": true, "handle_dialog": true,
}

func (h *BrowserHandler) Capabilities(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, 405, "method not allowed", "METHOD_NOT_ALLOWED")
		return
	}
	result, err := h.hub.SendContext(r.Context(), "browser.capabilities", nil, 10)
	if err != nil {
		writeError(w, 503, err.Error(), "CAPABILITIES_UNAVAILABLE")
		return
	}
	writeSuccess(w, result)
}

func (h *BrowserHandler) Action(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, 405, "method not allowed", "METHOD_NOT_ALLOWED")
		return
	}
	id, ok := extractTabID(w, r)
	if !ok {
		return
	}
	var body struct {
		Action         string                 `json:"action"`
		Args           map[string]interface{} `json:"args"`
		TimeoutSeconds int                    `json:"timeoutSeconds"`
	}
	r.Body = http.MaxBytesReader(w, r.Body, 2<<20)
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeError(w, 400, "invalid request body", "INVALID_REQUEST")
		return
	}
	if !browserActions[body.Action] {
		writeError(w, 400, "unknown browser action", "INVALID_ACTION")
		return
	}
	if body.Args == nil {
		body.Args = map[string]interface{}{}
	}
	if body.TimeoutSeconds == 0 {
		body.TimeoutSeconds = 40
	}
	if body.TimeoutSeconds < 1 || body.TimeoutSeconds > 330 {
		writeError(w, 400, "timeoutSeconds must be 1..330", "INVALID_TIMEOUT")
		return
	}
	// Bound the extension wait to the caller's deadline so it does not keep the
	// per-tab queue occupied after the HTTP request times out.
	if body.Action == "wait_for" {
		seconds := 30.0
		if v, exists := body.Args["timeout_seconds"]; exists {
			var valid bool
			seconds, valid = v.(float64)
			if !valid || seconds <= 0 || seconds > 300 {
				writeError(w, 400, "timeout_seconds must be 0..300", "INVALID_TIMEOUT")
				return
			}
		}
		if float64(body.TimeoutSeconds) <= seconds {
			writeError(w, 400, "timeoutSeconds must exceed wait timeout_seconds", "INVALID_TIMEOUT")
			return
		}
	}
	if body.Action == "upload_files" {
		if err := validateUploadFiles(body.Args); err != nil {
			writeError(w, 400, err.Error(), "INVALID_UPLOAD_FILES")
			return
		}
	}
	result, err := h.hub.SendContext(r.Context(), "browser.action", map[string]interface{}{
		"tabId": id, "action": body.Action, "args": body.Args,
	}, body.TimeoutSeconds)
	if err != nil {
		status, code := http.StatusUnprocessableEntity, "BROWSER_ACTION_FAILED"
		if !h.hub.IsConnected() {
			status, code = 503, "EXTENSION_NOT_CONNECTED"
		}
		if isNotFoundError(err) {
			status, code = 404, "TAB_NOT_FOUND"
		}
		writeError(w, status, err.Error(), code)
		return
	}
	value, ok := result["result"].(map[string]interface{})
	if !ok {
		writeError(w, 502, "invalid browser action response", "INVALID_EXTENSION_RESPONSE")
		return
	}
	writeSuccess(w, value)
}

func validateUploadFiles(args map[string]interface{}) error {
	files, ok := args["files"].([]interface{})
	if !ok || len(files) == 0 || len(files) > 100 {
		return fmt.Errorf("files must contain 1..100 absolute local file paths")
	}
	for _, v := range files {
		path, ok := v.(string)
		if !ok || !filepath.IsAbs(path) {
			return fmt.Errorf("file paths must be absolute")
		}
		info, err := os.Stat(path)
		if err != nil {
			return fmt.Errorf("file unavailable: %s: %w", path, err)
		}
		if !info.Mode().IsRegular() {
			return fmt.Errorf("not a regular file: %s", path)
		}
		f, err := os.Open(path)
		if err != nil {
			return fmt.Errorf("file unreadable: %w", err)
		}
		f.Close()
	}
	return nil
}
