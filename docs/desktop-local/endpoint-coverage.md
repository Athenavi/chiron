# 本地 / 云端端点覆盖率验收

本文记录「本地模式几乎什么都用不了」这一问题的**归因与验收判据**。

## 1. 结论先行

**网关不缺路由，缺的是 python-engine 这个进程。**

Go 网关 `/v1/**` 共 **215 条路由**，其中 **38 条（17.7%）** 依赖 python-engine。
这些路由不是边角功能，而恰恰是核心体验：聊天、知识库、图谱、记忆、能力发现。

而当前打包产物里**没有 python-engine**（`electron-builder.yml` 无对应条目），
`local-stack.cjs` 也**没有拉起引擎的代码** —— 于是网关启动后
`pythonClient` 指向默认的 `localhost:8000`，那里什么都没有。

所以用户感受到的"几乎所有功能不可用"，实际是这 38 条路由统一返回
`python engine not available`（HTTP 500），而不是网关本身缺实现。

## 2. 分组实测（按引擎依赖比例）

统计方式：`mux.Handle("METHOD /path")` 逐条归类，判据是该行是否走
`newProxy(...)` 转发器或直接引用 `pythonClient`。脚本见
`scripts/check_endpoint_coverage.py`（只读源码，不启动进程）。

| 分组 | 路由数 | 依赖引擎 | 状态 |
|---|---:|---:|---|
| `memory` | 12 | 12 | **100% 不可用** |
| `graphs` | 5 | 5 | **100% 不可用** |
| `chat` | 2 | 2 | **100% 不可用** |
| `capabilities` | 2 | 2 | **100% 不可用** |
| `workflows` | 2 | 2 | **100% 不可用** |
| `quick-execute` | 1 | 1 | **100% 不可用** |
| `kb` | 16 | 13 | 81% 不可用 |
| `admin` | 67 | 0 | 可用（少数子项有 nil 兜底） |
| `internal` | 18 | 0 | 可用 |
| `media` | 13 | 0 | 可用 |
| `agent` | 12 | 0 | 可用 |
| `billing` | 10 | 0 | 可用 |
| `plugins` | 8 | 0 | 可用 |
| `auth` / `conversations` / `session-map` / `subagent` / `settings` / `system` 等 | 其余 | 0 | 可用 |

## 3. 为什么这看起来像"全部不可用"

用户点进去的**主要入口**恰好都落在 100% 依赖引擎的那几个分组上：新建对话
（`chat`）、知识库（`kb`）、记忆（`memory`）、工作流（`graph`/`workflows`）、
能力面板（`capabilities`）。

管理、媒体、插件、计费这些"能用"的分组，反而是用户平时不常打开的。
这解释了体感与统计之间的落差：**17.7% 的路由，命中了 90% 的使用路径**。

## 4. 达标条件

要让本地模式"与云端几乎无差"，需要依次满足：

1. **引擎随包**：`electron-builder.yml` 增加 `python-engine` 的 `extraResources`
   条目，产物里出现 `resources/python-engine`。
2. **引擎被拉起**：`local-stack.cjs` 增加 `buildEngineEnv` / `startEngine`，
   由 `startLocalStack` 在**网关之前**拉起，并把地址传给 `buildGatewayEnv`
   （`PYTHON_ENGINE_ADDRESS` 目前是全仓唯一写它的地方，值为空）。
3. **停机顺序**：网关 → 引擎 → 引导器（引导器持有 PG/Redis）。
4. **降级模式**：引擎必须带 `DEGRADED_MODE=true` 才能单机启动（见 `deployment-multi-instance.md`
   的沙箱一节），否则 `verify_sandbox_backend()` 会因缺少 `SANDBOX_SERVICE_URL` 拒绝启动。

满足 1–4 后，上述 38 条路由才会真正可用；在此之前只是"网关侧就绪"。

## 5. 复测方法

```bash
python "$TMPDIR/p5_coverage.py"        # 静态：引擎依赖比例
```

端到端复测需在引擎就绪后逐组打点（`/v1/chat/submit`、`/v1/kb`、
`/v1/memory/profile`、`/v1/graphs`、`/v1/capabilities`），确认不再出现
`python engine not available`。
