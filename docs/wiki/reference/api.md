# 参考：HTTP 面

> **权威来源**：代码（`internal/api/`，138 个 .go）—— 本页只给"面在哪、怎么找"，
> **不逐条复制接口清单**（那会立刻过期，且仓库里已经有更权威的来源）。

## 入口与代理

对外入口是 **frontend 容器的 nginx**（容器形态 `:3000`），它反代到网关（`:8080`）：

```
/v1        → 网关业务接口
/events    → SSE 事件流
/ws        → WebSocket
/submit    → 提交一次 run
/cancel    → 取消
/media     → 媒体
```

本地开发直连前端 `:5173`（Vite），代理配置同上。

## 探针

| 端点 | 含义 |
|---|---|
| `GET /health` | 进程活着 |
| `GET /ready` | **依赖就绪**（编排系统应该用它当就绪探针） |

## 内部面（引擎 → 网关）

引擎不直连数据库，而是通过网关的 `/v1/internal/*` 走统一客户端 ——
所以跑 `integration` 用例时**必须把网关也起起来**。

## 怎么找具体接口

- **路由装配**：`internal/api/gateway_router.go`（只做装配）+ 按域拆开的 `routes_<domain>.go`
  （public / agent / auth / system / conversation / media / market / plugin / billing / proxy / admin）。
- **提交与事件**：`internal/api/submit_handler.go`。
- **错误语义**：见 [错误码](../production/error-codes.md)。

## 前端侧的调用面

`frontend-vue/src/api/` 是前端的 HTTP 封装（各域一个文件，如 `subagent.ts`）；
组件不直接拼 URL，都走这里。

## 延伸阅读

- [架构](../core/architecture.md) · [网关](../core/gateway.md) · [配置](config.md)
