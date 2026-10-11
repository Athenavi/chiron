# ── Build Stage ──
FROM node:22-alpine AS builder

WORKDIR /app

# 安装pnpm与依赖（使用缓存加速）
RUN npm install -g pnpm
COPY package.json pnpm-lock.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile

# 复制源码并构建
COPY . .
RUN pnpm run build

# ── Production Stage ──
FROM nginx:alpine

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
