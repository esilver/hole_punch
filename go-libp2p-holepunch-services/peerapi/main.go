package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
	"strings"
	"sync"
)

// peersMap stores peerID -> multiaddr (as string).
// This service keeps everything in memory – fine for demos.
var peersMap = make(map[string]string)
var peersMu sync.RWMutex

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}

	mux := http.NewServeMux()

	mux.HandleFunc("/peers", func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			peersMu.RLock()
			defer peersMu.RUnlock()
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(peersMap)
		case http.MethodPost:
			var payload struct {
				ID   string `json:"id"`
				Addr string `json:"addr"`
			}
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				http.Error(w, "bad JSON", http.StatusBadRequest)
				return
			}
			if payload.ID == "" || payload.Addr == "" {
				http.Error(w, "id and addr required", http.StatusBadRequest)
				return
			}
			peersMu.Lock()
			peersMap[payload.ID] = payload.Addr
			peersMu.Unlock()
			w.WriteHeader(http.StatusNoContent)
		default:
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		}
	})

	mux.HandleFunc("/peers/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodDelete {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		id := strings.TrimPrefix(r.URL.Path, "/peers/")
		if id == "" {
			http.Error(w, "missing id", http.StatusBadRequest)
			return
		}
		peersMu.Lock()
		delete(peersMap, id)
		peersMu.Unlock()
		w.WriteHeader(http.StatusNoContent)
	})

	addr := ":" + port
	log.Printf("Peer discovery API listening on %s (endpoints: GET/POST /peers, DELETE /peers/{id})", addr)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatalf("server error: %v", err)
	}
}
