.PHONY: build run clean test fmt vet

# Build the application
build:
	go build -o browser-agent-proxy main.go

# Run the application
run:
	go run main.go

# Run with debug logging
run-debug:
	go run main.go -log-level debug

# Clean build artifacts
clean:
	rm -f browser-agent-proxy

# Run tests
test:
	go test -v ./...

# Format code
fmt:
	go fmt ./...

# Vet code
vet:
	go vet ./...

# Download dependencies
deps:
	go mod download
	go mod tidy

# Install the application
install:
	go install

# Help
help:
	@echo "Available targets:"
	@echo "  build      - Build the application"
	@echo "  run        - Run the application"
	@echo "  run-debug  - Run with debug logging"
	@echo "  clean      - Clean build artifacts"
	@echo "  test       - Run tests"
	@echo "  fmt        - Format code"
	@echo "  vet        - Vet code"
	@echo "  deps       - Download dependencies"
	@echo "  install    - Install the application"
