package main

import (
	"flag"
	"fmt"
	"os"
	"os/signal"
	"syscall"

	"browser-agent-proxy/pkg/config"
	"browser-agent-proxy/pkg/logger"
	"browser-agent-proxy/pkg/server"
)

func main() {
	// Parse command line flags
	port := flag.Int("port", 12345, "Server port")
	logLevel := flag.String("log-level", "info", "Log level (debug, info, warn, error)")
	flag.Parse()

	// Initialize logger
	log := logger.NewLogger(*logLevel)

	// Create configuration
	cfg := &config.Config{
		Port:     *port,
		LogLevel: *logLevel,
	}

	log.WithField("port", cfg.Port).Info("Starting Browser Agent Proxy")

	// Create and start server
	srv := server.NewServer(cfg, log)
	if err := srv.Start(); err != nil {
		log.WithError(err).Fatal("Failed to start server")
	}

	// Wait for interrupt signal
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, os.Interrupt, syscall.SIGTERM)
	<-sigChan

	log.Info("Shutting down server...")
	if err := srv.Stop(); err != nil {
		log.WithError(err).Error("Error during shutdown")
	}
	fmt.Println("Server stopped")
}
