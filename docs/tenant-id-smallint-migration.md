# tenant_id 统一为 smallint（设计说明）

## 1. 动机

`tenant_id` 原先在 84 张表中混用 `varchar(36)` / `varchar(50)` / `varchar(64)`，且 44 列里有 36 列可空。
它几乎出现在每张业务表的复合索引里（`(tenant_id, ...)`）：37 字节的 UUID 让 B-tree 每页只能容纳约 200 个 key，
改为 2 字节 `smallint` 后每页约可容纳 4000 个；比较也从走 collation 的字符串比较变成机器整数比较。

## 2. 变更

- `tenants.id`：`varchar(36)` → `smallserial`
- 44 个 `tenant_id` 列：`varchar(36/50/64)` → `smallint NOT NULL`（三套长度归一，并补 `NOT NULL`）
- Go：`DefaultTenantID int16 = 1`、`Claims.TenantID int16`、33 个结构体字段与 helper 签名改为 `int16`；
  新增 `tenantStr(int16) string` / `parseTenantID(string) int16` 两个边界 helper
- Python：`get_tenant_id() -> str`（值规约为数字串，未绑定返回 `""`；绝大多数调用方是“格式化”场景：沙箱路径拼接、审计条目字段、`kb_access_allowed` 比较、`SkillStore` 目录段），另新增 `get_tenant_id_int() -> int` 供 SQL 参数与 Pydantic 字段使用；边界解析用 `to_tenant_int()`
- 前端：TS 类型 `tenant_id: string` → `number`（后端回传 `number`；表单文本字段与 API 之间显式 `String()` / `Number()` 互转）

## 3. 正确性边界

- SQL 参数必须是 `int16`（pgx）或 `int`（asyncpg），否则 `smallint` 列比较可能不走索引；
- HTTP / JSON / Redis 中的 `tenant_id` 允许是字符串，入口处用 `parseTenantID` / `to_tenant_int` 宽容解析；
- `tenants.id` 改为 `smallserial` 后，seed 显式插入 `id=1` 需要 `setval` 推进序列（已在 `EnsureDefaultTenant` 处理）。

## 4. 部署注意

- 本次是**破坏性 schema 变更**（用户确认可清库重建）；迁移链保持单 head `0003_system_settings_key_unique`；
- 已签发的 JWT 中 `tenant_id` 为旧 UUID 的 token 将因类型变化而失效，用户需重新登录；
- 存量库若存在重复 `(tenant_id, email)`，迁移 `0002` 会 fail-fast 而非静默删数据。

## 5. 后续收到的遗留（均已处理）

- Python 侧函数参数上的 `tenant_id: str` 类型注解（约 35 处，非 Pydantic 字段）**仍保持 `str`** —— 它们在运行时不影响行为，且大多是上一行所述“格式化”消费方，不再变动；
- `users.storage_id`（零引用死列）**已删除**；`system_settings` 的 `unique(category, key)` **已补**（迁移 `0003`），两项均已在基线与 `init.sql` 同步；
- 前端 `tenant_id` 改型后曾遗留 8 处类型错误（`stores/auth.ts` 两处仍赋 `''`、auth spec 的 mock、`CostCenterView` / `MarketView` 的表单互转、`CallChainTimeline` 的 `tenantId` prop）—— 它们使 `npm run build` 前置的 `vue-tsc -b` **失败**；现均已修复（`vue-tsc -b` 通过、vitest 631 项全绿）。
