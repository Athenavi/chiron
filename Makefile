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

# ── 开发环境（容器）──
# 前置：cp .env.example .env 并填写必填项；首次需先对目标库跑迁移：
#   python -m alembic -c alembic.ini upgrade head
dev-up:
	@test -f .env || (echo "缺少 .env：先 cp .env.example .env 并填写必填项" && exit 1)
	docker compose up -d
	@echo "前端入口: http://localhost:3000  （多副本：make dev-up-scale）"

# 多副本形态：验证跨实例语义时用（会话锁 / SSE 重放 / run 归属）
dev-up-scale:
	@test -f .env || (echo "缺少 .env：先 cp .env.example .env 并填写必填项" && exit 1)
	docker compose up -d --scale gateway=2 --scale python-engine=2
	@echo "前端入口: http://localhost:3000  （2 网关 + 2 引擎）"

dev-down:
	docker compose down

# ── 提交前自查 ──
# `check` 是快检（几十秒）；`check-all` 跑全量（pytest 约 2000 例 + 前端构建，数分钟）。
docs-check:
	python scripts/check_doc_links.py
	python scripts/check_doc_line_refs.py
	python scripts/check_md_tables.py
	python scripts/check_source_encoding.py

check-go:
	go build -mod=mod ./...
	go vet -mod=mod ./...
	go test -mod=mod ./internal/... -count=1

check: check-go docs-check
	@echo "check: OK（全量请用 make check-all）"

check-py:
	cd python-engine && python -m pytest -q && ruff check . && mypy app/

check-web:
	cd frontend-vue && npm run build && npm test

check-all: check check-py check-web
	@echo "check-all: OK"

docker-build:
	docker build -t $(APP):latest -t $(APP):$(VERSION) -f Dockerfile .
	@echo "docker image: $(APP):latest, $(APP):$(VERSION)"

docker-run:
	docker compose up -d

.PHONY: all build run clean test lint fmt dev dev-up dev-up-scale dev-down \
	docs-check check-go check check-py check-web check-all docker-build docker-run

