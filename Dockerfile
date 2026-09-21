# Backend - Multi-stage build
FROM golang:1.23-alpine AS builder
WORKDIR /build
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod \
    go mod download -x
COPY . .
RUN --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 go build -mod=mod -o /build/chiron ./cmd/chiron/
RUN --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 go build -mod=mod -o /build/chiron-cli ./cmd/chiron-cli/

FROM alpine:3.20
# Security: runtime dependencies + upgrade base image packages
RUN apk upgrade --no-cache && apk add --no-cache ca-certificates tzdata wget
# Security: run as non-root user
RUN addgroup -g 1001 -S appgroup && adduser -u 1001 -S appuser -G appgroup
WORKDIR /app
COPY --from=builder /build/chiron /app/
COPY --from=builder /build/chiron-cli /app/
COPY --from=builder /build/migrations /app/migrations/
# Create workspace and plugin directories with proper permissions
RUN mkdir -p /app/workspace /app/data/plugins && chown -R appuser:appgroup /app
USER appuser
EXPOSE 8080
# Security: health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD wget -qO- http://localhost:8080/health || exit 1
CMD ["./chiron"]

