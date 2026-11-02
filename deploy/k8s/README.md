# 本地 K8s 验证：Chiron 能否无缝扩展

零成本验证"水平扩展"的编排面。**不是**在集群里重建基础设施——本机 compose 的 PG/Redis
继续用，Pod 通过 `host.docker.internal` 直连，这样变量只剩"编排层"。

## 0. 前置

| 项 | 检查 |
|---|---|
| Docker Desktop **运行中** | `docker version --format '{{.Server.Version}}'` 有输出（GUI 首次启动需要接受条款/登录，这一步必须人工完成） |
| kind | `kind version`（`scoop install kind`） |
| kubectl | `kubectl version --client`（Docker Desktop 自带） |
| 本机 PG/Redis 在跑 | `docker ps` 里有 redis；PG 由 compose 提供 |
| 引擎已暴露指标 | `curl http://localhost:8000/metrics` 有 `queue_depth` 等（本次新增的 `/metrics` 路由） |

## 1. 起集群

```powershell
kind create cluster --name chiron --config deploy/k8s/kind-config.yaml
kubectl cluster-info --context kind-chiron
```

## 2. 构建并载入镜像

```powershell
docker build -t chiron/gateway:dev .
docker build -t chiron/python-engine:dev python-engine
docker build -t chiron/frontend:dev frontend-vue
# 迁移镜像：只在发布时跑一次，见 §3e。上下文必须是仓库根目录。
docker build -f deploy/migrations/Dockerfile -t chiron/migrations:dev .
kind load docker-image chiron/gateway:dev chiron/python-engine:dev chiron/frontend:dev chiron/migrations:dev --name chiron
```

> ⚠ 上面全是 `:dev` 浮动标签 + `IfNotPresent`，**只用于本地验证**。生产必须换成
> 不可变 tag 或 `image@sha256:<digest>`：浮动标签被覆盖后，`IfNotPresent` 会让
> "镜像已重建、Pod 仍在跑旧的"这类问题完全无声。

## 3. 注入密钥（从 `.env` 取，别写进 YAML）

```powershell
function EnvVal($n) { ((Get-Content .env | Where-Object { $_ -match "^$n=" } | Select-Object -First 1) -split '=',2)[1].Trim().Trim('"') }
$dsn = (EnvVal POSTGRES_DSN) -replace '@localhost', '@host.docker.internal'
# 引擎只认 REDIS_URL（整串），网关只认 REDIS_ADDR + REDIS_PASSWORD。
# 有密码时 REDIS_URL 里的密码必须做一次 URL 编码，否则含 `@ : /` 会被解析错。
$redisPwd = EnvVal REDIS_PASSWORD
$redisUrl = if ($redisPwd) { "redis://:$([uri]::EscapeDataString($redisPwd))@host.docker.internal:6379/0" } else { "redis://host.docker.internal:6379/0" }
kubectl -n chiron create secret generic chiron-secrets `
  --from-literal=APP_SECRET="$(EnvVal APP_SECRET)" `
  --from-literal=JWT_SECRET="$(EnvVal JWT_SECRET)" `
  --from-literal=INTERNAL_TOKEN="$(EnvVal INTERNAL_TOKEN)" `
  --from-literal=POSTGRES_DSN="$dsn" `
  --from-literal=REDIS_PASSWORD="$redisPwd" `
  --from-literal=REDIS_URL="$redisUrl" `
  --from-literal=METRICS_TOKEN="$(EnvVal METRICS_TOKEN)" `
  --dry-run=client -o yaml | kubectl apply -f - --namespace chiron
```

> `REDIS_URL` 只放在 Secret 里、ConfigMap 里**刻意没有**同名键：两份同名键会靠
> `envFrom` 的先后顺序隐式覆盖，顺序一调就是"NOAUTH"或"密码进日志"的隐性事故。
>
> `APP_SECRET` 必须 **≥32 字符**（`config.Load` 直接拒绝更短的值），且所有副本同值：
> 网关用它派生 JWT / 内部令牌 / 配置加密密钥。换值等于让所有会话与密文失效。

> 顺序：Secret 依赖 Namespace，因此**先** `kubectl create namespace chiron`（或先只 apply
> 清单里的 Namespace 一段），再执行上面的 secret 注入。Namespace 之后的步骤按
> §3b → §3c → §3d → §3e → §4 的顺序走。

## 3b. 创建沙箱服务的 ConfigMap（**必须在 apply 之前**）

`chiron.yaml` 里的 sandbox-service 复用引擎镜像，但服务入口（`service.py`）与
sandbox-lb 的 nginx 配置**不内嵌进 YAML**（避免与仓库里的源文件漂移），而是从工作区
文件创建 ConfigMap。**在仓库根目录**执行：

