package main

import (
    "os/exec"
    "testing"
)

func TestBinaryBuild(t *testing.T) {
    cmd := exec.Command("go", "build", "-o", "build-test-bin", ".")
    if out, err := cmd.CombinedOutput(); err != nil {
        t.Fatalf("go build failed: %v\n%s", err, string(out))
    }
} 