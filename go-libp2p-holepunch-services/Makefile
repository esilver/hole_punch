# Simple convenience Makefile for local development

# Usage examples:
#   make rendezvous            # start rendezvous on default port 40001
#   make worker RENDEZVOUS=<multiaddr>   # start a worker pointing at the rendezvous address
#   make worker-multi N=3 RENDEZVOUS=<multiaddr>  # start N workers in background (requires GNU seq)

.PHONY: rendezvous worker worker-multi peerapi

# Build variables
RENDEZVOUS_DIR=./rendezvous
WORKER_DIR=./worker
PEERAPI_DIR=./peerapi

rendezvous:
	@echo "Starting rendezvous service..."
	@cd $(RENDEZVOUS_DIR) && go run .

worker:
ifdef RENDEZVOUS
	@echo "Starting worker (RENDEZVOUS=$(RENDEZVOUS))..."
	@cd $(WORKER_DIR) && go run . -rendezvous $(RENDEZVOUS)
else
	@echo "Please provide RENDEZVOUS=<multiaddr>, e.g. make worker RENDEZVOUS=/ip4/127.0.0.1/tcp/40001/p2p/<peerid>" && false
endif

# Spawn N workers in background; example: make worker-multi N=5 RENDEZVOUS=<addr>
worker-multi:
ifndef RENDEZVOUS
	@echo "RENDEZVOUS variable required" && false
endif
	@echo "Spawning $(N) workers..."
	@for i in $(shell seq 1 $(or ${N},1)); do \
		( cd $(WORKER_DIR) && go run . -rendezvous $(RENDEZVOUS) ) & \
	done
	@echo "Workers started in background. Use 'jobs' to list."

peerapi:
	@echo "Starting peer discovery API service..."
	@cd $(PEERAPI_DIR) && go run . 