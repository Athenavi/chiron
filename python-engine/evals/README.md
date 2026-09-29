# Chiron 评测套件（E1 / E2 / E3）

**它存在的理由**：在它之前，"Chiron 是否超越 deepagents"只能靠主观判断 —— 没有度量就没有
超越，也没有回归防线。本套件让这件事变成可读数字。

## 结构

| 模块 | 职责 |
|---|---|
| `firmware.py` | 固件模型与加载（JSON）。任务 = 描述 + 固件文件 + 断言 + 效率期望 |
| `assertions.py` | 断言引擎（**success 硬失败 / efficiency 只记录**），纯函数、可单测 |
| `observe.py` | SSE 事件流 → `Observation`（纯函数）。含步数口径与思考剥离 |
| `runner.py` | 执行器：落固件 → 提交（**可注入**）→ 读产物 → 判定 |
| `submit.py` | `HttpSubmit`（真实网关路径）/ `ScriptedSubmit`（确定性替身） |
| `report.py` | 聚合（pass@k / avg@k / 分类 / 效率）与对标（`compare`） |
| `cli.py` | 命令行入口 |
| `suites/*.json` | 固件；`*.scripted.json` 是对应的替身脚本 |

## 用法

```bash
# 替身 provider：零密钥、零费用 —— 这是它能作 PR 门禁的前提
python -m evals.cli run --suite smoke --submit scripted --report out/smoke.json

# 真实栈（经网关，走用户实际路径）
python -m evals.cli run --suite smoke --submit http \
    --base-url http://127.0.0.1:8080 --api-key "$CHIRON_API_KEY" --report out/chiron.json

# 与对标结果对照（E3）
python -m evals.cli compare out/chiron.json out/deepagents.json
```

退出码 **0 = 全部通过 / 1 = 有失败**，CI 可直接用。

## 断言模型（对位 deepagents 的双层断言）

- **success 断言（不满足即失败）**：`final_text_contains` / `final_text_not_contains` /
  `file_contains` / `file_equals` / `tool_called` / `tool_denied` / `event_emitted` /
  `guardrail_blocked`；
- **efficiency（只记录，不影响通过）**：`max_steps` / `max_tool_calls` / `max_tokens` / `max_wall_ms`。

后者用来发现"做对了但绕远路"的回归，而**不会**把有效解判成失败。

## 两个必须知道的口径

1. **`steps` = `llm_call` span 的数量**（模型调用回合数），**不是**工具调用数 ——
   一轮里可以有任意多个工具调用，用后者会把"回合"与"步"混为一谈。
2. **思考不算最终回答**：`[thinking]…[/thinking]` 会被剥离，否则"模型想过但没说"会被
   `final_text_contains` 误判为通过。

## ⚠️ 替身 provider 验证的是**链路**，不是**能力**

`--submit scripted` 按脚本发事件、落产物 —— 它验证「固件 → runner → 断言 → 报告」这条链路
正确（也正因如此它能在 PR 上零密钥跑）。**agent 能力必须用真实模型跑**（`--submit http`）。

## 与 deepagents 对标（E3）

同一套固件格式、同一批任务、同一模型条件下分别跑 Chiron（经网关）与 `vendor/deepagents`
（`create_deep_agent`，直接调库），用 `compare` 产出分类对照表与差异清单。

**前置**：`vendor/deepagents` 需要额外的 langchain/langgraph 依赖与 API key，因此对标跑放在
**独立可选环境**，不阻塞主 CI。

## 尚未完成

- `smoke` 集 10 条已可跑；`full` 集（24–32 条，含质量类断言）待补；
- `HttpSubmit` 的端到端跑需要真实栈（网关 + 引擎），尚未在 CI 接线；
- PR 门禁（`.github/workflows/`）尚未挂 `smoke`；
- `compare` 尚未接入 deepagents 侧的执行器。
