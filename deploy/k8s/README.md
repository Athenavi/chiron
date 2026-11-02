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

### 生产：把 `:dev` 换成 digest

推完镜像后逐个取 digest（`-f '{{index .RepoDigests 0}}'` 给的是 `repo@sha256:…` 整串，
直接粘进 `image:` 即可 —— **不要**保留 tag 写成 `repo:dev@sha256:…`，k8s 只认
`@sha256` 那部分，但混写会让人误以为 tag 也参与固定）：

```powershell
$reg = 'registry.example.com/chiron'
docker push "$reg/gateway:1.4.0"
docker push "$reg/python-engine:1.4.0"
docker push "$reg/frontend:1.4.0"
docker push "$reg/migrations:1.4.0"
# ⚠ python-engine 与 sandbox-service 用**同一个** digest，见下方说明
docker inspect -f '{{index .RepoDigests 0}}' "$reg/gateway:1.4.0"
docker inspect -f '{{index .RepoDigests 0}}' "$reg/python-engine:1.4.0"
docker inspect -f '{{index .RepoDigests 0}}' "$reg/frontend:1.4.0"
docker inspect -f '{{index .RepoDigests 0}}' "$reg/migrations:1.4.0"
```

然后替换 `chiron.yaml` 里全部 **4 处** `chiron/*:dev`（gateway、python-engine、
sandbox-service、frontend）与 `migrate-job.yaml` 里 **1 处** `chiron/migrations:dev`。
用下面的命令自证没有漏网的浮动标签，输出应为空：

```powershell
# 必须只匹配 `image:` 行：文件头部注释里也写着 chiron/gateway:dev 等字样，
# 直接搜 'chiron/[a-z-]+:dev' 会把注释一起选中，替换完了也照样非空。
Select-String -Path deploy/k8s/chiron.yaml,deploy/k8s/migrate-job.yaml -Pattern '^\s*image:\s*\S*:dev'
```

顺便确认 5 处都已变成 digest（应输出 5 行 `@sha256:`）：

```powershell
Select-String -Path deploy/k8s/chiron.yaml,deploy/k8s/migrate-job.yaml -Pattern '^\s*image:\s*.*@sha256:'
```

> **`sandbox-service` 必须与 `python-engine` 用同一个 digest。** 两者跑的是同一份
> `app/tools/sandbox.py`（逃逸正则 + 可执行白名单 + rlimit）。pin 到不同版本时会出现
> "引擎侧放行、沙箱子进程侧拒绝"（或反之）的随机 `[blocked]`，而且两边镜像字符串看起来
> 都很正常，很难定位。清单里两者共用 `chiron/python-engine:dev` 就是为了让这条约束显式。

> 换成 digest 之后 `imagePullPolicy: IfNotPresent` 是**正确**取值（内容不可变，无需重拉）。
> 不要因为"想强制刷新"而改回 `Always`：那只会让每个 Pod 启动都多一次 registry 往返，
> 并在 registry 抖动时把可用副本一起带走。

## 3. 注入密钥（从 `.env` 取，别写进 YAML）

