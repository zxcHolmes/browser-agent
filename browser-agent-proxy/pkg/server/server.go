package server

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"

	"browser-agent-proxy/pkg/config"
	"browser-agent-proxy/pkg/events"
	"browser-agent-proxy/pkg/handler"
	"browser-agent-proxy/pkg/relay"

	"github.com/sirupsen/logrus"
)

// Server is the Browser Agent Proxy HTTP server.
type Server struct {
	cfg        *config.Config
	log        *logrus.Logger
	httpServer *http.Server
	hub        *relay.Hub
}

// NewServer wires up all dependencies and registers routes.
func NewServer(cfg *config.Config, log *logrus.Logger) *Server {
	eventStore := events.NewStore()
	hub := relay.NewHub(log, eventStore)

	mux := http.NewServeMux()

	// Health / extension WebSocket
	mux.HandleFunc("/", handler.NewHealthHandler(log).ServeHTTP)
	mux.HandleFunc("/extension", handler.NewWebSocketHandler(hub, log).ServeHTTP)

	// Tab CRUD
	tabH := handler.NewTabHandler(hub, log)
	mux.HandleFunc("/api/tabs", func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodPost:
			tabH.CreateTab(w, r)
		case http.MethodGet:
			tabH.ListTabs(w, r)
		default:
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		}
	})

	// High-level action APIs (must be registered before the wildcard /api/tabs/ route)
	actionsH := handler.NewActionsHandler(hub, eventStore, log)
	mux.HandleFunc("/api/tabs/", func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		switch {
		case hasSuffix(path, "/navigate"):
			actionsH.Navigate(w, r)
		case hasSuffix(path, "/screenshot"):
			actionsH.Screenshot(w, r)
		case hasSuffix(path, "/eval"):
			actionsH.Eval(w, r)
		default:
			switch r.Method {
			case http.MethodGet:
				tabH.GetTab(w, r)
			case http.MethodDelete:
				tabH.DeleteTab(w, r)
			default:
				http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			}
		}
	})

	// CDP send command
	cdpH := handler.NewCDPHandler(hub, log)
	mux.HandleFunc("/api/cdp", cdpH.Send)

	// Events query
	eventsH := handler.NewEventsHandler(eventStore, log)
	mux.HandleFunc("/api/events", eventsH.List)

	httpServer := &http.Server{
		Addr:    fmt.Sprintf(":%d", cfg.Port),
		Handler: mux,
	}

	return &Server{
		cfg:        cfg,
		log:        log,
		httpServer: httpServer,
		hub:        hub,
	}
}

// Start starts the HTTP server (non-blocking).
func (s *Server) Start() error {
	go func() {
		s.log.WithField("addr", s.httpServer.Addr).Info("[Server] HTTP server listening")
		if err := s.httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			s.log.WithError(err).Fatal("[Server] HTTP server error")
		}
	}()
	return nil
}

// Stop gracefully shuts down the server.
func (s *Server) Stop() error {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	if err := s.httpServer.Shutdown(ctx); err != nil {
		return fmt.Errorf("shutdown: %w", err)
	}
	s.log.Info("[Server] HTTP server stopped")
	return nil
}

func hasSuffix(path, suffix string) bool {
	return strings.HasSuffix(strings.TrimRight(path, "/"), suffix)
}
