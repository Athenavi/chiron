// Package api 的 API 版本管理策略。
//
// 当前版本：v1（路由前缀 /v1/）
//
// 一、版本化路由规范
//
// 所有业务端点必须使用 /v1/ 前缀（如 /v1/auth/login, /v1/agents, /v1/kb）。
// 以下例外路由保持原址（非 /v1/ 前缀）：
//   - /health, /ready          — 编排器探活，不参与版本化
//   - /metrics, /docs/         — Prometheus / API 文档，不参与版本化
//   - /ws/{sessionId}, /ws/rpa — WebSocket 升级，不参与版本化
//   - /media/s/{assetID}       — 签名媒体 URL，不参与版本化
//   - /api/editor/             — 编辑器文件 API（向后兼容，待迁移至 /v1/editor/）
//
// 二、Legacy 路由兼容
//
// 以下遗留路由（无版本前缀）已注册版本化别名，旧版前端继续可用：
//
//	POST /submit          → POST /v1/agent/submit   （2026-Q3 后移除 legacy）
//	POST /cancel          → POST /v1/agent/cancel   （2026-Q3 后移除 legacy）
//	GET  /events          → GET  /v1/events         （2026-Q3 后移除 legacy）
//	GET  /search          → GET  /v1/search
//
// 三、v2 演化策略
//
// 当需要向后不兼容的变更时，按以下步骤执行：
//  1. 注册 /v2/ 路由时保留 /v1/ 旧路由继续服务
//  2. 前端通过 Accept-Version header 或 URL 前缀选择版本
//  3. 旧版本至少维护 1 个发布周期（6 个月）后标记 Deprecated
//  4. 移除 legacy 路由前通过 /v1/system/deprecations 端点公告
//
// 四、内部端点
//
//	/v1/internal/ 前缀仅限 Go↔Python 内部通信，不对外暴露，
//	使用 X-Internal-Token header 鉴权，不参与版本化周期。
package api
