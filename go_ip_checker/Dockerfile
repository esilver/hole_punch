# Stage 1: Build the Go application
FROM golang:1.21-alpine AS builder

WORKDIR /app

# Copy go.mod and go.sum first to leverage Docker cache
# (Assuming you'll create a go.mod file for this simple app, 
# even if it has no external dependencies other than standard library for now.
# If you don't use go.mod, you can skip these two lines and the go mod download)
# COPY go.mod go.sum ./
# RUN go mod download

COPY main.go .

# Build the Go app
# CGO_ENABLED=0 is important for a statically linked binary in a minimal image
# -ldflags "-s -w" strips debug symbols and DWARF information to reduce binary size
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /go-ip-checker main.go

# Stage 2: Create the final minimal image
FROM alpine:latest

WORKDIR /app

# Copy the pre-built binary from the builder stage
COPY --from=builder /go-ip-checker /go-ip-checker

# Expose port 8080 (or the port your app listens on, as per PORT env var)
EXPOSE 8080

# Set the SCRIPT_NAME environment variable if your app is behind a reverse proxy with a path prefix
# ENV SCRIPT_NAME /go-ip-checker

# Command to run the executable
ENTRYPOINT ["/go-ip-checker"] 