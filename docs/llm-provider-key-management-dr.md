# DR：LLM Provider 密钥管理（集中派）

> **溯源（2026-10-08）**：本文件**原先缺失** —— 2 处引用它（`internal/api/llm_keys.go` 的文件头写
> "集中派，DR：…"，`python-engine/app/gateway/key_ring.py` 写"同接口即可平滑切换（见 …）"）。
> 现按**代码与注释**重建，**只写代码能证明的内容**；与代码冲突时以代码为准。
> 门禁：`python scripts/check_doc_links.py` 拦住"引用了不存在的文档"这类断链。

## 1. 决策

**LLM provider 的密钥由网关集中管理**，引擎侧只做"镜像 + 取用 + 上报失败"，不各自持有权威副本。

三层落点：

| 层 | 位置 | 说明 |
|---|---|---|
| 权威 | `llm_provider_keys` 表 | **密文**（AES-256-GCM，密钥由 `APP_SECRET` 派生；`internal/settings`） |
| 运行时 | Redis keyset `llm:keys:{provider}` | hash：`field = key_hash[:12]` → JSON `{k, s}`（权威）＋引擎瞬态 `{c, u, f, ft}` |
| 同步 | `llm:keys:ver`（版本号）+ `llm:keys:changed`（PUBLISH） | 变更广播，引擎 KeyRing 每次取 key 直读镜像收敛 |

网关**自己处理** `/v1/admin/api-keys`（不再把请求转发给引擎）—— 密钥的增删改查只有一个入口。

## 2. 为什么集中（而不是各自放在引擎 env）

分散放 env 会同时丢掉三件事，而这三件正是集中派用代码换来的：

- **统一轮换**：改一处（DB + keyset 整体重建 + 广播），引擎每次取 key 直读 Redis 镜像 ⇒ **即时**收敛（不再有本地 freshness 缓存的 3s 延迟）；
- **统一熔断**：失败计数与冷却都写进 Redis 镜像的字段（V2 单键 Lua 原子"读-改-写"），坏 key 被**全局**软停用，而不是每个实例各自重试一遍；
- **统一审计与权限**：密钥操作走管理端接口（读写权限中间件），有明确的操作面。

## 3. 键身份

`key_hash = sha256(provider + ":" + key)`（hex）；Redis 字段名取**前 12 位**。
用哈希而不是明文做字段名：日志、keyset 键名与 hash 字段名都不泄露密钥本身（明文只在字段值 `k` 里，见 §9）。

## 4. 状态机与字段语义（V2）

字段值是 JSON，分两类，**职责严格分离**：

- **权威状态 `s`（只有网关/管理员能写）**：`active` · `rate_limited` · `circuit_open`
  （`llmKeysStatuses`）。由 `syncProviderKeyset` 从 DB 写入，引擎**从不改 `s`** ——
  管理员手动停用/标注限流因此是硬的、不会被引擎覆盖或"自愈"绕过。
- **引擎瞬态（只有引擎能写，且整体同步会清掉）**：
  - `c` = 冷却到期时间戳（epoch 秒）。**自动**熔断就是给一把 `active` key 盖一个 `c`，
    到期即恢复（不翻 `s`）；
  - `u` = 最近被取用时间戳，用于 LRU 原子轮转（§5）；
  - `f` / `ft` = 窗口内失败计数与其窗口起点，用于达阈值判定（窗口用时间戳模拟，不依赖键 TTL）。

**可用谓词**（`get_key` 的 Lua 与 `active_keys` 完全一致）：`s == "active"` 且（`c` 缺省为 0 或 `c <= now`）。
⇒ 非 `active`（管理员态）永远不被取；`active` 且冷却已到期 ⇒ 立即可用（自愈）。

## 5. 引擎侧 KeyRing（V2：Redis Lua 原子取 key）

`python-engine/app/gateway/key_ring.py`。选 key 的路径不再有本地"环 + freshness 缓存"，
**Redis 镜像就是每次取的唯一仲裁**；所有读写都在服务端一段 EVAL 里原子完成。

