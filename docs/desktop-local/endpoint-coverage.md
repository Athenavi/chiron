# 本地 / 云端端点覆盖率验收

> 只写代码能证明的内容；与代码冲突时以代码为准。

## 1. 结论

**网关不缺路由；缺引擎时它们统一返回 `python engine not available`（HTTP 500）。**

Go 网关 `/v1/**` 共 **230 条路由**，其中 **38 条（16.5%）** 依赖 python-engine。
这些路由不是边角功能，而恰恰是核心体验：聊天、知识库、图谱、记忆、能力发现。

引擎现在**已随包**（`electron-builder.yml` 的 `extraResources`）且**已接线**
（`local-stack.cjs` 的 `startEngine` 在网关之前拉起，起不来即中止整栈），
所以这 38 条路由在本地模式下**具备可用条件**。剩余工作是端到端打点验收（§5），
而不是补实现。

## 2. 分组统计

统计方式：`mux.Handle("METHOD /path")` 逐条归类，判据是该行是否走
`newProxy(...)` 转发器或直接引用 `pythonClient`。脚本见
`scripts/check_endpoint_coverage.py`（只读源码，不启动进程）。

| 分组 | 路由数 | 依赖引擎 | 说明 |
|---|---:|---:|---|
| `memory` | 12 | 12 | 100% 依赖引擎 |
| `graphs` | 5 | 5 | 100% 依赖引擎 |
| `chat` | 2 | 2 | 100% 依赖引擎 |
| `capabilities` | 2 | 2 | 100% 依赖引擎 |
| `workflows` | 2 | 2 | 100% 依赖引擎 |
| `desktop` | 1 | 1 | 100% 依赖引擎 |
| `quick-execute` | 1 | 1 | 100% 依赖引擎 |
| `kb` | 16 | 13 | 81% 依赖引擎 |
| `admin` | 79 | 0 | 网关自足（少数子项有 nil 兜底） |
| `internal` | 18 | 0 | 网关自足 |
| `billing` | 13 | 0 | 网关自足 |
| `media` | 13 | 0 | 网关自足 |
| `agent` | 12 | 0 | 网关自足 |
| `conversations` | 8 | 0 | 网关自足 |
| `plugins` | 8 | 0 | 网关自足 |
| `auth` | 7 | 0 | 网关自足 |
| `session-map` | 6 | 0 | 网关自足 |
| `subagent` | 5 | 0 | 网关自足 |
| 其余（`settings` / `system` / `traces` / `rpa` / `market` / `tools` 等） | 其余 | 0 | 网关自足 |

引擎依赖**集中在用户最常打开的入口**上（新建对话、知识库、记忆、工作流、能力面板），
而网关自足的分组（管理、媒体、插件、计费）平时不常进 —— 这是"体感几乎全不可用"
与 16.5% 这个统计数字之间落差的来源。

## 3. 引擎就位的四个条件

以下四条**均已实现**，是本地模式可用的前提：

| # | 条件 | 落点 |
|---|---|---|
| 1 | **引擎随包**：产物里出现 `resources/python-engine` | `electron-builder.yml` 的 `extraResources` |
| 2 | **引擎被拉起**：`startLocalStack` 在**网关之前**调用 `startEngine`，并把地址传给 `buildGatewayEnv` | `local-stack.cjs` 的 `buildEngineEnv` / `startEngine` |
| 3 | **停机顺序**：网关 → 引擎 → 引导器（引导器持有 PG/Redis） | `local-stack.cjs` 的 `stopGateway` / `stopEngine` / `stopLauncher` |
| 4 | **降级模式**：引擎带 `DEGRADED_MODE=true` 才能单机启动（否则 `verify_sandbox_backend()` 会因缺少 `SANDBOX_SERVICE_URL` 拒绝启动） | `local-stack.cjs` 的 `buildEngineEnv` |

## 4. 复测方法

```bash
python scripts/check_endpoint_coverage.py   # 静态：路由总数与引擎依赖比例
```

端到端复测需逐组打点（`/v1/chat/submit`、`/v1/kb`、`/v1/memory/profile`、
`/v1/graphs`、`/v1/capabilities`），确认不再出现 `python engine not available`。
`desktop-local/scripts/verify_local_e2e.py` 目前**没有**覆盖这些引擎路由（见 `smoke.md` §8），
补齐它是这条线剩余的验收缺口。
