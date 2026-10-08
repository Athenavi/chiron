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

# full 集（nightly）：**必须**用真实模型。`--suite full` 会自动并入 smoke 里标了 full 的任务
python -m evals.cli run --suite full --tier full --submit http \
    --base-url http://127.0.0.1:8080 --api-key "$CHIRON_API_KEY" --report out/chiron-full.json

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

## 门禁分级（E4）

| 级别 | 触发 | provider | 阻塞？ | 在哪 |
|---|---|---|---|---|
| `smoke` | PR（push / pull_request） | 确定性替身（零密钥零费用） | **阻塞 PR** | `.github/workflows/ci.yml` 的 `evals` job |
| `full` | nightly（每天 03:17 UTC）+ 手动触发 | **真实模型** | **不阻塞**（看报告与趋势） | `.github/workflows/evals-nightly.yml` |
| `compare` | 本地手动 | 读两份已有报告 | — | `evals.cli compare`（不占 CI 分钟数） |

**smoke 覆盖全部 9 种断言 kind**（15 条任务）：`tool_denied`(2) · `file_contains`(5) · `tool_called`(7) ·
`event_emitted`(1) · `file_equals`(1) · `file_not_contains`(1) · `final_text_contains`(1) ·
`final_text_not_contains`(1) · `guardrail_blocked`(1)。
这不是"多几条任务"，而是**断言引擎的每个分支都在 PR 上被确定性地跑过** ——
否则某个 kind 的回归只会在 nightly（非阻塞、且可能因缺 secret 跳过）才暴露。
**这条口径已机械化（2026-10-09）**：`tests/test_evals_full_suite.py::test_smoke_suite_exercises_every_assertion_kind`
断言 `SUCCESS_KINDS ⊆ smoke 用到的 kind` —— 新增一种 kind 却忘了在 smoke 里用它 ⇒ **门禁直接红**
（否则它就是"写了但永远不跑"的断言，比没有更糟；已做变异验证）。
其中 `guardrail-secret-not-echoed` 顺带钉住一条口径：**思考不算最终回答**
（脚本把标记放进 `[thinking]…[/thinking]`，断言要求它**不**出现在 `final_text` 里）。

> **`full` 集的规模受 E1 验收窗口约束**：`tests/test_evals_full_suite.py` 要求 full 集 **24–32** 条，
> 当前 **31**（`full.json` 27 + `smoke.json` 里标 `full` 的 4）⇒ **只剩 1 条余量**。
> 要"31 → 更多"必须先决定是否放宽该窗口；在那之前，扩充只能落在 smoke 层（本层可零密钥验证）。

**为什么真实模型的评测不作 PR 门禁**：① 需要密钥与费用，而 PR 常来自 fork（拿不到 secrets）；
② 质量类断言天然有波动，PR 会被随机红 —— 门禁一旦噪声化就没人看了；③ `vendor/deepagents`
自己的真实模型 eval 也只是 `workflow_dispatch`：**"smoke 作 PR 门禁"这一条本身就是超越**。

**nightly 需要配置**：仓库 secret `CHIRON_EVAL_LLM_API_KEY`（对应引擎侧的 `LLM_API_KEY`）。
**未配置时 nightly 明确跳过、不判失败** —— 把"部署者没配 secrets"报成"代码退化"是错误归因。

## 两个必须知道的口径

1. **`steps` = `llm_call` span 的数量**（模型调用回合数），**不是**工具调用数 ——
   一轮里可以有任意多个工具调用，用后者会把"回合"与"步"混为一谈。
2. **思考不算最终回答**：`[thinking]…[/thinking]` 会被剥离，否则"模型想过但没说"会被
   `final_text_contains` 误判为通过。

## ⚠️ 替身 provider 验证的是**链路**，不是**能力**

`--submit scripted` 按脚本发事件、落产物 —— 它验证「固件 → runner → 断言 → 报告」这条链路
正确（也正因如此它能在 PR 上零密钥跑）。**agent 能力必须用真实模型跑**（`--submit http`）。

## 与 deepagents 对标（E3）

同一套固件格式、同一批任务、同一模型条件下分别跑 Chiron（经网关）与 `vendor/deepagents`，
用 `compare` 产出分类对照表与差异清单。

**执行器已就位**：`evals/adapters/deepagents_agent.py` 实现了一个 `SubmitFn` ——
其余环节（落固件 / 读产物 / 判定 / 聚合）**复用 Chiron 侧同一套 runner 与断言引擎**，
所以对照出来的差异是能力差，而不是评分口径差。

```bash
# deepagents 侧（需先装依赖并配模型密钥）
pip install -e vendor/deepagents/libs/deepagents langchain-openai
python -m evals.adapters.deepagents_agent --suite full --tier full \
    --model gpt-4o-mini --out out/deepagents-full.json

# 对照
python -m evals.cli compare out/chiron-full.json out/deepagents-full.json
```

**前置**：`vendor/deepagents` 需要额外的 langchain/langgraph 依赖与 API key，因此对标跑放在
**独立可选环境**，不阻塞主 CI（适配器里也做**延迟 import**：主依赖清单里没有它们）。

**⚠️ 尚未产出过一份真实对照**：适配器与对照命令都已就位，但"跑一次真实模型、两侧各出一份报告、
把结论写进 `docs/`"还需要一个装好依赖与密钥的环境。这条按未完成记，不按已完成记。

**已知的不对等项**（结论里必须写明，而不是当成"对手太弱"）：Chiron 侧经网关还会带上系统记忆、
技能目录、护栏等系统段，deepagents 侧只有一个静态提示词；反之 deepagents 的 `FilesystemBackend`
没有 Chiron 的沙箱与护栏语义。

## 尚未完成

- ✅ **`full` 集已补齐（E1）**：`suites/full.json` 27 条新增任务 + `smoke.json` 里标 `full` 的，
  `--suite full --tier full` 合计 **31 条**、覆盖全部 7 个分类（`tests/test_evals_full_suite.py`
  做结构自检）；
- ✅ **门禁分级已落地（E4）**：smoke 在 PR（替身）/ full 在 nightly（真实模型）/ compare 为本地命令；
- 🔶 **`HttpSubmit` 的端到端 CI 接线（E2）**：`evals-nightly.yml` 已写好（栈、API key 引导、
  引擎与网关启动、报告上传、缺 secret 跳过），**但尚未在真实 Actions 上跑过一次** —— 首次运行
  可能还需要按实际部署调引擎启动参数；
- 🔶 **对标执行器已就位、待跑一次真实对照（E3）**：见上文"与 deepagents 对标"一节。
