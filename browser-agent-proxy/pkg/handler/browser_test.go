package handler

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"browser-agent-proxy/pkg/relay"
	"github.com/sirupsen/logrus"
)

func TestValidateUploadFiles(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "视频.mp4")
	if err := os.WriteFile(file, []byte("fixture"), 0600); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name  string
		files interface{}
		valid bool
	}{
		{"regular file", []interface{}{file}, true},
		{"missing", []interface{}{filepath.Join(dir, "missing.mp4")}, false},
		{"directory", []interface{}{dir}, false},
		{"relative", []interface{}{"video.mp4"}, false},
		{"empty", []interface{}{}, false},
		{"wrong type", "video.mp4", false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			err := validateUploadFiles(map[string]interface{}{"files": tc.files})
			if (err == nil) != tc.valid {
				t.Fatalf("error = %v", err)
			}
		})
	}
}

func TestBrowserActionRoutesToExtensionWithCallerContext(t *testing.T) {
	log := logrus.New()
	log.SetOutput(io.Discard)
	hub := relay.NewHub(log, nil)
	client := hub.RegisterClient(nil)
	defer hub.UnregisterClient(client)
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	r := httptest.NewRequest(http.MethodPost, "/api/tabs/123/action", strings.NewReader(`{"action":"fill","args":{"target":"#title","value":"hello"}}`)).WithContext(ctx)
	w := httptest.NewRecorder()
	done := make(chan struct{})
	go func() { NewBrowserHandler(hub).Action(w, r); close(done) }()
	select {
	case data := <-client.Send:
		var command struct {
			ID     int                    `json:"id"`
			Method string                 `json:"method"`
			Params map[string]interface{} `json:"params"`
		}
		if err := json.Unmarshal(data, &command); err != nil {
			t.Fatal(err)
		}
		if command.Method != "browser.action" || command.Params["action"] != "fill" || command.Params["tabId"] != float64(123) {
			t.Fatalf("command = %+v", command)
		}
		reply, _ := json.Marshal(map[string]interface{}{"id": command.ID, "result": map[string]interface{}{"result": map[string]interface{}{"filled": true, "value": "hello"}}})
		if err := hub.HandleClientMessage(client, reply); err != nil {
			t.Fatal(err)
		}
	case <-ctx.Done():
		t.Fatal("action not dispatched")
	}
	select {
	case <-done:
	case <-ctx.Done():
		t.Fatal("handler did not finish")
	}
	if w.Code != 200 || !strings.Contains(w.Body.String(), `"filled":true`) {
		t.Fatalf("response: %d %s", w.Code, w.Body.String())
	}
}