- **Cluster 安全约束**：每段 EVAL **只碰一个键**（该 provider 的 `llm:keys:{provider}` HASH）。
  网关按多 master 路由这些键（`internal/api/model_discovery.go` 的"各 master"注释），所以 LRU 游标与
  失败计数**只能写进 HASH 的字段 payload**，不能用独立的 `llm:rr:` / `llm:fail:` 键
  （那会与 HASH 落不同 slot、触发 `CROSSSLOT`）。这也是删掉 `llm:fail:{provider}:{digest}` 独立键的原因。
- **取用 `get_key`（A1 修复）**：Lua `HGETALL` 后用**最小 `u`（LRU）**在可用 key 里选一把，
  选完立刻把它的 `u` 写成当前时间 —— 单段 EVAL 原子完成"选 + 记账"。
  这修掉了 V1 的 TOCTOU（V1 是 `active_keys()` 放锁→取队首→再拿锁挪队尾，读改之间有 await 间隙，
  同实例并发协程会都选中同一把；各实例本地环又各转各的）。V2 跨实例、跨协程都是**严格轮转**。
- **失败上报 `report_failure`（A2 修复）**：单段 EVAL 原子地——
  ① 窗口内 `f+1`（`now-ft > FAIL_WINDOW(60s)` 则窗口复位、`f` 从 1 重算）；
  ② 未达阈值盖**软冷却** `c = now + COOLDOWN_SOFT(30s)`；达 `FAIL_THRESHOLD(5)` 升级为
     **硬冷却** `c = now + COOLDOWN_HARD(60s)` 并复位窗口；
  ③ `until_ts` 只会**变长不会变短**（绝不把一次软冷却盖到一个更长的既有冷却上）。
  冷却写在共享镜像 ⇒ **跨实例立即可见**（V1 的进程内 30s 冷却别人看不见，且共享停用要等 3s freshness 才传播）；
  INCR 与窗口/TTL 在同一 EVAL 里 ⇒ **不再有"崩溃留下无 TTL、永不复位的孤儿计数键"的 bug**；
  `c` 是到期时间戳、`s` 保持 `active` ⇒ 熔断**到期自愈**（V1 把 `s` 写成 `circuit_open` 而放行只看
  `s==active`，`c` 只用来排除、从不用来重新放行 ⇒ 一次限流风暴把好 key **永久退役**，直到管理员重新同步）。
- **env 兜底**：`redis` 为 `None`、EVAL 抛错或镜像该 provider 为空时，退回 env 种子
  （`settings.xxx_api_key`），用进程内 LRU 轮转 + 软冷却，与 Redis 故障降级语义一致（§9）。
- **热路径优先级（V2 行为变更）**：取 key 时**Redis 镜像优先于 env 种子**（V1 是 env 优先）。
  既然做了集中原子池，管理端配的 key 就应参与轮换/熔断，而不是被一处 env 种子旁路掉。

### 5.1 多协议变体的命名回退（一个真实踩坑）

同产品的多协议变体（如 `X-anthropic`）在 keyset 里**按 preset id 分开存**，但**共享同一个上游 key**。
只配了一处时，另一条协议线取不到 key，而 `_resolve_client` 取不到会**静默回退到 placeholder key** ⇒
拿假 key 打上游 ⇒ 上游返回 `401 Invalid API key`（**而那个 key 其实是好的**）。

因此 `KeyRing._sources` 按命名约定回退：**`X-anthropic` 取不到就读 `X`**（取与上报都逐源尝试：
`X-anthropic` 的字段 EVAL 返 `-1` 未命中 ⇒ 换 `X` 的 HASH 重试，这也顺带修掉了 V1 把失败
记到并不持有该 key 的那份 HASH 上的错位）。

## 6. V1 → V2（已完成，2026-10-18）

V1 是"本地环 + 轮询"；V2 已把本类换成 **Redis Lua 原子取 key**（原子 LRU 选择 + 时间窗自愈熔断 +
跨实例共享冷却）。`active_keys` / `get_key` / `report_failure` **同接口**未变，对上层（providers）平滑。
V2 相对 V1 的两个刻意行为变更见 §5（热路径 Redis 优先于 env；自动停用改为 `c` 时间窗而非翻 `s`）。

原子性、LRU 轮转、时间窗自愈、阈值升级、跨实例协同等**只能在真 Redis 上验**，故有
`python-engine/tests/test_key_ring_live.py`（`-m live`：CI 的 real-stack job 带真 Redis 会跑，
无 Redis 环境即时 skip）。纯 Python 侧（env 兜底轮转、可用谓词、`-anthropic` 源回退、EVAL 抛错降级）
在 `python-engine/tests/test_key_ring.py`（无需 Redis）。

