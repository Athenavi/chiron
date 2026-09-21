.PHONY: all build run clean test lint fmt

APP=chiron
BUILD_DIR=build
VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo "dev")

all: fmt lint test build

build:
	mkdir -p $(BUILD_DIR)
	CGO_ENABLED=0 go build -mod=mod -ldflags="-s -w" -o $(BUILD_DIR)/$(APP) ./cmd/$(APP)
	@echo "build: $(BUILD_DIR)/$(APP)"

run:
	go run -mod=mod ./cmd/$(APP)

test:
	go test -mod=mod ./... -v -count=1 -timeout=30s

lint:
	go vet -mod=mod ./...
	@test -f $(shell which golangci-lint 2>/dev/null) && golangci-lint run || echo "golangci-lint not installed, skipping"

fmt:
	go fmt -mod=mod ./...

clean:
	rm -rf $(BUILD_DIR)

dev:
	@echo "starting dev server..."
	@echo "  Ensure .env file exists with POSTGRES_DSN and REDIS_ADDR configured"
	@echo "  See .env.example for reference"
	$(MAKE) run

docker-build:
	docker build -t $(APP):latest -t $(APP):$(VERSION) -f Dockerfile .
	@echo "docker image: $(APP):latest, $(APP):$(VERSION)"

docker-run:
	docker compose up -d

.PHONY: all build run clean test lint fmt dev docker-build docker-run