```powershell
kubectl -n chiron create configmap chiron-sandbox-code `
  --from-file=service.py=sandbox-service/service.py `
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n chiron create configmap chiron-sandbox-lb-conf `
  --from-file=sandbox-lb.conf=deploy/k8s/sandbox-lb.conf `
  --dry-run=client -o yaml | kubectl apply -f -
```

> **为什么必须有**：引擎默认 `SANDBOX_BACKEND=service` 且 fail-closed。这两个
> ConfigMap 缺失 ⇒ sandbox-service Pod 起不来 ⇒ sandbox-lb 不健康 ⇒
> **python-engine 拒绝启动**（`sandbox service ... unreachable and DEGRADED_MODE=false`）。
> 若某次验证确实不想跑沙箱层，可临时把 ConfigMap 的 `SANDBOX_BACKEND` 改成 `local`
> （会牺牲执行隔离，仅限验证）。

## 3c. 创建前端 nginx 配置的 ConfigMap（必须在 apply 之前）

frontend 容器用 **K8s 版** nginx 配置（`resolver` 指向 kube-dns、上游用 gateway 的 FQDN）：

```powershell
kubectl -n chiron create configmap chiron-frontend-nginx `
  --from-file=default.conf=deploy/k8s/frontend-nginx.conf `
  --dry-run=client -o yaml | kubectl apply -f -
```

> 同理不内嵌进 YAML，避免与 `deploy/k8s/frontend-nginx.conf` 漂移。

## 3d. 在共享卷上预建子目录（**必须在 apply 之前**）

`chiron-shared` 这一条 PVC 被切成两个**互不可见**的子目录使用：

| 子目录 | 内容 | 谁挂载 |
|---|---|---|
| `workspace/` | agent 文件区、编辑器、上传、媒体（含 `media/`） | gateway、python-engine、sandbox-service |
| `plugins/` | `{user_id}/plugins.json`（含用户插件凭据） | gateway、python-engine |

必须用 `subPath` 分开：不带 subPath 时两者是**同一个目录**，沙箱里任意命令都能改写
插件凭据（横向提权）。

`subPath` 指向的子目录**不会**被 kubelet 自动创建，必须先建一次（清单见
`deploy/k8s/pvc-init.yaml`，一次性 Pod）：

```powershell
kubectl -n chiron delete pod pvc-init --ignore-not-found
kubectl apply -f deploy/k8s/pvc-init.yaml
kubectl -n chiron wait --for=jsonpath='{.status.phase}'=Succeeded pod/pvc-init --timeout=120s
kubectl -n chiron logs pvc-init
kubectl -n chiron delete pod pvc-init
```

> 漏掉这一步的典型现象：Pod 卡在 `ContainerCreating`，`kubectl describe` 里是
> `failed to mount ... no such file or directory`（subPath 不存在时不会自动创建）。

## 3e. 执行数据库迁移（**必须在滚动发布之前，且只跑一次**）

应用**从不执行 DDL**：网关启动只做只读的 schema 版本校验，版本不一致直接
`FATAL: refusing to start on mismatched schema`。所以迁移是发布流程的硬前置，
且必须**先迁移、后滚动**：

```powershell
kubectl -n chiron delete job chiron-migrate --ignore-not-found
kubectl apply -f deploy/k8s/migrate-job.yaml
kubectl -n chiron wait --for=condition=complete job/chiron-migrate --timeout=10m
kubectl -n chiron logs job/chiron-migrate    # 确认 alembic 输出到 "Running upgrade ... -> <head>"
```

要点：
- 迁移镜像由 `deploy/migrations/Dockerfile` 构建（`alembic` 只在
  `requirements-migrate.txt` 里，运行时不装）。
- Job 直连 PG **5432**，不走 pgbouncer：DDL 需要会话级语义，transaction 池下会拿到
  半途断开的连接。
- 迁移必须**串行**：两个 Job 同时 `upgrade` 会在 `alembic_version` 行锁上互等到超时。
- 只想出 SQL 给 DBA 审阅/在受控窗口执行：`docker run --rm chiron/migrations:dev upgrade head --sql`。

## 4. 部署并观察

```powershell
kubectl apply -f deploy/k8s/chiron.yaml
kubectl -n chiron get pods -w   # 期望：3 gateway + 3 python-engine + 2 frontend + 2 sandbox-service + 2 sandbox-lb 全 Ready
```

> 沙箱层从 1 副本改成 2：引擎侧 `SANDBOX_BACKEND=service` 是 **fail-closed**，单副本的
> sandbox-service 或 sandbox-lb 一挂，`run_in_sandbox` 全部失败（不会降级到本地执行）。
> 两个 Deployment 都配了 host 级反亲和，扩到 2 才是"无缝"的前提。

前端入口：`http://localhost:18082`（kind-config 把节点 30082 映射到宿主 18082）。

网关**不再**发布节点端口：它的 mux 上同时挂着 `/v1/internal/*`（含 `/v1/internal/db/execute`
这类能执行 SQL 的端点），NodePort 会把它们暴露在每个节点 IP 上，因此这里已收回
`ClusterIP`。本地直连调试改用：