## 7. 管理端接口

| 方法 | 路径 | 处理函数 |
|---|---|---|
| `GET` | `/v1/admin/api-keys` | `ListLLMKeys` |
| `POST` | `/v1/admin/api-keys` | `AddLLMKey` |
| `PUT` | `/v1/admin/api-keys/{id}` | `UpdateLLMKeyStatus` |
| `DELETE` | `/v1/admin/api-keys/{id}` | `DeleteLLMKey` |

写操作后会**重建该 provider 的 keyset 并广播变更**（`syncProviderKeyset`）；
**Redis 不可用时只返回 nil**（DB 仍是权威，引擎退化为 env 种子），由调用方按需告警。

「重建」是**整体替换**而非增量写：一次原子 Lua 脚本先 `DEL` 该 hash 再逐对 `HSET`
当前 DB 里解得开的每一把 key（`llmReplaceKeyset`）。之所以不能用 HMSET 增量——
引擎 `KeyRing._refresh` 用 `HGETALL` 把整份 hash 当作「当前可用 key」轮询，一条 key 从
DB 删除后只要该 provider 还剩别的 key，它的指纹字段就会被增量写残留下来，等于**把一把
已撤销的 key 一直服务下去**（拿去打上游）。全部解密失败或该 provider 已无 key ⇒ 结果为空
⇒ 陈旧明文不再被服务。

整体替换只重建 `{k, s}` ⇒ 会顺带清掉引擎写在同一份 hash 里的瞬态 `{c, u, f, ft}`
（冷却、LRU 游标、失败窗口）。这意味着**一次管理员同步会重置该 provider 的冷却与轮转顺序** ——
管理员操作罕见、且 DB 才是权威、冷却本就到期自愈，故判定可接受（与 §5 的熔断自愈性一致，不是待修 bug）。

## 8. 实现落点

| 环节 | 位置 |
|---|---|
| 网关侧增删改查 + keyset 重建 + 广播 | `internal/api/llm_keys.go`（`syncProviderKeyset` / `llmKeyHash` / `llmEnc`） |
| 密文存储 | `internal/settings`（`APP_SECRET` 派生的 AEAD） |
| 引擎侧密钥环 | `python-engine/app/gateway/key_ring.py`（`KeyRing.active_keys` / `get_key` / `report_failure`） |
| 管理端路由 | `internal/api/routes_admin.go`（`/v1/admin/api-keys`，读写权限中间件） |

## 9. 边界与已知取舍

- **keyset 里是明文 key**（`{"k": <明文>, ...}`）：DB 层加密，但 Redis 侧必须按敏感数据处理
  （访问控制、不落日志）。这是"引擎要能直接用 key"与"密文存储"之间的取舍；瞬态 `c/u/f/ft` 不含密钥、非敏感；
- **Redis 不可用 ⇒ 无 keyset 同步**：引擎退回 env 种子（单机可用，但失去集中轮换/熔断）；
- **`rate_limited` / `circuit_open` 都是纯人工态**（管理端在 `active/rate_limited/circuit_open` 间切换，
  引擎从不写 `s`）；**自动**停用不再翻 `s`，而是给 `active` key 盖一个**到期自愈**的时间窗 `c`（§4/§5）——
  这是 V2 修掉的 A2：V1 达阈值把 `s` 写成 `circuit_open` 且放行只看 `active`，冷却时间戳只能"再排除"
  不能"重新放行"，等于一次限流风暴把暂时坏的好 key **永久退役**；
- **V2（Redis Lua 原子取 key）已实现**：跨实例/跨协程原子 LRU 严格轮转（A1）、时间窗自愈熔断 +
  跨实例共享冷却 + 原子窗口化失败计数（A2）、取消本地 freshness 双状态源（B1）。
  **仍未做**的是**每 key 权重**（V2 是等权 LRU，非加权）与 LiteLLM 式"换一把 key 重试"预算 ——
  二者都需要新的产品输入（现无每 key 权重配置、重试预算属策略决策），不属本轮 §5.2 缺陷集。
