# 注册邮箱判重布隆过滤器（设计说明）

> 目标：消除注册热路径上那次昂贵的「邮箱是否已存在」DB 判重。
> 原则：布隆过滤器**只做快速否定**，唯一性的真相源始终是数据库唯一索引。

## 1. 问题：注册热路径上的一次全表扫描

`internal/api/auth.go` 的 `Register` 原先在事务内执行：

```sql
SELECT COUNT(*) FROM users WHERE email = $1 AND tenant_id = $2
```

而 `users` 表（基线见 `migrations/sql/init.sql`）在 `email` 上**没有任何索引** —— 唯一相关的
索引是 `ix_users_tenant_created (tenant_id, created_at)`，对上面的查询无用。因此每次注册都会
触发一次**全表扫描**，代价随 `users` 行数线性增长；它还在事务内、位于人机验证之后，属于
每个注册请求都要支付的固定成本。

## 2. 为什么是布隆过滤器，以及它不能做什么

布隆过滤器的语义是单向的：

| 判定 | 含义 |
|---|---|
| `MightExist == false` | 该元素**一定**没被加入过（无假阴性，构造保证） |
| `MightExist == true` | 可能加入过（存在假阳性） |

所以它可以安全地承担「快速否定」：命中 `false` 时跳过 DB 判重。反之**必须**回落查库。
这也意味着布隆过滤器绝不能作为唯一性的真相源 —— 任何"漏记"都必须由别处的强约束兜底。

## 3. 分层设计

1. **真相源**：`users(tenant_id, email)` 唯一索引（迁移 `0002_users_tenant_email_unique`）。
   并发注册、旁路写入、过滤器同步滞后，最终都由它拦截。
2. **前置否定**：进程内布隆过滤器（`internal/bloom`），按租户分片
   （`internal/api/user_email_bloom.go`）。
   - 只在 `MightExist == false` 时跳过判重，否则回落查库；
   - **预热完成前**一律回落（`ready == false` ⇒ `MightExist` 恒为 `true`）；
   - 注册成功后 `Add` 并通过 Redis pub/sub 广播给其他副本（尽力而为）。

### 为什么按租户分片

判重查询带 `tenant_id`，过滤器必须同粒度：全局单过滤器会把"租户 A 用过 `x@y.com`"变成
租户 B 的假阳性，白白丢掉优化机会。分片键为 `tenantID + NUL + email`，避免 `("a","bc")`
与 `("ab","c")` 这类拼接歧义。

### 预热

启动时后台异步两遍扫描（`userEmailBloom.Warmup`）：

1. `SELECT tenant_id, COUNT(*) ... GROUP BY tenant_id` → 按实际规模给每个分片定容量
   （留 1.5 倍余量，避免所有分片都按最大容量吃内存）；
2. 按 `id` 键集分页 `SELECT id, tenant_id, email ...` 填充（避免大表上的 `OFFSET` 退化）。

任一步失败都**不**标记 `ready`，保持保守回落。预热不阻塞服务启动。

### 跨实例同步

`broadcastUserEmailBloomAdd` 向 Redis channel（`db.RedisKey("users:email:bloom:sync")`）
发布 `tenantID \t email`，其他实例的订阅者 `Add` 之。漏收只让该实例回落 DB，不影响正确性。
形态与既有的 `StartBlacklistPubSub` 一致：一个 goroutine + 一条订阅。

## 4. 哈希与参数

- 位长取 2 的幂 ⇒ 取模退化为按位与；第二个哈希强制为奇数 ⇒ 与 2^k 互质，满足
  double hashing（Kirsch–Mitzenmacher）成立的前提。
- 用 `crypto/sha256` 的单次摘要导出两个 64 位哈希，而非 `hash/maphash`：后者带进程随机种子，
  **跨副本不可复现** —— 而多副本必须对同一邮箱给出相同判定。
- 参数：`m = ceil(-n·ln p / (ln2)²)`、`k = round((m/n)·ln2)`。

内存量级：`p = 0.001` 时约 **1.44 n bits ≈ 0.18 n 字节**。10 万邮箱的租户约 18 KB。

## 5. 失败模式（全部退化为"多查一次库"）

| 情形 | 后果 |
|---|---|
| 预热未完成 | `MightExist` 恒 `true` ⇒ 全部回落 DB |
| 预热失败 | 不标记 `ready` ⇒ 全部回落 DB |
| 广播丢失 / 订阅断开 | 该实例对漏收的邮箱回落 DB |
| 实例重启 | 重新预热；期间回落 DB |
| 过滤器假阳性 | 回落 DB，结果仍正确 |
| 过滤器假阴性 | 不可能（构造保证） |
| 唯一索引冲突（兜底触发） | `INSERT` 报 23505 ⇒ 返回与"邮箱已存在"同形的模糊提示，而非 500 |

最后一行是这套设计的关键保险：即便前三行同时发生，也不会写出重复邮箱。

## 6. 配置与运维

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `EMAIL_BLOOM_FILTER_ENABLED` | `true` | 关闭后注册流程始终回落 DB 判重 |
| `EMAIL_BLOOM_CAPACITY` | `100000` | 每个租户分片的默认预期容量（预热时按实际数量重估） |
| `EMAIL_BLOOM_FP_RATE` | `0.001` | 目标误报率，越低误判越少、内存越大 |

迁移：`alembic upgrade head`（新增 `0002_users_tenant_email_unique`）。若库中已有重复的
`(tenant_id, email)`，迁移会**显式失败并列出冲突行** —— 合并/保留哪个账号属于业务决策，
须人工处理后再重跑，不会静默删数据。

观测：`userEmailBloom.Stats()` 暴露 `Ready / Shards / SkippedDBLookups / FellbackDBLookups`；
预热完成时会打印一行带 `entries`、`shards`、`duration` 的结构化日志。

## 7. 相关代码

| 关注点 | 位置 |
|---|---|
| 布隆过滤器核心 | `internal/bloom/bloom.go` |
| 分片 / 预热 / 广播 | `internal/api/user_email_bloom.go` |
| 注册链路集成 | `internal/api/auth.go`（`Register`） |
| 装配与生命周期 | `internal/api/gateway_router.go` |
| 配置项 | `config/config.go` |
| 唯一索引迁移 | `migrations/versions/0002_users_tenant_email_unique.py` |
