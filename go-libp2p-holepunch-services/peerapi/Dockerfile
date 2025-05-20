# --- Build stage ---
FROM --platform=$BUILDPLATFORM golang:1.24.3-alpine AS builder
RUN apk add --no-cache git
WORKDIR /app
COPY . ./
RUN go mod tidy && go mod download && \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o peerapi .

# --- Runtime stage ---
FROM gcr.io/distroless/static-debian11
WORKDIR /app
COPY --from=builder /app/peerapi .
EXPOSE 8080
ENTRYPOINT ["/app/peerapi"] 