```powershell
function EnvVal($n) { ((Get-Content .env | Where-Object { $_ -match "^$n=" } | Select-Object -First 1) -split '=',2)[1].Trim().Trim('"') }
# 直连 PG 的 DSN —— 迁移 Job 用（DDL 必须走会话级语义，不能经 transaction 池）。
$dsnDirect = (EnvVal POSTGRES_DSN) -replace '@localhost', '@host.docker.internal'
# 应用侧 DSN。**默认直连**；接入 pgbouncer 后改成指向 pgbouncer:6432（见下方方框），
# 且必须带 default_query_exec_mode=exec —— transaction 池下 pgx 的 prepared statement 会错配。
$dsnApp = $dsnDirect
# 引擎只认 REDIS_URL（整串），网关只认 REDIS_ADDR + REDIS_PASSWORD。
# 有密码时 REDIS_URL 里的密码必须做一次 URL 编码，否则含 `@ : /` 会被解析错。
$redisPwd = EnvVal REDIS_PASSWORD
$redisUrl = if ($redisPwd) { "redis://:$([uri]::EscapeDataString($redisPwd))@host.docker.internal:6379/0" } else { "redis://host.docker.internal:6379/0" }
kubectl -n chiron create secret generic chiron-secrets `
  --from-literal=APP_SECRET="$(EnvVal APP_SECRET)" `
  --from-literal=JWT_SECRET="$(EnvVal JWT_SECRET)" `
  --from-literal=INTERNAL_TOKEN="$(EnvVal INTERNAL_TOKEN)" `
  --from-literal=POSTGRES_DSN="$dsnApp" `
  --from-literal=POSTGRES_DSN_DIRECT="$dsnDirect" `
  --from-literal=REDIS_PASSWORD="$redisPwd" `
  --from-literal=REDIS_URL="$redisUrl" `
  --from-literal=METRICS_TOKEN="$(EnvVal METRICS_TOKEN)" `
  --dry-run=client -o yaml | kubectl apply -f - --namespace chiron
```

> **`POSTGRES_DSN` 与 `POSTGRES_DSN_DIRECT` 是两个不同的 key，别合并**：应用（gateway /
> python-engine）读前者；迁移 Job 读后者并在缺失时回退到前者。两者指向同一个库，
> 只是 host:port 不同。合并成一个 key 的后果见下面「要不要接 pgbouncer」。

> `REDIS_URL` 只放在 Secret 里、ConfigMap 里**刻意没有**同名键：两份同名键会靠
> `envFrom` 的先后顺序隐式覆盖，顺序一调就是"NOAUTH"或"密码进日志"的隐性事故。
>
> `APP_SECRET` 必须 **≥32 字符**（`config.Load` 直接拒绝更短的值），且所有副本同值：
> 网关用它派生 JWT / 内部令牌 / 配置加密密钥。换值等于让所有会话与密文失效。
>
> `REDIS_PASSWORD` 为空时 Redis 是不认证的 —— 本清单的 Redis 在宿主机上，**任何能打到
> 6379 的进程**都能读写队列、会话映射与 `chiron:*` 键（等于绕过租户隔离）。本地 kind 可
> 接受，生产必须给 Redis 设密码并让 `REDIS_PASSWORD` / `REDIS_URL` 同时带上。

### 要不要接 pgbouncer

`chiron.yaml` **默认就部署了 2 个 pgbouncer 副本**，但连接预算那行注释的前提是
"应用经它连 PG"。两者的差距很实在：

| | 应用直连 PG | 应用经 pgbouncer |
|---|---|---|
| HPA 扩到 6 副本时的 PG 连接 | 6×10（网关）+ 6×10（引擎）= **120** | 固定 `default_pool_size` 25 × 2 副本 = **50** |
| PG 默认 `max_connections` = 100 | **超限** ⇒ 新连接被拒（报 `too many clients`） | 安全 |

所以：**只要两个 HPA 允许扩到 5 副本以上（本清单是 6），就必须接**。切换只要把
`POSTGRES_DSN` 改成经 pgbouncer 的形态并重建 Secret：

```powershell
# pgbouncer 的 [databases] 里别再写 host.docker.internal（那只对宿主机成立），
# 改成 k8s 服务名 —— 见 chiron.yaml 的 pgbouncer-config。
$dsnApp = 'postgresql://<user>:<pwd>@pgbouncer:6432/<db>?default_query_exec_mode=exec'
kubectl -n chiron create secret generic chiron-secrets `
  --from-literal=POSTGRES_DSN="$dsnApp" `
  --from-literal=POSTGRES_DSN_DIRECT="$dsnDirect" `
  # …其余 key 与上面一致（create secret 是整份替换，漏写就等于删掉）…
  --dry-run=client -o yaml | kubectl apply -f - --namespace chiron
kubectl -n chiron rollout restart deploy/gateway deploy/python-engine
```

