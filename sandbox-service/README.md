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
| GET | `/v1/internal/exec/stats` | 并发观测：`inflight` / `queued` / `executed_total` / `rejected_total` / `max_inflight` |
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

引擎侧开关（**默认已启用**）：`sandbox_backend=service`（默认值）· `sandbox_service_url`
（默认 `http://sandbox-service:8001`）· `sandbox_service_tenants`（**租户白名单**；空 = 全部租户走服务，
填名单则只有名单内租户走服务 —— 用于灰度 / 豁免；含 `*` 等价于空）。

引擎启动时会探测本服务健康：不可达且 `DEGRADED_MODE=false`（生产默认）⇒ **拒绝启动**；
`DEGRADED_MODE=true`（单机开发）⇒ 告警并回退进程内执行。要整体退出隔离用 `SANDBOX_BACKEND=local`。

## 扩缩与容量

**并发闸门（服务侧）**：`SANDBOX_MAX_INFLIGHT`（默认 8）限制每个 worker 同时在飞的执行数，
超出**排队**；排队超过 `SANDBOX_QUEUE_TIMEOUT_SECONDS`（默认 30s）才拒绝（非零退出码 + 原因）。
与容器 `pids_limit` 对齐：一次执行约 2–3 个进程，因此 `pids_limit` 应 ≥ `MAX_INFLIGHT × 3`。
用 `uvicorn --workers N` 时**每个 worker 各持一份**闸门。

**负载均衡（多副本）**：引擎侧只有一个 URL，而 Docker DNS 的 round-robin 会被 HTTP
keep-alive 抵消（连接建立后一直复用）。因此 compose 里加了内部 nginx（服务名 `sandbox-lb`，
配置见 `deploy/nginx/sandbox.conf`）：`least_conn` + `server sandbox-service:8001 resolve`
（动态解析 ⇒ `--scale sandbox-service=N` 后新副本自动纳入；需 nginx ≥ 1.27.3）。
引擎的 `SANDBOX_SERVICE_URL` 指向 `http://sandbox-lb:8001`。

**引擎侧闸门**：`SANDBOX_SERVICE_MAX_CONCURRENCY`（默认 0 = 不限）。多副本时建议设置，
避免 N 个引擎副本各自无限并发把沙箱层打满。

**实测口径**：`python scripts/bench_sandbox.py --url http://sandbox-lb:8001 --token $INTERNAL_TOKEN`
—— 并发阶梯输出 RPS / 失败率 / P50·P95·P99，并在每级前后读 `/stats`。
把「RPS 不再涨且 P99 可接受」的并发数作为**每实例安全并发数**，据此设 `MAX_INFLIGHT`、
`SANDBOX_SERVICE_MAX_CONCURRENCY` 与副本数 M。

**审计**：本容器根是 `read_only`，落盘必失败 ⇒ 必须外发。compose 已配
`EXEC_AUDIT_SHIP_URL=http://gateway:8080/v1/internal/audit/exec`（端点是 `audit/exec`），
并让 **gateway 也加入 `sandbox_net`**（否则本服务够不到网关）。`record_execution` 的
落盘与外发已**解耦**：写盘失败不再阻断外发。

## 部署必须配齐的（代码之外的边界，见 `vendor/规划.md` §5.3）

- **无状态**、**最小权限**、**不挂可写宿主路径**、**不与引擎共容器**；
- 只在内网可达（不要暴露到公网入口）；
- **`SANDBOX_ROOT` 必须指向与引擎共享的同一个工作区卷** ⚠ —— 这是"实现时才发现"的前提：
  `CompositeBackend` 的契约是"`execute` 与 `read`/`write` 必须落在**同一文件系统**"，
  否则命令写出的产物与工具读到的文件会**分叉**（功能看起来能跑，产物读不回来）。
  共享卷不削弱隔离目标 —— 本服务隔离的是**故障/资源/审计**，不是文件系统；
- 审计要**多副本收集**，否则"隔离更好了、但看不见了"。

## 还没做的（如实列出）

- **真实部署验证**：编排（受限容器 + internal 网络 + 共享卷 + nginx 均衡）与启动门禁已落地，
  但 `nginx -t`、真实并发阶梯、审计外发落库、副本被 kill 后的表现**都还没在真实集群跑过**；
- **实测定容**：`MAX_INFLIGHT` / 副本数 M / 引擎闸门的默认值都是保守估计，
  需按 `scripts/bench_sandbox.py` 的结果校准；
- `uvicorn --workers N` 的取值（多 worker 各持一份闸门，需与 `pids_limit` 一起算）。
