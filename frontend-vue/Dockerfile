# ── Build Stage ──
FROM node:22-alpine AS builder

WORKDIR /app

# 固定 pnpm 主版本到 10：与 CI（pnpm/action-setup version: 10）和 pnpm-lock（lockfileVersion
# 9.0）一致。此前是 `npm install -g pnpm`（不固定版本），pnpm 一旦发布新大版本，本地构建
# 与 CI 就会各装一份不同的 pnpm，锁文件解析与「构建白名单」（pnpm-workspace.yaml）语义随之漂移。
RUN npm install -g pnpm@10
# .npmrc / pnpm-workspace.yaml 必须与 package.json 同层 COPY：`pnpm install` 在下一层
# 就执行，若等到 `COPY . .` 才带入，install 期间读不到 registry 与构建白名单配置
# （实测表现为去连官方源长时间挂住，或 ERR_PNPM_IGNORED_BUILDS 直接退出）。
COPY package.json pnpm-lock.yaml .npmrc pnpm-workspace.yaml ./
# registry 自动选择：对候选源（官方源 / npmmirror / 可经 NPM_REGISTRY_CANDIDATES 追加私有源）
# 测速取最快者 —— 受限网络直连官方源会挂起、海外直连 npmmirror 会慢，改由实测延迟决定。
# 脚本 stdout 只输出 URL；全部不可达时非零退出，`|| echo` 兜底到官方源。
COPY scripts/pick-registry.mjs scripts/
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    REG="$(node scripts/pick-registry.mjs || echo https://registry.npmjs.org)" && \
    echo "pnpm install --registry $REG" && \
    pnpm install --frozen-lockfile --registry "$REG"

# 复制源码并构建
COPY . .
RUN pnpm run build

# ── Production Stage ──
FROM nginx:alpine

# 每次构建都升一次系统包。
# nginx:alpine 这个标签的更新节奏跟 alpine 的安全补丁不同步，构建出来的镜像里
# 会残留已知漏洞。本次是 brotli-libs 1.2.0-r1 的 CVE-2026-111497（Out-of-Bounds Write，
# HIGH，修复版 1.2.1-r0）。不升的话 Trivy 以 HIGH 中断 release-images 流水线。
# 用整包升级而不是只装 brotli：同类滞后不止一个包，逐个点名迟早会漏。
RUN apk upgrade --no-cache

# 复制构建产物
COPY --from=builder /app/dist /usr/share/nginx/html

# Nginx 配置
COPY nginx.conf /etc/nginx/conf.d/default.conf
# 安全头片段：被 nginx.conf 的 server 级与各静态 location include
# （nginx 的 add_header 在子块出现会覆盖父级，见该片段的说明）
COPY security-headers.conf /etc/nginx/snippets/security-headers.conf

# 健康检查
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://localhost:80/ || exit 1

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
