package events

import (
	"testing"
	"time"
)

func TestRingEvictionRetainsNewestEventsInOrder(t *testing.T) {
	s := &Store{events: make([]*Event, 3), ttl: time.Minute}
	for i := 1; i <= 5; i++ {
		s.Append(&Event{TabID: i, ReceivedAt: time.Now()})
	}
	got := s.Query(QueryFilter{})
	if len(got) != 3 {
		t.Fatalf("count = %d", len(got))
	}
	for i, e := range got {
		if e.TabID != i+3 {
			t.Fatalf("event %d = %d", i, e.TabID)
		}
	}
}

func TestGCReleasesExpiredPayloadsAcrossWrappedRing(t *testing.T) {
	s := &Store{events: make([]*Event, 3), ttl: time.Minute}
	now := time.Now()
	s.Append(&Event{TabID: 1, ReceivedAt: now})
	s.Append(&Event{TabID: 2, ReceivedAt: now.Add(-time.Hour)})
	s.Append(&Event{TabID: 3, ReceivedAt: now})
	s.Append(&Event{TabID: 4, ReceivedAt: now})
	s.gc()
	s.Append(&Event{TabID: 5, ReceivedAt: now})
	got := s.Query(QueryFilter{})
	if len(got) != 3 {
		t.Fatalf("count = %d", len(got))
	}
	for i, e := range got {
		if e.TabID != i+3 {
			t.Fatalf("event %d = %d", i, e.TabID)
		}
	}
	for _, e := range s.events {
		if e != nil && e.TabID == 2 {
			t.Fatal("expired payload retained")
		}
	}
}
