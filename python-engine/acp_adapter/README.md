# ACP 适配层（方案 03 §5 · 批 I）

把 Chiron 包成 ACP 的 `Agent`，供编辑器（Zed 等）使用。**纯客户端**：只调既有的
`/submit` + `/events` + 控制端点，不 import 引擎内部类。

## 装与配

```bash
pip install agent-client-protocol   # 只有这条产品线需要它；引擎与评测都不需要
export CHIRON_API_KEY=...           # 一个连接 = 一个身份；缺了就退出，不"连上再说"
export CHIRON_BASE_URL=http://127.0.0.1:8080
python -m acp_adapter
```

编辑器侧（Zed 为例）：

```toml
[agent_servers.chiron]
command = "python"
args = ["-m", "acp_adapter"]
env = { CHIRON_API_KEY = "...", CHIRON_BASE_URL = "http://127.0.0.1:8080" }
```

## 选型：为什么是 Python + 官方 SDK（与设计稿的原推荐不同）

设计稿推荐 **Go**（复用批 F 的 `/submit` + `/events` + 审批处理，**零新增依赖**）。真正动手时看清了
ACP 的 wire 协议规模：20+ 个 schema 类型（`InitializeResponse` / `NewSessionResponse` /
`PromptResponse` / `ToolCallStart` / `ToolCallUpdate` / `PermissionOption` / `AgentPlanUpdate` …），
且官方 SDK 已经提供**方法分发与 stdio 服务端**。用 Go 自己实现这些，等于把"协议正确性"押在通读规格
上 —— 而协议写错的后果是**整个适配层不可用**，这比"少一个依赖"重得多。

代价是一个**只属于本条产品线**的依赖。它与"不引清单外依赖"的约束不冲突：引擎与评测都不需要它，
本模块**延迟 import**，没装的机器上连 `import acp_adapter.agent` 都不会炸。

## 结构

| 文件 | 职责 |
|---|---|
| `mapping.py` | 引擎事件 → ACP 更新语义（**纯函数、可单测**、不依赖 SDK） |
| `stream.py` | HTTP + SSE 客户端（**可单测**：注入 httpx `MockTransport`） |
| `agent.py` | SDK 接线（`Agent` 的 7 个方法）；延迟 import |
| `__main__.py` | stdio 入口（**日志一律走 stderr**：stdout 是 JSON-RPC 通道） |

## 已做的（可验证）与待联调的

**已做**：映射语义 · SSE 解析与终态（含 `cancelled`）· 取消==真取消（调 `/v1/agent/interrupt`）·
缺 SDK / 缺身份的明确退出 · 非文本块留 warning（不静默丢）。测试见 `tests/test_acp_adapter.py`。

**待联调（需要 SDK + 一个真实 ACP 客户端）**：

1. `_emit()` 里 `self._conn.session_update(...)` 的调用形状（按 SDK 版本核对 —— 参照实现用的是这个属性）；
2. `run_agent(agent)` 的 serve 形状；
3. **permission 请求的接线**：`PermissionAsk` 目前**只记日志** —— agent→client 的回调要接上；
   在接上之前**不假装问过用户**（那会让"未裁决"看起来像"已批准"）；
4. `load_session` 的历史端点与分页；
5. `initialize` 的能力声明（只声明我们真的会发的更新类型）。

## 不承诺

* **事件回放**：`load_session` 只拉历史消息；窗口外的 SSE 事件补不回来，也不假装能补；
* **图片 / 音频 / 资源块**：当前链路只有文本（`prompt_text` 会记 warning）；
* **plan 更新**：引擎有 `todo_updated` 事件，但适配层目前只把它降级成提示（映射到 ACP 的
  `AgentPlanUpdate` 是后续的事，先不声明）。
