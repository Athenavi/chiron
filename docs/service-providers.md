# 模型服务提供商目录与路由（service providers）

> **溯源（2026-10-08）**：本文件**原先缺失** —— 3 处引用它（`internal/api/llm_providers.go`、
> `python-engine/app/providers/{anthropic,openai}.py`，其中 openai.py 明确要求"见 service-providers.md
> 的 OpenCode 一节"）。现按**代码与注释**重建，**只写代码能证明的内容**；与代码冲突时以代码为准。
> 门禁：`python scripts/check_doc_links.py` 拦住"引用了不存在的文档"这类断链。

## 1. 目录是什么

`internal/api/llm_providers.go` 的 `llmProviderCatalog` 是**内建目录**（当前 **27 条**；新增提供商 =
在此追加一项 + **同步 Python 兜底目录**）。每项是一个 `llmProviderPreset`：

| 字段 | 含义 |
|---|---|
| `id` | 与 keyset / DB 的 provider 名一致（小写） |
| `label` · `vendor` | 展示名 / 厂商名（卡片副标题） |
| `category` | 分组，见 §2 |
| `kind` | 接入协议，见 §2 |
| `base_url` | 目录默认端点；**空 = 必须由管理员手工填写** |
| `api_key_env` | 引擎侧读取的 key 环境变量名（env 种子兜底） |
| `api_key_prefix` | 前端提示 key 形态（如 `sk-`），空则不提示 |
| `model_prefixes` | 供引擎**按模型名路由** |
| `docs_url` | 厂商文档 |
| `cost` · `quality` | 参考成本（$/1M tokens）与参考质量（0–1），供加权路由 |
| `requires_key` | `false` = 本地/自托管端点可无 key（用占位符） |
| `model_discovery` | `false` = 不做 `/v1/models` 自动发现（如 Anthropic 原生协议） |

## 2. 协议与分组

- **协议（kind）**：`openai` = OpenAI 兼容（`/chat/completions`）· `anthropic` = Anthropic Messages。
- **分组（category）**：`international`（国际厂商）· `china`（国内厂商）· `aggregator`（聚合网关）·
  `self_hosted`（自托管推理）。前端按此顺序分区展示。

目录覆盖三类典型形态：国际厂商（OpenAI / Anthropic / Google / xAI / Groq / Mistral …）、
国内厂商（DeepSeek / Moonshot / 智谱 / 通义 DashScope / MiniMax / 百川 / 混元 / 阶跃 …）、
聚合与自托管（OpenRouter / SiliconFlow / one-api / Ollama / vLLM / LM Studio / OpenCode Zen …），
外加 `custom` / `custom-anthropic`（自定义端点）。

**同一厂商可以有多个"协议变体"条目**：例如 `opencode`、`opencode-go` 是 OpenAI 兼容，而
`opencode-anthropic`、`opencode-go-anthropic` 是 Anthropic 协议。**所有 `kind=anthropic` 的条目
（`anthropic` 及三个 `*-anthropic` 变体）都是 `model_discovery=false`** —— 原生 Messages 协议
没有 `/v1/models` 可发现。

## 3. 端点解析与校验

生效端点 = **DB 覆盖优先**（`{id}_base_url`），否则目录默认；两者都做**尾部 `/` 裁剪**。

写入时 `validProviderBaseURL` 只接受**能解析、scheme 为 `http`/`https`、且有 host** 的地址 ——
避免把坏端点写进引擎配置（写进去的后果是"启动看着正常、调用时才失败"）。

## 4. 密钥来源与形态

- **env 种子**：`api_key_env` 指定的环境变量（迁移/首次启动的兜底）；
- **集中密钥**：keyset（`llm:keys:{provider}`，见 `internal/api/llm_keys.go`）；
- `requires_key=false` 的端点（`ollama` / `vllm` / `lmstudio`）可无 key 注册，key 用占位符。

`api_key_prefix` 只用于**前端提示**，不参与校验。密钥在库里是密文，出口凭据不进入引擎进程
（与工具出口凭据同一原则，见 `internal/api/tool_broker.go` 的说明）。

## 5. 模型路由

引擎按**模型名前缀**在候选 provider 之间选路（`model_prefixes`），`cost` / `quality` 是加权路由的
参考值。`model_discovery=true` 的端点支持 `/v1/models` 自动发现（前端"刷新模型列表"用它）。

## 6. 引擎侧注册条件

**注册条件（代码注释里写明）**：`env 种子非空` **或** `keyset 已存在该 provider 的 key` **或**
`requires_key=false`。三条都不满足的 provider 不会在引擎侧注册 —— 表现为"目录里有、调用时没有"。

## 7. 管理端接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/v1/admin/llm-providers` | `AdminHandler.ListLLMProviders`（读权限中间件）：目录 + 生效端点 + key 数量等视图字段 |
| `PUT` | `/v1/admin/llm-providers/{id}` | `AdminHandler.SetLLMProviderConfig`（写权限中间件）：写 DB 覆盖（端点等） |

视图层（`llmProviderView`）会把**目录默认**与**DB 覆盖后的生效值**一起给出，便于前端显示"改过没有"。

## 8. OpenCode（Zen / Go）——必须带会话标识

OpenCode 的网关除了 key 之外**还要求请求头 `x-opencode-session`**；缺失时直接返回：

```
400 MissingSessionID: Request is missing x-opencode-session and cannot be routed
efficiently. Please see https://opencode.ai/docs/go/#where-can-i-use-it
```

这个头**只影响对方的路由与 prompt 缓存**（不影响可用性，但缺了就直接 400）。因此 Chiron 注入的是
**稳定的会话 ID** —— 同一对话的请求走同一路由，prompt 缓存命中更好。

实现方式：`AgentRuntime` 在一次运行开始前把 `task.session_id`（缺省用 `task.id`）写进 contextvar
（`app/providers/session_context.py::set_llm_session_id`），provider 层在发请求时读取并注入该头；
**空值时不注入**（行为与不带头的旧版本一致）。用 contextvar 而不是改 `chat_stream` 的三层签名，
是因为 provider 是长生命周期对象、而会话 ID 是**每次请求**的属性（与 `app/tools/context.py` 同一模式）。

目录条目：`id=opencode`（Label「OpenCode Zen」，`aggregator`，`kind=openai`，默认端点
`https://opencode.ai/zen/v1`）、`opencode-go`，以及两者的 `-anthropic` 变体；
key 从 <https://opencode.ai/auth> 获取。

## 9. 实现落点

| 环节 | 位置 |
|---|---|
| 目录与校验 | `internal/api/llm_providers.go`（`llmProviderCatalog` / `llmProviderEffectiveBaseURL` / `validProviderBaseURL` / `validLLMProviderName`） |
| 集中密钥 | `internal/api/llm_keys.go`（keyset `llm:keys:{provider}`） |
| 引擎侧 key 轮换 | `python-engine/app/gateway/key_ring.py` |
| provider 实现 | `python-engine/app/providers/{openai,anthropic}.py`（协议差异与 `x-opencode-session` 注入） |
| 会话/档位上下文 | `python-engine/app/providers/session_context.py`（会话 ID 与思考档位） |
| 前端展示 | 管理端 provider 卡片（目录 + 生效端点 + 模型列表） |

## 10. 边界

- 目录是**服务端**的单一事实源，前端只负责展示与提交覆盖值；
- 新增 provider 时**必须同步 Python 兜底目录**，否则网关认得、引擎不认；
- `model_prefixes` 是**前缀**匹配，因此新增同前缀模型无需改路由，但**跨厂商同前缀**模型会互相争抢候选。