> ⚠ **切换时必须单独创建 `chiron-pgbouncer` Secret**（内容是 `scram-sha-256` 的 userlist）。
> 它**不在** `chiron-secrets` 里：后者被网关/引擎 `envFrom` 整体注入，混在一起等于把
> PG 角色的 SCRAM 密码发给每个应用容器（任意能读环境变量的工具/插件都能取到）。
> 缺这个 Secret 时 pgbouncer Pod 卡在 `ContainerCreating`，应用侧表现为"连不上 PG"：
>
> ```powershell
> # userlist 形如："" "SCRAM-SHA-256$4096:<salt>$<storedkey>:<serverkey>"
> # 生成方式见 deploy/pgbouncer/userlist.example.txt 的头注释（用 psql 取 SCRAM 串）。
> kubectl -n chiron create secret generic chiron-pgbouncer `
>   --from-file=PGBOUNCER_USERLIST=./userlist.txt `
>   --dry-run=client -o yaml | kubectl apply -f - --namespace chiron
> ```
>
> 另外 `pgbouncer-config` 里 `[databases]` 的 host 目前写的是 `host.docker.internal`
> （只对 kind 成立：Pod 跑在 Docker 里，宿主机可达）。真集群要改成 PG 的**集群内地址**
> （Service 名或托管实例的域名），否则 pgbouncer 连不上 PG，全部请求失败。
>
> ⚠ 如果只改了 `POSTGRES_DSN` 而没建 `POSTGRES_DSN_DIRECT`，迁移 Job 会跟着走
> transaction 池 —— 它不会报错，只是**迁移可能半途失败**（advisory lock 失效）。
> 这个顺序陷阱是 `migrate-job.yaml` 头注释里专门写明的。

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
| 1 | **就绪 ≠ 存活** | 临时把 Secret 的 `REDIS_URL` 改成错误值（ConfigMap 里**没有**这个键，见 §3 的说明）→ `kubectl rollout restart deploy/gateway` | Pod 变 **NotReady**（被摘出 Service），但 **RESTARTS 仍为 0**；改回后自动恢复 |
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
8. **`chiron-pgbouncer` 是独立 Secret，漏建会整站不可用**：`PGBOUNCER_USERLIST` 不在 `chiron-secrets` 里（那是给应用容器 `envFrom` 用的，不该拿到 PG 口令）。只在真的把 `POSTGRES_DSN` 指向 `pgbouncer:6432` 时才需要建它，建法见 §3。漏建的表现是 pgbouncer Pod `CreateContainerConfigError`，然后 gateway/engine 连不上 PG —— 两者都是 **fail-closed**（`DEGRADED_MODE` 默认 false），所以现象是"全站 502/5xx"，而不是"降级可用"。
9. **`/v1/internal/*` 在 nginx 层被 404**（`frontend-nginx.conf` 与 `frontend-vue/nginx.conf` 都有一份）。这是刻意的：这些端点能执行任意 SQL、读写 Redis、写存储，只靠 `X-Internal-Token` 一道门。改动 nginx 配置时**不要**为了图方便把这条 `location` 删掉或改成 `proxy_pass`。若将来确实需要浏览器经 nginx 调内部端点，应当先在网关侧为那些端点加上用户级鉴权，而不是撤掉这一层。
10. **PDB 只在 `kubectl drain` / 节点故障时起作用**，不保证滚动升级（那是 `maxUnavailable: 0` 管的）。`pgbouncer`、`sandbox-service`、`sandbox-lb` 的 `minAvailable: 1` 意味着一台节点上同时有这两个副本时，drain 会被 PDB **阻塞**直到副本迁走 —— 这是想要的（它们是 fail-closed 的单点入口），但意味着 `drain --timeout` 要给足，否则会半途放弃。

## 7. 清理

```powershell
kind delete cluster --name chiron
```
