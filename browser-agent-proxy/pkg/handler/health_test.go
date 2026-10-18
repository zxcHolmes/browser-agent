package handler

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/sirupsen/logrus"
)

func TestDocsPreserveLiteralPercentAndRenderBaseURL(t *testing.T) {
	r := httptest.NewRequest(http.MethodGet, "http://127.0.0.1:12345/", nil)
	w := httptest.NewRecorder()
	NewHealthHandler(logrus.New()).ServeHTTP(w, r)
	body := w.Body.String()
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d", w.Code)
	}
	for _, want := range []string{"width: 100%;", "http://127.0.0.1:12345"} {
		if !strings.Contains(body, want) {
			t.Errorf("missing %q in rendered docs", want)
		}
	}
	if strings.Contains(body, "%!") || strings.Contains(body, "{{BASE_URL}}") {
		t.Fatal("rendered docs contain formatting errors or unresolved placeholders")
	}
}
