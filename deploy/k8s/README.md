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
kind load docker-image chiron/gateway:dev chiron/python-engine:dev --name chiron
```

## 3. 注入密钥（从 `.env` 取，别写进 YAML）

```powershell
function EnvVal($n) { ((Get-Content .env | Where-Object { $_ -match "^$n=" } | Select-Object -First 1) -split '=',2)[1].Trim().Trim('"') }
$dsn = (EnvVal POSTGRES_DSN) -replace '@localhost', '@host.docker.internal'
kubectl -n chiron create secret generic chiron-secrets `
  --from-literal=APP_SECRET="$(EnvVal APP_SECRET)" `
  --from-literal=JWT_SECRET="$(EnvVal JWT_SECRET)" `
  --from-literal=INTERNAL_TOKEN="$(EnvVal INTERNAL_TOKEN)" `
  --from-literal=POSTGRES_DSN="$dsn" `
  --from-literal=METRICS_TOKEN="$(EnvVal METRICS_TOKEN)" `
  --dry-run=client -o yaml | kubectl apply -f - --namespace chiron
```

> 顺序：先 `kubectl apply -f deploy/k8s/chiron.yaml`（含 Namespace/ConfigMap/Deployment）
> 之后再执行上面的 secret 注入，或先 `kubectl create namespace chiron`。

## 4. 部署并观察

```powershell
kubectl apply -f deploy/k8s/chiron.yaml
kubectl -n chiron get pods -w          # 期望 3 gateway + 3 python-engine 全 Ready
```

接口从宿主直连：`http://localhost:18080`（kind-config 把 30080 映射到 18080）。

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

1. **多副本必须共享 sandbox/plugins 卷**：本 YAML 用 `emptyDir`（仅够验编排属性）。kind 的 local-path 只支持 RWO，要真多副本得配 nfs-server-provisioner 或换云盘 RWX。
2. **前端容器要改 resolver**：`frontend-vue/nginx.conf:11` 已注明——K8s 下把 `resolver` 换成 kube-dns 的 ClusterIP，否则上游解析不到 Service。
3. **Redis/PG 仍是单点**（`docs/deployment-multi-instance.md:153` 自认）：本地验证不影响，评估"真企业化"时必须把它们高可用化——这比引入 MQ 的收益高得多。
4. **持久 shell 不跨实例**（`app/tools/terminal.py:198,212`）：多副本轮转时 shell 的 cwd/env/后台进程会丢，工具描述承诺的"同 session 持久"只在单副本成立。
5. **`/metrics` 走白名单**：引擎指标端点无需令牌，生产请用 NetworkPolicy 限制来源。

## 7. 清理

```powershell
kind delete cluster --name chiron
```
