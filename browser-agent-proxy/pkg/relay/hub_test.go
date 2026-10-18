package relay

import (
	"context"
	"encoding/json"
	"io"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/sirupsen/logrus"
)

func testHub() *Hub {
	log := logrus.New()
	log.SetOutput(io.Discard)
	return NewHub(log, nil)
}

func TestDisconnectUnblocksPendingWithoutWaitingForTimeout(t *testing.T) {
	h := testHub()
	c := h.RegisterClient(nil)
	done := make(chan error, 1)
	go func() { _, err := h.Send("browser.action", nil, 30); done <- err }()
	<-c.Send
	h.UnregisterClient(c)
	select {
	case err := <-done:
		if err == nil || !strings.Contains(err.Error(), "outcome unknown") {
			t.Fatalf("error = %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("disconnect did not unblock pending request")
	}
	h.mu.RLock()
	defer h.mu.RUnlock()
	if len(h.pendingReq) != 0 {
		t.Fatal("pending request leaked")
	}
}

func TestReplacementRejectsOldConnectionReplies(t *testing.T) {
	h := testHub()
	old := h.RegisterClient(nil)
	current := h.RegisterClient(nil)
	h.UnregisterClient(old)
	if !h.IsConnected() {
		t.Fatal("old unregister removed new connection")
	}
	done := make(chan error, 1)
	go func() { _, err := h.Send("test", nil, 5); done <- err }()
	var command map[string]interface{}
	json.Unmarshal(<-current.Send, &command)
	reply, _ := json.Marshal(map[string]interface{}{"id": command["id"], "result": map[string]interface{}{"ok": true}})
	h.HandleClientMessage(old, reply)
	select {
	case err := <-done:
		t.Fatalf("old reply accepted: %v", err)
	default:
	}
	h.HandleClientMessage(current, reply)
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("valid reply not accepted")
	}
	h.UnregisterClient(current)
}

func TestConcurrentSendAndDisconnect(t *testing.T) {
	for i := 0; i < 50; i++ {
		h := testHub()
		c := h.RegisterClient(nil)
		var wg sync.WaitGroup
		for j := 0; j < 16; j++ {
			wg.Add(1)
			go func() { defer wg.Done(); h.Send("test", nil, 1) }()
		}
		h.UnregisterClient(c)
		wg.Wait()
	}
}

func TestCallerCancellationRemovesPendingRequest(t *testing.T) {
	h := testHub()
	c := h.RegisterClient(nil)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { _, err := h.SendContext(ctx, "test", nil, 30); done <- err }()
	<-c.Send
	cancel()
	select {
	case err := <-done:
		if err == nil {
			t.Fatal("cancel ignored")
		}
	case <-time.After(time.Second):
		t.Fatal("cancel blocked")
	}
	h.mu.RLock()
	count := len(h.pendingReq)
	h.mu.RUnlock()
	if count != 0 {
		t.Fatal("pending request leaked")
	}
	h.UnregisterClient(c)
}
