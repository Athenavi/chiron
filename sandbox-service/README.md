# chiron-sandbox —— 执行沙箱服务（S5-(e) 的 B 层）

把**执行**从引擎进程里挪出去的独立服务：故障隔离（执行把引擎拖垮）、资源隔离（执行独立扩缩/限流）、
审计单点。

> **不是安全等级的跃升。** 基线（命令白名单 / RLIMIT / 逃逸拦截 / 审计）**直接复用引擎那一份**
> （`python-engine/app/tools/sandbox.py` 的 `run_in_sandbox`），不是服务侧另写一份"看起来一样"的名单
> —— 两份名单必然漂移，且漂移方向通常是**放宽**。仍然**没有**命名空间 / seccomp。

## 契约（与引擎既有 `/v1/internal/*` 同构）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/internal/exec/health` | 存活探针；同样要令牌 |
| POST | `/v1/internal/exec/run` | body `{"command", "tenant_id"?, "user_id"?, "session_id"?, "timeout_seconds"?}` → `{"stdout","stderr","exit_code","truncated"}` |

请求头必须带 `X-Internal-Token`（等于 `INTERNAL_TOKEN`）。**没配令牌 = 503 拒绝服务**（fail-closed）。

身份字段**可选但应当传**：服务侧据此恢复工具上下文，`run_in_sandbox` 的审计才答得出
"谁在哪个租户/会话里执行的"。引擎侧客户端 `app/backends/remote_exec.py` 已随请求带上。

被基线拦下 / 超时都会映射为**非零退出码 + 原因进 stderr**（调用方据此看到"为什么没跑"）。

## 怎么跑

```bash
# 引擎那棵树要在 import 路径上（服务复用它的基线；也可用 PYTHONPATH 显式指定）
PYTHONPATH=../python-engine \
INTERNAL_TOKEN=<与引擎一致> \
SANDBOX_ROOT=/var/lib/chiron-sandbox \
uvicorn service:app --host 0.0.0.0 --port 9000
```

引擎侧开关：`sandbox_backend=service` · `sandbox_service_url` · `sandbox_service_tenants`（**租户白名单**；
空白名单 ⇒ 谁都不走服务，这是刻意的安全默认）。

## 部署必须配齐的（代码之外的边界，见 `vendor/规划.md` §5.3）

- **无状态**、**最小权限**、**不挂可写宿主路径**、**不与引擎共容器**；
- 只在内网可达（不要暴露到公网入口）；
- **`SANDBOX_ROOT` 必须指向与引擎共享的同一个工作区卷** ⚠ —— 这是"实现时才发现"的前提：
  `CompositeBackend` 的契约是"`execute` 与 `read`/`write` 必须落在**同一文件系统**"，
  否则命令写出的产物与工具读到的文件会**分叉**（功能看起来能跑，产物读不回来）。
  共享卷不削弱隔离目标 —— 本服务隔离的是**故障/资源/审计**，不是文件系统；
- 审计要**多副本收集**，否则"隔离更好了、但看不见了"。

## 还没做的（如实列出）

- 部署形态：镜像 / compose 编排 / 网络与卷的具体配置（**必须真实部署验证**，不能用单测替代）；
- 服务侧自身的事件循环与并发上限调优；
- 引擎侧按租户白名单的**灰度**操作手册（改白名单即分流，需写清回滚）。
