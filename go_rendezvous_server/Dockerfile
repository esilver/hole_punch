# Stage 1: Build the Go application
FROM golang:1.22-alpine AS builder

WORKDIR /app

# Copy go.mod and go.sum first to leverage Docker cache
COPY go.mod go.sum ./
RUN go mod download

COPY main.go .

# Build the Go app
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /go-rendezvous-server main.go

# Stage 2: Create the final minimal image
FROM alpine:latest

WORKDIR /app

# Copy the pre-built binary from the builder stage
COPY --from=builder /go-rendezvous-server /go-rendezvous-server

# Expose the HTTP port (Cloud Run will map this)
# The PORT environment variable will be set by Cloud Run (default 8080)
EXPOSE 8080

# Expose the UDP port for libp2p QUIC traffic
# This is informational for the Dockerfile; Cloud Run port configuration is separate.
EXPOSE 7777/udp

# Command to run the executable
ENTRYPOINT ["/go-rendezvous-server"] 