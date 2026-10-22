# ACP（Agent Client Protocol）接入

`cmd/chiron-acp` 是一个 **ACP 前端**：把 Chiron 的 agent 暴露给支持 ACP 的编辑器
（Zed 等）。编辑器用标准协议说话，本进程负责翻译成 Chiron 网关的 HTTP API。

```
editor ──(ACP: JSON-RPC 2.0 · 换行分隔 · stdio)──> chiron-acp ──(HTTP / SSE)──> gateway ──> engine
```

## 为什么是独立进程

ACP 说的是 **stdio + JSON-RPC**，网关是 HTTP 服务。把 stdio 语义塞进 HTTP 进程会同时破坏
两者（stdin 只有一个、生命周期绑定终端）。独立进程还顺带保证了"编辑器崩了不影响网关"。

## 快速接入

```bash
# 1) 准备一个 Chiron API token（后台「API 密钥」页创建，或用登录得到的 JWT）
export CHIRON_ACP_TOKEN="<your-token>"
# 2) 指向网关（默认 http://127.0.0.1:8080）
export CHIRON_GATEWAY_URL="http://127.0.0.1:8080"
# 3) 编译并运行（编辑器会自己拉起它）
go build -mod=mod -o chiron-acp ./cmd/chiron-acp
```

Zed 的 `settings.json`：

```json
{
  "agent_servers": {
    "Chiron": {
      "command": "/absolute/path/to/chiron-acp",
      "env": {
        "CHIRON_GATEWAY_URL": "http://127.0.0.1:8080",
        "CHIRON_ACP_TOKEN": "<your-token>"
      }
    }
  }
}
```

> 日志一律写到 **stderr**（stdout 是协议通道）。排查连接问题时看 stderr。

## 已实现的方法

| 方法 | 说明 |
| --- | --- |
| `initialize` | 能力协商。**未配置 token 时会在 `authMethods` 里如实说明**，而不是假装就绪 |
| `authenticate` | 用 `GET /v1/models` 探活一次；token 不可用当场失败（不等到用户发第一条消息） |
| `session/new` | 在网关 `POST /v1/conversations` 建**真实会话** —— 历史/计费/审计与会话一致地成立 |
| `session/prompt` | `POST /v1/agent/submit`（SSE），边流边推 `session/update` |
| `session/cancel` | 取消本轮：同时 `POST /v1/agent/interrupt`（引擎侧真中断） |

未实现的方法一律返回 **`-32601`**（Method not found）—— 明确失败，不假装支持。

## 事件映射

引擎事件 → ACP `session/update`：

| 引擎事件 | ACP update |
| --- | --- |
| `text` | `agent_message_chunk` |
| `thinking` | `agent_thought_chunk` |
| `tool_call` | `tool_call`（`status: in_progress`） |
| `tool_result` | `tool_call_update`（`status: completed` / 结果含 error 时 `failed`） |
| `error` | 作为一条 `agent_message_chunk` 如实呈现，并以 `stopReason: refusal` 收尾 |
| `cancelled` | `stopReason: cancelled` |
| `done` | `stopReason: end_turn` |

## 边界（有意为之）

- **不做客户端侧 fs 代理**：`promptCapabilities.image/audio/embeddedContext` 一律声明为 `false`，
  因为 Chiron 的文件工作在服务端沙箱里完成，不经过编辑器的文件系统。
- **不存储凭据**：token 只从环境变量读，进程内不做持久化。
- **不做 `session/request_permission`**：工具的确认走 Chiron 自己的审批通道（`/v1/agent/approval`），
  ACP 侧不重复一套。
