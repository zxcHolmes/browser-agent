package events

import (
	"sync"
	"time"
)

const defaultTTL = 3 * time.Minute

// Event represents a single event pushed from the Chrome extension
type Event struct {
	TabID       int                    `json:"tabId"`
	EventMethod string                 `json:"eventMethod"`
	EventParams map[string]interface{} `json:"eventParams,omitempty"`
	Timestamp   int64                  `json:"timestamp"` // Unix ms from extension
	ReceivedAt  time.Time              `json:"receivedAt"`
}

// Store is a time-windowed in-memory event store.
// Events older than TTL are automatically discarded.
type Store struct {
	mu     sync.Mutex
	events []*Event
	ttl    time.Duration
}

// NewStore creates a Store with the default 3-minute TTL.
func NewStore() *Store {
	s := &Store{
		ttl: defaultTTL,
	}
	go s.runGC()
	return s
}

// Append adds a new event to the store.
func (s *Store) Append(ev *Event) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.events = append(s.events, ev)
}

// QueryFilter holds all filter criteria for Store.Query.
type QueryFilter struct {
	TabID   int       // 0 = all tabs
	Since   time.Time // zero = no lower bound
	Methods []string  // empty = all event methods; supports exact match and prefix (e.g. "Page.")
}

// Query returns events matching the given filter.
func (s *Store) Query(f QueryFilter) []*Event {
	s.mu.Lock()
	defer s.mu.Unlock()

	cutoff := time.Now().Add(-s.ttl)
	result := make([]*Event, 0)

	for _, ev := range s.events {
		if ev.ReceivedAt.Before(cutoff) {
			continue
		}
		if f.TabID != 0 && ev.TabID != f.TabID {
			continue
		}
		if !f.Since.IsZero() && !ev.ReceivedAt.After(f.Since) {
			continue
		}
		if len(f.Methods) > 0 && !matchesMethod(ev.EventMethod, f.Methods) {
			continue
		}
		result = append(result, ev)
	}
	return result
}

// matchesMethod returns true if eventMethod matches any entry in the filter list.
// Each filter entry can be an exact name ("Page.loadEventFired") or a domain
// prefix ending with "." ("Page." matches all Page.* events).
func matchesMethod(eventMethod string, filters []string) bool {
	for _, f := range filters {
		if f == eventMethod {
			return true
		}
		// prefix match: "Page." matches "Page.loadEventFired" etc.
		if len(f) > 0 && f[len(f)-1] == '.' && len(eventMethod) > len(f) && eventMethod[:len(f)] == f {
			return true
		}
	}
	return false
}

// runGC periodically removes expired events.
func (s *Store) runGC() {
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()
	for range ticker.C {
		s.gc()
	}
}

func (s *Store) gc() {
	s.mu.Lock()
	defer s.mu.Unlock()

	cutoff := time.Now().Add(-s.ttl)
	i := 0
	for _, ev := range s.events {
		if ev.ReceivedAt.After(cutoff) {
			s.events[i] = ev
			i++
		}
	}
	removed := len(s.events) - i
	s.events = s.events[:i]
	if removed > 0 {
		_ = removed // available for logging if needed
	}
}
