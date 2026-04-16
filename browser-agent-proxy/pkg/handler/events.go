package handler

import (
	"net/http"
	"strconv"
	"time"

	"browser-agent-proxy/pkg/events"

	"github.com/sirupsen/logrus"
)

// EventsHandler handles the event query API.
type EventsHandler struct {
	store *events.Store
	log   *logrus.Logger
}

func NewEventsHandler(store *events.Store, log *logrus.Logger) *EventsHandler {
	return &EventsHandler{store: store, log: log}
}

// List GET /api/events
//
// Query params:
//
//	tabId   int     - filter by tab ID (0 = all tabs)
//	since   int64   - Unix ms, only return events received after this time
//	method  string  - filter by event method, repeatable; supports exact match
//	                  and domain prefix (e.g. "Page." matches all Page.* events)
//
// Examples:
//
//	/api/events?tabId=123&method=Page.loadEventFired
//	/api/events?tabId=123&method=Page.&method=Network.
//	/api/events?method=tab.closed
func (h *EventsHandler) List(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeError(w, http.StatusMethodNotAllowed, "method not allowed", "METHOD_NOT_ALLOWED")
		return
	}

	q := r.URL.Query()

	tabID := 0
	if s := q.Get("tabId"); s != "" {
		v, err := strconv.Atoi(s)
		if err != nil {
			writeError(w, http.StatusBadRequest, "invalid tabId", "INVALID_TAB_ID")
			return
		}
		tabID = v
	}

	var since time.Time
	if s := q.Get("since"); s != "" {
		ms, err := strconv.ParseInt(s, 10, 64)
		if err != nil {
			writeError(w, http.StatusBadRequest, "invalid since (expected Unix ms)", "INVALID_SINCE")
			return
		}
		since = time.UnixMilli(ms)
	}

	// method is repeatable: ?method=Page.loadEventFired&method=Page.domContentEventFired
	// or use domain prefix: ?method=Page.&method=Network.
	methods := q["method"]

	filter := events.QueryFilter{
		TabID:   tabID,
		Since:   since,
		Methods: methods,
	}

	evs := h.store.Query(filter)

	h.log.WithFields(logrus.Fields{
		"tabId":   tabID,
		"methods": methods,
		"count":   len(evs),
	}).Debug("[Events] Query")

	writeSuccess(w, map[string]interface{}{
		"events": evs,
		"count":  len(evs),
	})
}