```powershell
kubectl -n chiron port-forward svc/gateway 8080:8080
```

`chiron.yaml` 末尾是一组 `NetworkPolicy`（默认拒绝入站 + 逐服务放行）。**kind 自带的
kindnet 不支持 NetworkPolicy**，apply 不报错但策略不生效；要验证策略得换 CNI
（Calico/Cilium）或至少在真集群上测。

## 5. 验证清单（逐条对应项目已有的扩展设计）

| # | 要验的属性 | 操作 | 判定 |
|---|---|---|---|
| 1 | **就绪 ≠ 存活** | 临时把 ConfigMap 的 `REDIS_URL` 改成错误值 → `kubectl rollout restart deploy/gateway` | Pod 变 **NotReady**（被摘出 Service），但 **RESTARTS 仍为 0**；改回后自动恢复 |
| 2 | **滚动升级零中断** | 一边 `kubectl rollout restart deploy/gateway`，一边压接口 | `maxUnavailable: 0` 下请求失败数 **0** |
| 3 | **PDB 生效** | `kubectl drain <node> --ignore-daemonsets --delete-emptydir-data` | 驱逐过程中 gateway 可用副本不降到 2 以下 |
| 4 | **HPA 弹性** | 装 metrics-server（见 YAML 末尾注释）→ 压测 | 副本数随 CPU 上升，之后回落 |
| 5 | **队列全局并发门控** | 向 `engine:tasks` 灌入一批 `tool_job`，观察各副本 | 3 个副本 in-flight 总和 ≤ `QUEUE_WORKER_GLOBAL_CONCURRENCY`（`worker.py` 的 Redis Lua 门控） |
| 6 | **PEL 崩溃接管** | `kubectl delete pod <某个 engine>` | 它 in-flight 的任务在 10 分钟窗口后被别的副本 `XCLAIM` 接管，且**不重复执行**（查 `task_idempotency`） |
| 7 | **会话亲和 + 归属路由** | 对同一 session 连发多个请求；再触发一次工具审批 | 请求尽量落到同一副本；审批在**持有该 run 的副本**上完成（`engine:run:{sid}`）；kill 持有者后审批返回**明确错误**而非静默失败 |
| 8 | **跨实例 SSE / 取消** | 在副本 A 派发后台子 Agent，前端连在副本 B 上观察；再在 B 上点「停止」 | B 能收到 A 产生的事件（`chiron:events`）；取消经 `subagent:cancel` 广播到 A 生效 |
| 9 | **schema 闸门** | 用一个"旧代码"镜像（例如临时回退一个迁移文件的 tag）起 Pod | 该 Pod **拒绝启动**并报 `database schema does not match this build`（`schema_version.go`） |
| 10 | **优雅停机** | `kubectl delete pod <engine>` | 日志出现 `Queue worker stopping, waiting for N in-flight tasks...` → `stopped`，30s 内排空 |

## 6. 已知约束（测的时候一定会撞到）

1. **`chiron-shared` 一条 RWX PVC，按 `subPath` 切成 `workspace/` 与 `plugins/` 两个子目录**：副本之间必须共享，否则同一用户的任务落到不同副本时会"间歇性失忆"。**前提是集群有支持 ReadWriteMany 的 provisioner** —— kind 自带的 local-path 只支持 RWO，本地验证前要装 nfs-server-provisioner（或把那两处临时改回 `emptyDir`，但此时必须去掉 `subPath`），否则挂它的 Pod 会一直 Pending。子目录要先按 §3d 预建，否则 Pod 卡在 `ContainerCreating`。
2. **内置技能在 K8s 下默认为空**：`builtin-skills` 是 `emptyDir`，引擎启动会告警且技能列表不含内置技能。要内置技能就把该卷换成 hostPath / PVC 指向仓库的 `market/skills`。
3. **前端 resolver 已解决**：K8s 版配置 `deploy/k8s/frontend-nginx.conf` 的 `resolver` 指向 kube-dns（默认 10.96.0.10），不再是 Docker 内嵌 DNS。
4. **Redis/PG 仍是单点**（`docs/deployment-multi-instance.md` §10 自认）：本地验证不影响，评估"真企业化"时必须把它们高可用化——这比引入 MQ 的收益高得多。
5. **持久 shell 不跨实例**（`app/tools/terminal.py:198,212`）：多副本轮转时 shell 的 cwd/env/后台进程会丢，工具描述承诺的"同 session 持久"只在单副本成立。
6. **`/metrics` 走白名单**：引擎指标端点无需令牌，生产请用 NetworkPolicy 限制来源。
7. **TLS**：本清单的 frontend 只提供 HTTP。生产请用 Ingress / 外部 LB 终结 TLS（配置示例见 `deploy/nginx/tls.conf.example`）。

## 7. 清理

```powershell
kind delete cluster --name chiron
```
