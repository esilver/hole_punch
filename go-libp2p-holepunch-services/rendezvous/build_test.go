package main

import (
    "os/exec"
    "testing"
)

// TestBinaryBuild ensures the rendezvous service builds without errors. This is a guard
// for Docker image builds (which also run `go build`).
func TestBinaryBuild(t *testing.T) {
    cmd := exec.Command("go", "build", "-o", "build-test-bin", ".")
    if out, err := cmd.CombinedOutput(); err != nil {
        t.Fatalf("go build failed: %v\n%s", err, string(out))
    }
} 