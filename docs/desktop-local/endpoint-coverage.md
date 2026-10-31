# 本地 / 云端端点覆盖率验收

> 只写代码能证明的内容；与代码冲突时以代码为准。

## 1. 结论

**网关不缺路由；缺引擎时它们统一返回 `python engine not available`（HTTP 500）。**

Go 网关 `/v1/**` 的 **38 条（约 16.5%）** 路由依赖 python-engine，且恰恰是核心体验：
聊天、知识库、图谱、记忆、能力发现。**具体数字以脚本实时输出为准**，不在本文抄写
（路由会增删，抄下来的数只会过期）：

```bash
python scripts/check_endpoint_coverage.py   # 静态：总数 / 依赖引擎数 / 按分组明细
```

统计口径：`mux.Handle("METHOD /path")` 逐条归类，判据是该行是否走 `newProxy(...)`
转发器或直接引用 `pythonClient`（只读源码，不启动进程）。

引擎**已随包**（`electron-builder.yml` 的 `extraResources`）且**已接线**
（`local-stack.cjs` 的 `startEngine` 在网关之前拉起，起不来即中止整栈），
所以这些路由在本地模式下**具备可用条件**。

引擎依赖集中在用户最常打开的入口上（新建对话、知识库、记忆、工作流、能力面板），
而网关自足的分组（管理、媒体、插件、计费）平时不常进 —— 这是"体感几乎全不可用"
与 16.5% 这个统计数字之间落差的来源。

## 2. 引擎就位的四个条件

以下四条**均已实现**，是本地模式可用的前提：

| # | 条件 | 落点 |
|---|---|---|
| 1 | **引擎随包**：产物里出现 `resources/python-engine` | `electron-builder.yml` 的 `extraResources` |
| 2 | **引擎被拉起**：`startLocalStack` 在**网关之前**调用 `startEngine`，并把地址传给 `buildGatewayEnv` | `local-stack.cjs` 的 `buildEngineEnv` / `startEngine` |
| 3 | **停机顺序**：网关 → 引擎 → 引导器（引导器持有 PG/Redis） | `local-stack.cjs` 的 `stopGateway` / `stopEngine` / `stopLauncher` |
| 4 | **降级模式**：引擎带 `DEGRADED_MODE=true` 才能单机启动（否则 `verify_sandbox_backend()` 会因缺少 `SANDBOX_SERVICE_URL` 拒绝启动） | `local-stack.cjs` 的 `buildEngineEnv` |

## 3. 端到端验收

`desktop-local/scripts/verify_local_e2e.py` 真实拉起 core 子进程并逐端点打点，
其中会话地图的数据面（`/v1/conversations`、`/v1/session-map/*`）此前只存在于 Go 网关、
本地模式下 404 过，现已纳入覆盖并钉住 501 兜底语义。

```bash
cd desktop-local && python scripts/verify_local_e2e.py --timeout 300
```

未覆盖的部分见 `smoke.md` §8。
