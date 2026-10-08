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
| 运行时 | Redis keyset `llm:keys:{provider}` | hash：`field = key_hash[:12]` → JSON `{k, s, c}` |
| 同步 | `llm:keys:ver`（版本号）+ `llm:keys:changed`（PUBLISH） | 变更广播，引擎 KeyRing 据此收敛 |

网关**自己处理** `/v1/admin/api-keys`（不再把请求转发给引擎）—— 密钥的增删改查只有一个入口。

## 2. 为什么集中（而不是各自放在引擎 env）

分散放 env 会同时丢掉三件事，而这三件正是集中派用代码换来的：

- **统一轮换**：改一处（DB + keyset 重建 + 广播），所有引擎实例在 `FRESHNESS` 内收敛；
- **统一熔断**：失败计数在 **Redis 共享**，坏 key 被全局停用，而不是每个实例各自重试一遍；
- **统一审计与权限**：密钥操作走管理端接口（读写权限中间件），有明确的操作面。

## 3. 键身份

`key_hash = sha256(provider + ":" + key)`（hex）；Redis 字段名取**前 12 位**。
用哈希而不是明文做字段名：日志、keyset 键名与失败计数键都不泄露密钥本身。

## 4. 状态机

`status` 取值（`llmKeysStatuses`）：**`active`** · **`rate_limited`** · **`circuit_open`**。
另有 `c`（cooldown 到期时间戳）：到期前该 key 不被取用。

## 5. 引擎侧 KeyRing（V1）

`python-engine/app/gateway/key_ring.py`。语义：

- **权威镜像 + env 兜底**：以 Redis keyset 为准；keyset 为空/Redis 不可用时**只用 env 种子**
  （`settings.xxx_api_key`，单机降级）；
- **按 freshness 刷新**：`FRESHNESS = 3s`（管理端变更在此延迟内收敛到各实例），刷新是**访问时惰性**做；
- **取用**：`active_keys(provider)` 过滤掉"非 `active`"与"仍在冷却期"的项；`get_key` 在 V1 里是
  **轮询**（取队首、再移到队尾）；
- **失败上报**：`report_failure(provider, key)` 做两件事 ——
  ① **本地冷却** `LOCAL_COOLDOWN = 30s`（单实例侧软停用，避免反复打同一个坏 key）；
  ② **Redis 共享计数** `llm:fail:{provider}:{digest}`（窗口 `FAIL_WINDOW = 60s`），
  达 `FAIL_THRESHOLD = 5` 次即把 keyset 字段写成 `circuit_open` + 冷却到期时间（**全局停用**）。

### 5.1 多协议变体的命名回退（一个真实踩坑）

同产品的多协议变体（如 `X-anthropic`）在 keyset 里**按 preset id 分开存**，但**共享同一个上游 key**。
只配了一处时，另一条协议线取不到 key，而 `_resolve_client` 取不到会**静默回退到 placeholder key** ⇒
拿假 key 打上游 ⇒ 上游返回 `401 Invalid API key`（**而那个 key 其实是好的**）。

因此 `_refresh` 按命名约定回退：**`X-anthropic` 取不到就读 `X`**。

## 6. V1 → V2（预留，接口不变）

V1 是"本地环 + 轮询"；V2 计划把本类替换为 **Redis Lua 原子取 key**（原子加权选择）。
只要保持 `active_keys` / `report_failure` **同接口**，替换对上层是平滑的 —— 这正是
`python-engine/app/gateway/key_ring.py` 文件头所引用的那句"同接口即可平滑切换"。

## 7. 管理端接口

| 方法 | 路径 | 处理函数 |
|---|---|---|
| `GET` | `/v1/admin/api-keys` | `ListLLMKeys` |
| `POST` | `/v1/admin/api-keys` | `AddLLMKey` |
| `PUT` | `/v1/admin/api-keys/{id}` | `UpdateLLMKeyStatus` |
| `DELETE` | `/v1/admin/api-keys/{id}` | `DeleteLLMKey` |

写操作后会**重建该 provider 的 keyset 并广播变更**（`syncProviderKeyset`）；
**Redis 不可用时只返回 nil**（DB 仍是权威，引擎退化为 env 种子），由调用方按需告警。

## 8. 实现落点

| 环节 | 位置 |
|---|---|
| 网关侧增删改查 + keyset 重建 + 广播 | `internal/api/llm_keys.go`（`syncProviderKeyset` / `llmKeyHash` / `llmEnc`） |
| 密文存储 | `internal/settings`（`APP_SECRET` 派生的 AEAD） |
| 引擎侧密钥环 | `python-engine/app/gateway/key_ring.py`（`KeyRing.active_keys` / `get_key` / `report_failure`） |
| 管理端路由 | `internal/api/routes_admin.go`（`/v1/admin/api-keys`，读写权限中间件） |

## 9. 边界与已知取舍

- **keyset 里是明文 key**（`{"k": <明文>, ...}`）：DB 层加密，但 Redis 侧必须按敏感数据处理
  （访问控制、不落日志）。这是"引擎要能直接用 key"与"密文存储"之间的取舍；
- **Redis 不可用 ⇒ 无 keyset 同步**：引擎退回 env 种子（单机可用，但失去集中轮换/熔断）；
- `rate_limited` 是**人工**标注的状态（管理端在 active / rate_limited 之间切换）；**自动**停用走的是
  `circuit_open`（§5）；
- V2（Lua 原子取 key）尚未实现 —— 当前 V1 的"取队首再移到队尾"在多实例并发下是**近似**轮询，
  不做全局公平保证。
