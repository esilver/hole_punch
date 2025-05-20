package main

import (
    "context"
    "strings"
    "testing"
)

// TestCreateWorkerHostIncludesWebsocket ensures worker host exposes /ws address.
func TestCreateWorkerHostIncludesWebsocket(t *testing.T) {
    ctx := context.Background()
    h, err := createWorkerHost(ctx, 0)
    if err != nil {
        t.Fatalf("createWorkerHost error: %v", err)
    }
    defer h.Close()

    hasWS := false
    for _, a := range h.Addrs() {
        if strings.Contains(a.String(), "/ws") {
            hasWS = true
            break
        }
    }
    if !hasWS {
        t.Fatalf("expected /ws address in %v", h.Addrs())
    }
} 