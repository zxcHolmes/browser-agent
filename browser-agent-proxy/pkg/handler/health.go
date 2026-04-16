package handler

import (
	"encoding/json"
	"net/http"

	"github.com/sirupsen/logrus"
)

// HealthHandler handles health check requests
type HealthHandler struct {
	log *logrus.Logger
}

// NewHealthHandler creates a new health handler
func NewHealthHandler(log *logrus.Logger) *HealthHandler {
	return &HealthHandler{log: log}
}

// ServeHTTP handles health check requests
func (h *HealthHandler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodHead {
		w.WriteHeader(http.StatusOK)
		return
	}

	response := map[string]string{
		"status": "ok",
		"server": "Browser Agent Proxy",
	}

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(response)
}
