# SaaS 体验与功能规划

**口径**：每条都写「现状（已核实）→ 做法 → 验收」。凡是没读到实现、只有推断的，一律标「待确认」，不当作结论。

## 0. 为什么现在做这件事

工程侧已经不再是瓶颈：`docs/development-roadmap.md` §2 的待办只剩三条技术债（L4-3 时间列类型、S5-(e) 沙箱部署形态、L3-4 巨型文件拆分），全量门禁长期全绿。也就是说，继续加功能之前，"用户能不能顺畅地用起来、看得懂花了多少钱"变成了更划算的投入方向。

## 1. 现状盘点（2026-10-29 复核，已按实现逐条核对）

> 本节初稿写于规划阶段，其中多行已被后续交付改写（曾把已存在的 onboarding、CSV 导出、发票、团队标为「未找到」）。下表是复核后的真实状态；P2 组已全部交付。

| 维度 | 现状（已核实） | 结论 |
|---|---|---|
| 计费模型 | 预付费 credits + **一次性套餐包**（`billing_packages` 目录 / `user_entitlements` 权益快照，迁移 `0009`/`0010`） | 已完成（P2-2） |
| 用量视图 | `GET /v1/billing/usage` 支持 `groupBy=day\|model\|session` + `range=7d\|30d\|90d`，breakdown 有 `usageBreakdownLimit=100` + `truncated` + 窗口函数求总量 | 已完成（P0-1、A2） |
| 账单导出 | `internal/api/billing_export.go` 同步流式 CSV，5 万行上限、366 天跨度、UTF-8 BOM | 已完成（P1-1） |
| 发票 | `invoice_requests`（`0007`）+ 用户申请 + 管理端审核 | 已完成（P1-2） |
| 组织形态 | `tenant_members`（`0008`，复用 `ent_roles`、播种内置角色）+ 成员管理与邀请 | 已完成（P2-1） |
| 身份能力 | OIDC、SMTP、RBAC 均已存在；用户级 API Key 自助（`0006` + `UserApiKeysView.vue`） | 已完成（P1-3） |
| 错误文案 | 三类错误三语齐备，带可执行动作；`retryable=false` 时前端不渲染重试按钮 | 已完成（P0-2、A3） |
| 新用户引导 | `onboarding_handler.go` 三步白名单（试用额度 / 默认模型 / 第一轮对话）+ `user_onboarding`（`0005`）+ 漏斗统计端点 | 已完成（P0-3、A1） |
| 通知中心 | `notifications` + `user_notification_prefs`（`0011`）+ `internal/notify` 包；四类事件（支付成功 / 权益到期 / 配额变更 / 成员加入）+ 站内与邮件双出口 + 去重键幂等 + `NotificationsView.vue` 与顶栏角标 | 已完成（P2-3） |
| 技术债 | L4-3 时间列、S5-(e) 沙箱部署形态、L3-4 巨型文件尾批 | 仍缺（见 §12.5） |

## 2. P0 —— 体验快赢（低风险、直接减少流失）

### P0-1 用量分摊可见

**做法**：`/v1/billing/usage` 增加 `groupBy`（model / endpoint / session）与 `range`（7/30/90 天）参数；前端做可筛选表格 + 趋势图。底层数据来自已有的 `credit_transactions`，不新增写入路径。

**验收**：能回答"上周哪个模型花了多少"；切换分组/时间范围不报错；大范围查询走分页而不是一次全出。

### P0-2 额度与失败的可读化

**做法**：把三类情况彻底区分开——余额不足（去充值）、配额上限（找租户管理员或升级套餐）、速率限制（等 N 秒重试）。文案给**可执行动作**，不只描述状态。

**验收**：三类错误在 zh-CN / en-US / ar 三语齐备（`check-i18n-keys` 缺键 = 0）；每条错误都能点到对应的下一步。

### P0-3 新用户引导

**做法**：注册后三步——领试用额度 → 选默认模型 → 发出第一轮对话。可跳过，完成状态落库，便于统计完成率。

**验收**：新账号登录能走完并成功发出一次对话；跳过不打乱主界面；完成率可查。

## 3. P1 —— 自助与账务

### P1-1 交易明细导出

**做法**：CSV 导出（异步生成 + 下载链接），避免大查询拖死接口。字段：时间 / 事由 / 金额 / 余额快照。

**验收**：导出文件能被 Excel 直接打开；10 万行级别的账号不出现接口超时。

### P1-2 发票申请（工单式，非自动开票）

**做法**：用户填抬头与税号 → 生成申请单 → 管理员审核。不接入第三方开票平台。

**验收**：审核通过后用户能看到状态与金额；重复申请同一笔交易被拦。

### P1-3 用户级 API Key 自助

**做法**：把用户级 key 管理从 `admin/` 挪到用户侧，管理员保留全局视图。作用域最小化。

**验收**：普通用户可自助创建 / 轮换 / 吊销；越权访问他人 key 返回 403。

## 4. P2 —— 结构化新增（需单独立项评审）

### P2-1 团队与成员 ✅ 已完成

> 本节保留规划时的判断，供追溯。**实际已交付**：`migrations/versions/0008_tenant_members.py` + `internal/api/tenant_members_handler.go`，落地方式见 §12.4。

~~这是当前最大的结构性缺口。~~ **不改** `tenant_id` 的 `smallint` 类型——那会波及所有带 `tenant_id` 的表。做法是新增 `tenant_members(tenant_id, user_id, role)` + 邀请表（邮件邀请复用已有 SMTP），角色直接接 `internal/enterprise/rbac.go`。

**风险**：这是多租户数据隔离的地基，一旦设计错，后面所有共享资源（知识库、Agent、插件）都要返工。需单独设计评审。

**验收**：同一 tenant 下多用户可见共享资源；角色可分配；邀请可撤销且过期失效。

### P2-2 一次性套餐包

**做法**（已按 2026-10-28 决策定为一次性，见 §6）：套餐 = 一次性发放的 credits + 权益快照（模型可用范围 / 并发 / 存储配额），到期只让权益失效，**不续期、不扣款、不发周期任务**。落地形态就是一次 `credit_transactions` 入账 + 一份带有效期的权益记录。

**为什么不做订阅**：支付宝/微信的周期扣款需要单独的签约资质，是产品与合规问题而非工程问题；而且周期任务会引入"扣款失败怎么重试""欠费如何处理"一整类状态机。一次性套餐包能用同样的表结构承载，实现量小一个量级。

**验收**：购买后 credits 与权益一次性到位；权益过期后按预期失效并提示；不出现"用不了但没提示"。

### P2-3 通知中心

额度预警、支付成功、配额变更、团队邀请统一进一张通知表，站内 + 邮件两个出口，每类事件有去重键。

**验收**：同一事件不重复推送；用户可标记已读；渠道可单独关闭。

**已完成（2026-10-30）**：`migrations/versions/0011_notifications.py`（`notifications` + `user_notification_prefs`）、`internal/notify` 包（`Notify` 撞 23505 即跳过 / 偏好缺行默认开启 / 未读计数）、四个事件源接入（`ConfirmPayment`、到期预警扫描、`CreateQuota`/`UpdateQuota`、凭邀请码注册）、用户侧六个接口 + `NotificationsView.vue` + 顶栏角标 + 三语文案。「渠道可单独关闭」落为**用户级邮件开关**：站内信始终保留（关掉它会让「用户声称没收到」变成无法查证的口径之争）。

## 5. 明确不建议现在做

- **改 `tenant_id` 主键类型**：破坏性迁移，收益远小于风险。团队能力用关联表实现即可。
- **自动续费订阅**：支付资质问题先于技术问题，等产品确认。
- **SAML / SCIM 全量落地**：有 OIDC 就够覆盖多数企业；等真实企业客户提需求，避免为假想客户写代码。

## 6. 已定决策（2026-10-28）

| 决策 | 结论 | 对规划的影响 |
|---|---|---|
| 首批范围 | **P0 体验快赢三项**（P0-1/P0-2/P0-3） | P2-1 团队能力不进入首批，先拿到可验收的体验改善 |
| 商业模式 | **一次性套餐包**，不做订阅 | P2-2 降级为"额度包 + 权益包"，删掉周期性发放与到期降级，实现量显著缩小 |
| 目标客户 | 个人开发者与团队**两者都要** | 结构必须支持"个人平滑升级为团队"，因此 P2-1 的 `tenant_members` 从第一天就要按"单人也能是 tenant 成员"设计，避免后期改主键 |

### 由决策派生的两条设计约束

1. **一次性套餐包不得引入 `billing_cycle`**：额度包就是一次 `credit_transactions` 入账 + 权益快照，到期只是权益失效，不涉及周期性任务。这样不需要定时扣款、不需要续费提醒、不需要处理"扣款失败"。
2. **个人账号默认自带一个 tenant**：注册时创建 `tenant_members(tenant_id, user_id, 'owner')`，个人使用时这个 tenant 只有一个人；后续邀请成员即是"升级"而非"迁移"。这是唯一能避免 P2-1 返工的做法。

## 7. 首批任务拆分（P0）

按依赖顺序排列，每项都是可独立验收的最小单元。

### 任务 1：用量分摊可见（P0-1）

1. `internal/api/billing_handler.go`：`GetUsage` 增加 `groupBy`（`day`/`model`/`endpoint`/`session`，默认 `day`）与 `range`（`7d`/`30d`/`90d`，默认 `30d`）参数；`groupBy != day` 时走独立查询，保留现有 day 聚合的返回结构不变。
2. 白名单校验参数，非法值返回 400 而不是静默回落。
3. `frontend-vue/src/views/BillingView.vue`：加分组切换 + 时间范围切换，表格分页。
4. 测试：`billing_handler_test.go` 覆盖四种分组 + 非法参数；前端补一条切换不报错的用例。

**验收**：能回答"上周哪个模型花了多少"；非法 `groupBy` 返回 400；`range=90d` 走分页不一次性全出。

### 任务 2：额度与失败可读化（P0-2）

1. 梳理三类错误的实际来源：余额不足（`Deduct` 返回不足）、配额上限（`ent_quota_enforce.go`）、速率限制（`distributed_ratelimit.go`）。
2. `frontend-vue/src/locales/{zh-CN,en-US,ar}/errors.ts`：三语齐备，每条文案带**可执行动作**（去充值 / 找管理员 / 等 N 秒）。
3. 错误响应补上机器可读的 `code` 与可选的 `retry_after`，供前端做差异化跳转。

**验收**：`check-i18n-keys` 缺键 = 0；三类错误点击后分别落到充值页 / 权限说明 / 倒计时重试。

### 任务 3：新用户引导（P0-3）

1. 前端加引导流程：领试用额度 → 选默认模型 → 发出第一轮对话。
2. 完成/跳过状态落库（用户级标记），便于统计完成率。
3. 引导只在首登触发一次，不打断已有用户。

**验收**：新账号能走完并成功发出一次对话；跳过后主界面正常；完成率可查。

## 8. 首批交付后的复核（2026-10-29）

P0 三项已实现，但逐条对验收时发现**四个未闭环点**。以下每条都是读实现得出的，不是推断。

| 验收点 | 状态 | 依据 |
|---|---|---|
| 能回答"上周哪个模型花了多少" | ✅ | `groupBy=model` + `range=7d` 已可用 |
| 非法 `groupBy` 返回 400 | ✅ | 白名单校验，不做静默回落 |
| `range=90d` 走分页而非一次全出 | ⚠️ **部分** | 分页只在前端（`BREAKDOWN_PAGE_SIZE=10`）；`getBreakdownUsage` 的 SQL **没有 LIMIT/OFFSET**，聚合结果仍全量返回 |
| 三类错误三语齐备 | ✅ | `check-i18n-keys` 缺键 0 |
| 三类错误各有可点落点 | ⚠️ **部分** | `insufficient_credits`→`/billing`、`session_busy`→修会话+倒计时；`quota_exceeded` **只有文案，没有可点入口** |
| `retryable` 与 UI 一致 | ⚠️ **不一致** | `MessageItem.vue` 的「重试」按钮无条件渲染，`retryable=false` 的 `quota_exceeded` 也能点 |
| 新账号能走完引导 | ✅ | 后端两步接口 + 前端浮层已接通 |
| 跳过不打乱主界面 | ✅ | 跳过只写 `skipped_at`，不改任何界面状态 |
| **完成率可查** | ❌ **未达成** | `user_onboarding` 只有写路径；全仓检索无任何读取聚合入口 |

顺带核实到两条会改变 P2 判断的事实：

- **`tenant_members` 表不存在**，但 `tenant_invites` 已存在（`code, tenant_id, max_uses, uses, expires_at`）。当前成员关系只靠 `users.tenant_id` —— 邀请码能把你注册进某个 tenant，但注册后没有成员列表与租户内角色。§6 决策 2 的 `tenant_members(tenant_id, user_id, 'owner')` 确实还没落地。
- **通知表、套餐/权益表均不存在**；CSV 导出零实现（`text/csv`、`Content-Disposition: attachment` 全仓无命中）。`credit_transactions` 现有字段（`created_at, reason, amount, balance, turn_id`）已够 P1-1 的「时间/事由/金额/余额快照」。

### 8.1 复核结论已通过 A 组闭环（2026-10-29）

上表四个未闭环点已逐条补齐，**仍有两条口径限制**，不要当成"全绿"：

| 缺口 | 补法 | 遗留限制 |
|---|---|---|
| 完成率不可查 | `GET /v1/admin/onboarding/funnel` + 管理端漏斗卡片；分母取 `user_onboarding` 去重人数 | 存量用户（迁移前注册）无行，永远不计入分母 —— 见 §10.5 的入口取舍 |
| breakdown 全量返回 | `usageBreakdownLimit = 100` + `truncated`/`total_buckets`/`limit`；合计走窗口函数取全量 | SQL 只用编译与前端替身验证，**未在真实 PG 上跑过**；上线前需一条真库冒烟 |
| `quota_exceeded` 无落点 | 「查看用量」→ `/billing`（仓库无支持邮箱/工单，故**不**做"联系管理员"） | 真正的"找管理员"仍缺联系方式来源，见 §10.2 |
| `retryable` 与 UI 不一致 | `errorRetryable` 贯通到 `MessageItem`，`false` 时不渲染重试 | `undefined`（无结论）仍允许重试 —— 这是有意保留的旧行为 |

## 9. 下一步任务（按依赖顺序）

### A 组：补齐首批验收缺口 —— **A1/A2/A3 已全部交付（2026-10-29 复核）**

**A1 引导完成率可查** ✅
`GET /v1/admin/onboarding/funnel` 已注册（`routes_admin.go:55`，挂 `adminReadMW`），handler 为 `onboarding_funnel_handler.go`，按 step 聚合完成/跳过，分母取走过引导的用户数。

**A2 usage breakdown 后端限量** ✅
`usageBreakdownLimit = 100`，响应返回 `truncated` / `limit` / `total_buckets`，总量用 `SUM(...) OVER ()` 在聚合后全量行上求和（窗口函数先于 LIMIT 执行），`COUNT(*) OVER ()` 供 truncated 判定，无需二次查询。

**A3 `quota_exceeded` 的可点落点 + 重试按钮按 `retryable` 渲染** ✅
`MessageItem.vue` 按后端 `retryable` 决定是否渲染重试按钮（:813 注释写明「服务端若显式说 retryable=false，就不该给」），配额耗尽给「查看用量」动作。

### B 组：P1 自助与账务 —— **B1/B2/B3 已全部交付**

**B1 交易明细 CSV 导出** ✅ `internal/api/billing_export.go`：同步流式、`exportMaxRows=50000`、`exportMaxSpanDays=366`、UTF-8 BOM（Excel 直开不乱码）、超限明确报错。
**B2 发票申请（工单式）** ✅ `0007_invoice_requests` + 用户申请 + 管理端审核（`invoice_handler.go`、`InvoiceReviewView`）。
**B3 用户级 API Key 自助** ✅ `0006_api_keys_key_hash_index` + `/v1/api-keys` CRUD + `UserApiKeysView.vue`；与 `views/admin/ApiKeysView.vue`（LLM 供应商密钥池）是两个不同功能。

### C 组：P2 结构化新增

**C1 团队与成员** ✅ 已完成：`0008_tenant_members`（复用 `ent_roles`、播种内置角色、从 `users` 回填）。
**C2 一次性套餐包** ✅ 已完成：`0009_billing_packages` + `0010_seed_billing_packages`，权益快照 + 有效期，无 `billing_cycle`。
**C3 通知中心** ❌ **仍未开工**：全仓无通知表、无迁移、无 handler。这是本规划中唯一剩下的功能缺口，形状见 §14。

### D 组：既有技术债（`docs/development-roadmap.md`）

L4-3 时间列 `timestamp` → `timestamptz`（142 列、72 表，需生产时区语义抽样 + 可达 PG）、S5-(e) 沙箱部署形态（需独立容器环境）、L3-4 巨型文件拆分尾批。无功能收益，放最后。

### 顺序建议（2026-10-29 复核后）

A/B/C1/C2/C3 已全部完成，功能线不再有待办；其余为 D 组技术债，受外部条件阻塞。

## 10. 后续任务的落地细节（2026-10-29 核实）

§9 的 B/C 组只有一句话，真正动手前有几处必须先纠正。以下每条都读到了实现。

### 10.1 三条事实修正（会改变原计划）

**① B3 的对象不是 `ApiKeysView`。** `views/admin/ApiKeysView.vue` → `GET/POST /v1/admin/api-keys` → `AdminHandler.ListApiKeys` 直接 `ForwardRequest` 给 python 服务，落库在 `llm_provider_keys`（`internal/api/llm_keys.go`）——那是**平台的 LLM 提供商密钥**，管理员配置用。

用户自己的 API Key 走的是另一条路：`api_keys` 表（`id, user_id, name, key_hash, last_used_at, expires_at, created_at, revoked`）已经存在，且**已经在鉴权链路里**（`internal/api/middleware.go`: `X-API-Key` → `sha256` → `JOIN api_keys ak ON ak.user_id = u.id`，含 `revoked`、`expires_at`、`tenant_id` 非空校验）。但全仓没有针对它的 CRUD handler —— 用户目前**只能手工往表里插行**才能拿到 key。

所以 B3 = 给 `api_keys` 补用户侧 CRUD（生成时只展示一次明文、存 `key_hash`），**不是**把管理端页面搬过去。

**② `tenant_invites` 的用量上限在并发下会超发。** `consumeInvite`（`internal/api/auth.go`）先 `SELECT ... WHERE max_uses = 0 OR uses < max_uses` 校验，再 `UPDATE tenant_invites SET uses = uses + 1 WHERE code = $1`。两句之间没有行锁，UPDATE 也不带 `uses < max_uses` 条件：

- 两个请求同时通过 SELECT 时都会 +1，`max_uses=1` 的邀请码能被用两次；
- 校验与自增不在一条语句里，`READ COMMITTED` 下 SELECT 读的是快照。

C1 既然要复用邀请，就得先修这条：改成一条 `UPDATE ... SET uses = uses + 1 WHERE code = $1 AND (max_uses = 0 OR uses < max_uses) RETURNING tenant_id`，用影响行数判成败。这是**存量 bug**，不是 C1 引入的。

**③ `ent_roles.permissions` 是 `varchar(255)`，不是 `text[]`。** `internal/enterprise/rbac.go` 的注释写"permissions text[] 并集去重"，但 `migrations/sql/init.sql` 里是 `varchar(255)`，代码实际按 JSON 字符串处理（`encodePerms`/`decodePerms`）。C1 若给 `tenant_members.role` 直接映射到 `ent_roles.name`，要按 JSON 字符串这条路走；`varchar(255)` 也意味着**角色权限点数量有硬上限**（超长会写入失败），设计角色时要留意。

### 10.2 B1 交易明细 CSV 导出

**现状（已核实）**：零实现。`GetHistory` 是 `ORDER BY created_at DESC LIMIT $2`，上限 50 且不接受分页参数 —— **不能复用**，导出必须独立查询（按时间范围 + 流式游标）。`internal/api/events.go` 已有 `http.Flusher` 的 SSE 先例，可参照同样的"拿到 flusher 才流式，拿不到退回一次性写"的写法。

**做法**：
1. `GET /v1/billing/transactions.csv?from=&to=`，`authMW(rlMW(...))`，按 `user_id` 过滤，`ORDER BY created_at`（导出的稳定序，不要 DESC —— 对账时正序更好读）。
2. 先 `SELECT count(*)` 判上限（5 万行）：超限返回 400 + 明确文案（"请缩小时间范围"），**不静默截断**。
3. 流式写出：`text/csv; charset=utf-8` + `Content-Disposition: attachment; filename="transactions_<from>_<to>.csv"`，首字节写 UTF-8 BOM（`\xEF\xBB\xBF`），否则 Excel 打开中文乱码。
4. 字段：`时间,事由,金额,余额快照,关联轮次`（`created_at, reason, amount, balance, turn_id`）—— 现有列已够，不改表。
5. 注意 `credit_transactions.created_at` 是 `timestamp`（无时区），导出时统一按 UTC 或服务器本地时区格式化并在表头注明，否则用户跨时区对账会差几小时（与 D 组 L4-3 是同一根源）。

**验收**：Excel 直接打开无乱码、中文正常；超 5 万行时返回明确错误；导出期间内存不随行数线性增长（流式）。

**风险**：CSV 公式注入（单元格以 `=`/`+`/`-`/`@` 开头时 Excel 会执行）—— `reason` 是内部枚举值风险低，但若将来放用户可控字段必须先做转义。这条建议在实现时同时加一行前缀保护，成本几乎为零。

### 10.3 B2 发票申请（工单式）

**现状（已核实）**：`payments` 表已存在（`id, user_id, channel, credits, amount_cents, currency, status, qr_code, provider_order_id, trade_no, created_at, paid_at`）；`invoice` 全仓无命中。

**做法**：新表 `invoice_requests(id, user_id, payment_id, title, tax_no, amount_cents, status, reviewed_by, reviewed_at, created_at, note)`；`status ∈ pending/approved/rejected`。重复拦截**靠唯一索引**而不是先查后插：`UNIQUE (payment_id)` 会让"同一笔支付只能开一张票"，用 `23505` 判重（与注册里邮箱冲突的处理同款）。

**验收**：审核通过/驳回后用户能看到状态与金额；对同一 `payment_id` 二次提交返回 409。

**要提前定的产品问题**（不是工程问题）：退款后已开票怎么处理、部分开票是否支持。建议**一期只支持全额开票 + 不可撤销**，把复杂状态机挡在门外——与 §6 "一次性套餐包"同一个思路。

### 10.4 B3 用户级 API Key 自助（对象见 §10.1 ①）

**做法**：
1. 复用 `api_keys` 表，不需要迁移。新增用户侧路由 `/v1/api-keys`（不是 `/v1/admin/...`，避免被 `adminReadMW` 拦下）。
2. 生成：`sk-` 前缀 + 随机 32 字节，**明文只返回一次**，库里只存 `sha256`（与 middleware 的校验方式一致，改算法会让存量 key 全部失效）。
3. 列表只回 `name / created_at / last_used_at / expires_at / revoked`，**永不回 `key_hash`**（它不是密码但也没必要暴露）。
4. 越权：所有查询带 `WHERE user_id = $currentUser`，他人 key 返回 **404 而非 403**（403 会确认"这个 id 存在"，是可枚举性泄漏）。

**验收**：用户可自助创建/轮换/吊销；访问他人 key 返回 404；新生成的 key 能通过 `X-API-Key` 直接调用。

**注意**：`middleware.go` 对 `tenant_id` 为空的用户直接拒绝（P1-5 的跨租户防护），所以 key 的可用性依赖用户已绑租户 —— 存量 `tenant_id=NULL` 的用户会拿到"key 有效但用不了"，需在创建时先拦一道。

### 10.5 C1 团队与成员（建议先评审，再动代码）

**现状（已核实）**：`tenant_members` 不存在；`users.tenant_id smallint REFERENCES tenants`；`tenants(id smallserial, name, created_at, status)`；`ent_roles(id, tenant_id, name, display_name, is_builtin, permissions varchar(255))`；注册时会按邀请码决定 tenant，并把**全站第一个用户**设为 `owner`。

**建议的最小切口**（与 §6 决策 2 一致）：
1. 新表 `tenant_members(tenant_id smallint REFERENCES tenants(id), user_id varchar(36) REFERENCES users(id), role_id varchar(36) REFERENCES ent_roles(id), joined_at timestamptz DEFAULT now(), PRIMARY KEY (tenant_id, user_id))`。`role_id` 指向 `ent_roles`（产品选定复用），**不再**存 `role varchar(16)`。另加 **`UNIQUE (user_id)`** 来落实「一人一租户」—— 靠数据库约束，而不是靠代码自觉。
2. 注册事务内**同时**写 `users.tenant_id` 与 `tenant_members`（两条 INSERT 在同一 tx），保证两者不会不一致；存量用户需要一条回填迁移。因为角色改成了外键，回填要先把 `owner/admin/member` 三条内置角色建出来（每个 tenant 一份，`ON CONFLICT DO NOTHING`），再按 `users.role = 'owner'` 映射到 owner 角色、其余映射到 member。
3. 先修 §10.1 ② 的邀请超发，再接"邀请 → 成员"。
4. 权限判定：**优先读 `tenant_members`**，`users.tenant_id` 保留为兼容字段（不删、不改类型）。这样灰度期两套数据都在，出问题可回退。

**三个问题已由产品答复（2026-10-29）**，答案与派生约束见 §12.4：

- 一个用户能否属于多个 tenant → **不能**，`tenant_members` 加 `UNIQUE (user_id)`；
- 成员角色与 `ent_roles` 的关系 → **直接复用**，`tenant_members.role_id` 外键指向 `ent_roles(id)`；
- 移除成员后资产归属 → **公开资产归团队、个人资产归个人**，所以不需要资产转移状态机，移除只切断访问。

**验收**：同 tenant 下多用户可见共享资源；角色可分配；邀请可撤销且过期失效；移除成员后其 API key 与登录会话同时失效。

**落地形态（按现有迁移规范）**：新建 `migrations/versions/0008_tenant_members.py`（`Revises: 0007_invoice_requests`）。① 本节原先写的是 `0006_tenant_members.py` / `Revises: 0005_user_onboarding`，那是 T2/T3 尚未占用迁移号时的推算；截至 2026-10-29 迁移链已是 `0001 → 0007`（head = `0007_invoice_requests`），故以 §11.5 的编号为准。灰度顺序是**先加表与回填、后切读路径**：① 建表 + 从 `users` 回填（幂等，`ON CONFLICT DO NOTHING`）；② 注册事务开始双写；③ 权限判定改读 `tenant_members`；④ 观察一轮后再考虑是否纳入更广的资源隔离。前三步任一步出问题都能靠"继续读 `users.tenant_id`"回退，不会出现两套数据都不完整的窗口。

### 10.6 建议的执行顺序（修订）

1. **B1**（无依赖、用户可感知，半天到一天）
2. **B3**（无依赖，但价值取决于是否有 API 用户；可与 B1 并行）
3. **§10.1 ② 邀请并发修复**（存量 bug，本身很小，是 C1 的前置）
4. **B2**（需要先定"部分开票/退款"的产品口径）
5. **C1**（单独立项评审；上面三个问题不定就不要开工）

**不建议**把 C1 和 B 组混在一批做：C1 动的是多租户隔离地基，一旦设计错，共享资源全部要返工。

## 11. B1 交付后的复核与后续开发任务（2026-10-29）

B1 已实现并通过门禁（后端 build/vet/test、前端 vue-tsc + i18n + vitest、源码编码检查）。本节记录**实际交付与 §10.2 规划的偏差**，以及接下来四项任务的拆解。口径同 §0：没读到实现的标「待确认」。

### 11.1 B1 实际交付 vs §10.2 规划（已核实）

| 规划项 | 实际实现 | 差异与理由 |
|---|---|---|
| 异步生成 + 下载链接 | **同步流式**（`http.Flusher`，逐行写出） | 仓库没有可复用的导出队列（§9 B1 已核实）；5 万行上限下同步足够，超时再评估异步 |
| 5 万行上限 | `exportMaxRows = 50000`，`>` 判定 | 恰好 5 万行可导，边界已单测钉住 |
| 无跨度限制 | 新增 `exportMaxSpanDays = 366` | 防止"5 年范围但每月只有几条"绕过行数上限做长扫描 |
| 无错误码 | 新增 `export_too_many_rows` / `export_range_too_wide` + `params` | 否则中文用户只能看到英文原文，且不知道"多宽算宽" |
| 字段 5 个 | 一致：时间(UTC)/事由/金额/余额快照/关联轮次 | — |
| 未提过滤口径 | 排除 `reason <> 'free_chat'` | 与 `GetHistory` 同口径，否则导出会多出用户界面上看不到的行 |
| 未提公式注入 | `csvSafeText` 中和 `=`/`+`/`-`/`@` 前缀 | 成本近乎为零，先按 §10.2 的风险提示做了 |

**仍在的三条局限**（不要当成闭环）：

1. 两条导出 SQL 只做了字符串断言，**未在真实 PostgreSQL 上执行过** —— 与 A2 同一个缺口，上线前需一次真库冒烟，重点看 5 万行时 `count(*)` 的耗时。
2. 前端导出范围**复用用量视图的 7/30/90 天**，用户无法自定义区间；后端其实接受任意 RFC3339。等有"按自然月对账"的真实诉求再放开，避免再做一套日期选择器（仓库无此依赖）。
3. `credit_transactions.created_at` 仍是 `timestamp`（无时区），导出一律按 UTC 解释并写在表头。跨时区用户看到的时间会与自己本地时间差几小时 —— 根源是 D 组 L4-3，不是导出引入的。

### 11.2 任务 T1：邀请码并发超发修复（最小、无依赖、存量 bug）

**现状（已核实）**：`internal/api/auth.go:57` 的 `consumeInvite` 是「先 SELECT 校验 `max_uses`/`expires_at` → 再 `UPDATE ... SET uses = uses + 1`」。两句之间无行锁，UPDATE 也不带 `uses < max_uses` 条件：
- 两个并发注册同时通过 SELECT，`max_uses=1` 的邀请码能被消费两次；
- `READ COMMITTED` 下 SELECT 读的是快照，校验与自增不是原子操作。

**做法**：合并成一条语句，用影响行数判成败：

```sql
UPDATE tenant_invites SET uses = uses + 1
 WHERE code = $1
   AND (expires_at IS NULL OR expires_at > NOW())
   AND (max_uses = 0 OR uses < max_uses)
 RETURNING tenant_id
```

`pgx.ErrNoRows` 分别落到"邀请码无效/已过期/已达上限"的既有文案（对外文案不变，避免前端文案跟着改）。

**验收**：并发 N 个 goroutine 抢同一个 `max_uses=1` 的邀请码，**恰好一个**成功；`max_uses=0`（不限次）仍可无限次使用；过期码仍被拒。

**为什么排第一**：它本身很小，且是 C1（团队邀请）的前置 —— 邀请一旦用于真正的团队扩容，"一个码被用两次"就是越权。

### 11.3 任务 T2：B3 用户级 API Key 自助

**现状（已核实，比 §10.4 更细）**：
- `api_keys(id, user_id, name, key_hash, last_used_at, expires_at, created_at, revoked)` 已存在，且已在鉴权链路（`X-API-Key` → `sha256` → `JOIN users`，校验 `revoked`/`expires_at`/`tenant_id` 非空）。
- 但全仓**没有任何写路径**：无 `INSERT INTO api_keys`，也没有任何地方更新 `last_used_at` —— 用户目前只能手工插表。
- 表上**只有主键与外键，没有 `key_hash` 索引**：鉴权那条 `WHERE ak.key_hash = $1` 是全表扫描，key 数量一多就是每个请求一次全扫。

**做法**：
1. 迁移 `0006_api_keys_selfservice.py`：`CREATE UNIQUE INDEX ... ON api_keys (key_hash) WHERE revoked = false`（部分唯一索引 —— 轮换后旧行 `revoked=true`，不参与唯一性，也天然防止"两个有效 key 同 hash"）。
2. 新增用户侧路由 `/v1/api-keys`（`authMW(rlMW(...))`，**不带** `adminReadMW`），CRUD：列表 / 创建 / 吊销。轮换 = 吊销旧的 + 创建新的（不做原地改 hash，避免并发下旧 key 短暂双活）。
3. 生成：`sk-` + 32 字节随机，**明文只在创建响应里返回一次**，库里只存 `sha256`（与 middleware 完全一致，改算法会让存量 key 全部失效）。
4. 列表只回 `id/name/created_at/expires_at/revoked`，**永不回 `key_hash`**。
5. 所有查询带 `WHERE user_id = $currentUser`；他人 key 返回 **404 而非 403**（403 会泄漏 id 存在性）。
6. 创建前先拦 `tenant_id IS NULL` 的用户：middleware 会直接拒绝这类 token，否则用户会拿到"key 有效但用不了"。

**要决定的一处**：`last_used_at` 目前从不写入。一期建议**不在列表展示它**（展示永远是空值，比不展示更让人困惑）；若确实要，按"每 key 最多每分钟更新一次"节流，不能每个请求都 UPDATE。

**验收**：用户可自助创建/吊销；新 key 能通过 `X-API-Key` 调通一个真实接口；访问他人 key 返回 404；`tenant_id` 为空的用户创建时得到明确提示而不是静默失败。

### 11.4 任务 T3：B2 发票申请（工单式）

**现状（已核实）**：`payments(id, user_id, channel, credits, amount_cents, currency, status, qr_code, provider_order_id, trade_no, created_at, paid_at, expired_at)` 已存在，`status ∈ pending/paid/failed`（`internal/billing/payments.go:22`），有 `idx_payments_user`；`invoice` 全仓零实现（仅 `internal/auth/mail_test.go` 的附件测试文件名叫 invoice.pdf，无关）。

**做法**：
1. 迁移 `0007_invoice_requests.py`：`invoice_requests(id, user_id, payment_id, title, tax_no, amount_cents, status, reviewed_by, reviewed_at, created_at, note)`，`status ∈ pending/approved/rejected`，`UNIQUE (payment_id)`。
2. 重复拦截**靠唯一索引**判 `23505`，不先查后插（与注册邮箱冲突同款做法）。
3. 只允许对 `payments.status = 'paid'` 且 `user_id` 匹配的支付开票 —— 用一条 `INSERT ... SELECT ... FROM payments WHERE ...` 保证"校验与写入"在同一语句里，不给并发留窗口。
4. 管理端审核走 `adminWriteMW`，状态只允许 `pending → approved|rejected` 单向流转。

**一期口径（按 §10.3 建议，需产品确认）**：只支持**全额开票 + 不可撤销**；退款后已开票的处理留到有真实诉求时再做。

**验收**：对同一 `payment_id` 二次提交返回 409；未支付的订单不能开票；审核后用户能看到状态与金额。

### 11.5 任务 T4：C1 团队与成员（前置 T1，先评审）

**现状（已核实）**：`tenant_members` / `invoice_requests` 均不存在（全仓仅 roadmap 提及）；`tenant_invites` 已存在并有管理端 CRUD（`/v1/admin/invites`）；`ent_roles.permissions` 是 `varchar(255)` 而 `rbac.go` 注释写 `text[]`（§10.1 ③）。

**前置**：T1 必须先完成。**开工前仍需产品回答 §10.5 的三个问题**（多 tenant / 角色与 `ent_roles` 的关系 / 移除成员后资产归属）。

**灰度四步不变**（§10.5）：建表 + 回填 → 注册双写 → 切读路径 → 再评估资源隔离。迁移号 `0008_tenant_members.py`，`Revises: 0007_invoice_requests`（T3 未做则 `0006`）。

### 11.6 建议顺序

1. **T1**（存量 bug，半天内，C1 的前置）
2. **T2**（无依赖，价值取决于是否有 API 用户；顺手补上 `key_hash` 索引，鉴权性能也跟着修）
3. **T3**（需产品先定"部分开票/退款"口径，否则只做全额版）
4. **T4**（单独立项评审，三个问题不定就不开工）

**不建议**并行做：T2 与 T1 可以穿插，但 T4 必须单独一批 —— 它动多租户隔离地基。另：B1 的三条局限（真库冒烟、自定义区间、时区）建议在 T2 开工前先做一次真库冒烟，成本低且能顺带验证 A2 的窗口函数 SQL。

## 12. T3 交付后的进度盘点与下一批任务（2026-10-29 核实）

> 本节只记录**已核实的事实**与**可独立验收的任务**。工作区曾长期处于未提交状态，现已分批进入历史（`HEAD` = `4300953`，套餐目录后台管理）；仍留在工作区的改动见 §13.5 末尾。

### 12.1 进度盘点（已核实）

| 任务 | 后端 | 前端 | 迁移 | 状态 |
|---|---|---|---|---|
| T1 邀请码并发超发 | `consumeInviteQuery` 单条 `UPDATE ... RETURNING`（`auth.go:67`），窄接口 `inviteStore` | — | 无 | 代码完成；**并发未在真库验证**（`auth_invite_live_test.go` 需 `CHIRON_TEST_POSTGRES_DSN`，本机 docker 未运行 → skip） |
| T2 用户级 API Key | `GET/POST/DELETE /v1/api-keys`（`api_keys_handler.go:104-106`），越权 404、明文只回一次、每用户上限 50 | 用户侧 `views/UserApiKeysView.vue` + `/api-keys` 路由 + 账号菜单入口（T6 新建，**不是**从 admin 挪来的，见 §12.3） | `0006_api_keys_key_hash_index`（`key_hash` 部分唯一索引） | 前后端完成，P1-3 验收已闭环；`last_used_at` 已按每 key 每分钟一次节流写入 |
| T3 发票申请 | 用户 `GET/POST /v1/billing/invoices` + `GET /v1/billing/invoiceable-orders`；管理端 `GET /invoices`、`POST /invoices/{id}/review` | 用户侧 `BillingView` 发票 tab；管理端 `InvoiceReviewView` + `/admin/invoices` + 侧栏入口 | `0007_invoice_requests` | 前后端完成，9 条新用例全绿；**`INSERT ... SELECT` 原子性未在真库验证** |

迁移链现状（2026-10-30 复核）：`0001 → 0011`，head = `0011_notifications`。**下一个可用号是 `0012`**。CI 的 `schema` job 会强制单 head，`integration` job 会 `alembic upgrade head` 后跑活库用例。

### 12.2 任务 T5：真库冒烟（最高优先，一次成本覆盖六处缺口）

**为什么排第一**：目前累计 **6 处 SQL 只做过字符串断言，从未在真实 PostgreSQL 上执行**。每新增一个功能都在扩大这个池子，而它们都是"单测全绿、上线才炸"的类型：

1. B1 两条导出 SQL（`exportCountQuery` / `exportRowsQuery`，含 5 万行时 `count(*)` 的耗时）
2. A2 用量明细的窗口函数 + `COUNT(*) OVER ()` 求 `truncated`
3. `invoice_requests` 建表 + `INSERT ... SELECT ... FROM payments WHERE status='paid'`
4. `consumeInvite` 的并发（本地已有 live 用例，缺的是能跑的环境）
5. **T6 新增**：`AuthMiddleware` 里补选 `ak.id` 的那条 JOIN（`SELECT ak.id, u.id, u.role, ...`）—— 单测只校验了字符串包含关系，列数/扫描顺序错了要真库才暴露
6. **T6 新增**：`UPDATE api_keys SET last_used_at = now() WHERE id = $1`（节流后每 key 每分钟一次）

**做法**：起一个临时 PostgreSQL（docker 或本机实例），设 `CHIRON_TEST_POSTGRES_DSN` 后跑 `go test ./internal/... -run 'Live'`，再补一条只跑 A2/B1 两条 SQL 的冒烟用例。

**验收**：4 项全部从 skip 变为真实通过；5 万行导出的 `count(*)` 耗时有具体数字。

**阻塞**：本机 docker daemon 未运行（`npipe:////./pipe/docker_engine` 找不到）。需要一台可达的 PG，或启动 Docker。

#### 12.2.1 用例已预置（2026-10-29，**只差环境，不是已完成**）

把"起库之后才有活干"的部分先做掉了：新建 `internal/api/sql_smoke_live_test.go`，覆盖上表 ①②③⑤⑥ 五处（④ 复用既有 `auth_invite_live_test.go`，不重复实现）。

一条关键做法：这些 SQL 原先散在 handler 里是内联字符串，测试若照抄一份，两边漂移就回到"测试绿、线上炸"。因此先把 7 条提成包级常量再让用例直接跑**生产语句本身**：

| 常量 | 所在文件 | 覆盖缺口 |
|---|---|---|
| `exportCountQuery` / `exportRowsQuery` | `billing_export.go`（原已提） | ① |
| `usageBreakdownQuery` | `billing_handler.go` | ② |
| `applyInvoiceQuery` | `invoice_handler.go`（原已提） | ③ |
| `apiKeyAuthQuery` | `api_keys_handler.go` | ⑤ |
| `apiKeyMarkUsedQuery` | `api_keys_handler.go` | ⑥ |
| `apiKeysListQuery` / `apiKeyCountActiveQuery` / `apiKeyInsertQuery` / `apiKeyRevokeQuery` | `api_keys_handler.go` | T2 契约 |

用例不止验"语法能跑"，还钉住了三个只有真库才暴露的行为：

1. **A2 截断后总量仍准确**：造 150 个 model 桶（超过 `usageBreakdownLimit = 100`），断言 `grand_input` 是全部 150 桶之和 11325，而**不是**返回的 100 行之和 5050。若有人把 `SUM(...) OVER ()` 改成"把返回行相加"，用户对账就会少算 —— 这个错误在任何"数据量小于 100"的测试里都看不出来。
2. **导出的 `reason <> 'free_chat'` 会静默排除 NULL 行**：`credit_transactions.reason` 可为 NULL，该条件对 NULL 求值为 UNKNOWN。用例插入 4 条流水（2 条 chat / 1 条 free_chat / 1 条 NULL）断言 count = 2。当前实现即如此；将来若认为这些流水也该进账单，需改成 `COALESCE(reason, '') <> 'free_chat'`，届时这条断言会提醒。
3. **`last_used_at` 的时区隐式转换**：`init.sql` 里该列是 `timestamp`（无时区），回写却用 `now()`（timestamptz）。断言回写后与当前时间偏差不超过 1 小时，否则视为时区转换出错。

**当前状态：本机跑全部 `skip`（无 DSN），但 CI 会真跑。** `.github/workflows/ci.yml` 的 `integration` job（:274-331）用 `pgvector/pgvector:pg16` service 起真库、`alembic upgrade head` 建 schema，再以 `go test ... -run 'Live|Diag'` 执行 —— 注入的正是 `CHIRON_TEST_POSTGRES_DSN`（:304），所以本文件这 4 条用例会在那里真正跑起来。

因此**能保证的只有"编译通过 + 无 DSN 时正确 skip"**；断言逻辑本身仍未在本机验证过，要看 CI 的 `Real-stack integration` 结果才能确认缺口是否关闭。**推 CI 时优先看这个 job，而不是 `go` job** —— 后者没有 DSN，`TestLive*` 一律 skip，绿了也不说明这些 SQL 跑通了。

**起库之后直接执行**：

```bash
docker run -d --name chiron-pg -e POSTGRES_PASSWORD=chiron -p 5433:5432 postgres:16
export CHIRON_TEST_POSTGRES_DSN="postgres://postgres:chiron@localhost:5433/postgres?sslmode=disable"
python -m alembic upgrade head   # 把 0001..0007 全部应用，缺表会让用例直接报 undefined_table
GOFLAGS=-mod=mod go test ./internal/api/ -run 'Live' -count=1 -v
```

还要单独补一项本文件未覆盖的指标：**5 万行导出的 `count(*)` 耗时**（缺口 ① 的后半句）。它需要造数，写进单测会让 CI 变慢，建议起库后手工跑一次 `EXPLAIN ANALYZE` 并记下数字。

### 12.3 任务 T6：T2 前端闭环 + `last_used_at` 写入（**已完成**）

**先更正一条此前的错误判断**：§12.1 曾写「`ApiKeysView.vue` 仍在 `views/admin/`，需挪到用户侧」。**这个是错的**，实读后确认两件事是不同功能：

| | `views/admin/ApiKeysView.vue` | `views/UserApiKeysView.vue`（T6 新建） |
|---|---|---|
| 接口 | `/v1/admin/api-keys` | `/v1/api-keys` |
| 字段 | `provider` / `key` / `base_url` / `weight` / `remark` | `name` / `created_at` / `last_used_at` / `revoked` |
| 语义 | 平台运营配置的 **LLM 供应商密钥池** | 用户自己账号调网关的**凭证** |
| 权限 | 需 `admin:read`/`admin:write` | 任意登录用户，后端用 `user_id = $1` 隔离 |

也就是说：**用户级 Key 的前端从未存在过**，T6 是从零新建，而不是"搬文件"。admin 页保持原样不动 —— 它是运营工具，不该挪。

**实际交付**：
1. `src/api/userApiKeys.ts` —— `listUserApiKeys` / `createUserApiKey` / `revokeUserApiKey`，返回 `data?.data`。
2. `src/views/UserApiKeysView.vue` —— 列表 / 创建 / 吊销。两条约束决定了它长什么样：
   - **明文只出现一次**：创建成功后弹窗不关、停在"请立刻复制"形态；没复制就点关闭要二次确认（关掉等于永久丢失，只能吊销重建）。
   - **已吊销的 key 保留展示**：否则用户以为自己从没创建过，会重复创建。
3. `router/index.ts` 挂 `/api-keys`（`requiresAuth` + `cloudOnly`，本地模式无账号体系）；`AppLayout.vue` 加入**账号下拉菜单**而非主导航 —— 它是账号凭证，不是每天切换的工作台。
4. `settings` 域补 25 个 `user_api_key_*` 键，zh-CN → en-US → ar 三语齐备。
5. **`last_used_at` 节流写入**：`internal/api/apikey_usage.go` 的 `apiKeyUsageTracker`，同一 key 每分钟最多写一次；`AuthMiddleware` 的鉴权 SQL 补选 `ak.id`，成功后调 `markAPIKeyUsed`。刻意选了**同步**而非 `go func()`：节流后每 key 每分钟一次，代价相对整个请求可忽略，而异步要额外处理"请求已返回、写入还在跑"的生命周期。写失败只告警 —— `last_used_at` 是运维信息，不能因此让合法请求失败。

**验收**：✅ 普通用户不经过 admin 路由即可创建/吊销；✅ `last_used_at` 有值且不随每个请求刷新（7 条节流用例覆盖：间隔内只写一次、满间隔重新允许、多 key 隔离、空 id 拒绝、上限清理、50 并发同 key 只 1 次写库）；✅ 五项门禁全绿（`go test ./internal/api` 2.09s / vitest 767 passed / vue-tsc / check-i18n-keys / 编码检查）。

**遗留**：`SELECT ak.id, ...` 与 `UPDATE api_keys SET last_used_at` 两处 SQL 仍只做过字符串级校验，**未在真实 PG 上执行** —— 归入 §12.2 的 T5。

### 12.4 任务 T7：C1 团队与成员（**已完成**，`0008_tenant_members`）

**产品答复（2026-10-29）**：

| 问题 | 答复 | 派生约束 |
|---|---|---|
| 一个用户能否属于多个 tenant | **不能**，一期一人一 tenant | `tenant_members` 加 `UNIQUE (user_id)` —— 靠数据库约束而不是代码自觉；不建租户切换器；`users.tenant_id` 继续单值且不失真 |
| 成员角色与 `ent_roles` 的关系 | **直接复用 `ent_roles`** | `tenant_members.role_id` 外键指向 `ent_roles(id)`，不存 `role varchar(16)`；必须补内置角色种子 —— 现状 `ent_roles` **没有任何种子**，只能靠管理端手工 POST 建（`ent_identity_handler.go:430`） |
| 移除成员后资产归属 | **公开资产归团队，个人资产归个人** | 不需要资产转移状态机。资产已有 `visibility` 列（`knowledge_bases.visibility DEFAULT 'private' NOT NULL`、`agents.visibility`），移除成员 = 切断其访问；私有资产随人走、公开资产留在团队 |

**复用 `ent_roles` 的强制成本**（本轮与 §10.5 原建议相反的一处，必须写清代价）：原建议是团队角色只用于成员管理、不接 `ent_roles`，理由是「谁是管理员」会出现两个真相。产品选择复用后，该风险只能靠规则消掉：

1. `tenant_members.role_id` 是**唯一**的租户内角色真相；`users.role`（`owner`/`user`）只保留系统级语义（全站首用户），不再参与租户内判定。
2. 内置 `owner/admin/member` 由迁移**按 tenant 播种**（`ON CONFLICT DO NOTHING`），权限点写进 `ent_roles.permissions`，使 `rbac.go` 的 `LoadEffectivePerms` 能自然聚合，而不是另起一套权限判断。

**顺带更正一处长期悬置的事实**：本节原先写「`ent_roles.permissions` 在 `init.sql:450` 是 `varchar(255)`，而 `rbac.go` 按 `text[]` 用，需真库确认」。核实结果：`init.sql` 是历史遗留、已不被执行（README:174-181 明确「单一权威基线 + 增量迁移」，基线 DDL 取自 `pg_dump --schema-only`）；权威基线 `0001_authoritative_baseline.py:328` 就是 `permissions text[] DEFAULT ARRAY[]::text[]`，且**没有任何后续迁移改过它**（`rg permissions migrations/versions/*.py` 全仓仅此一处）。**所以 `rbac.go` 本来就对，这一项不需要真库即可结掉，也不必再挂到 T5。**

**任务拆解**（`0008_tenant_members.py`，`Revises: 0007_invoice_requests`），灰度顺序仍是「先加表与回填、后切读路径」：

1. **迁移**：建 `tenant_members`（PK `(tenant_id, user_id)`、`UNIQUE (user_id)`、`role_id` 外键、`joined_at timestamptz DEFAULT now()`）；播种内置角色；从 `users` 回填（`role='owner'` → owner 角色，其余 → member）。全部 `ON CONFLICT DO NOTHING`，保证可重复执行。
2. **注册双写**：在 `auth.go` 已有事务内追加一条 `INSERT INTO tenant_members`，与 `users` 的 INSERT 同 tx，不留「用户已存在但无成员记录」的窗口。
3. **成员管理 API**：成员列表 / 邀请 / 移除（邀请复用已有 SMTP 与 `consumeInvite`）；移除时同时吊销该用户的 API key 与登录会话。
4. **切读路径**：权限判定优先读 `tenant_members`，`users.tenant_id` 保留为兼容字段（不删、不改类型）。任何一步出问题都能退回读 `users.tenant_id`。
5. **资产可见性**：按 `visibility` 列落实「公开归团队 / 私有归个人」，不做转移。

**排序说明**：三个问题已不再是阻塞项；C1 仍排在 T5 之后，理由从「语义未定」变成「每加一条 SQL 都在扩大待验池」，见 §12.6。

### 12.5 D 组技术债（都要外部条件，排最后）

| 项 | 缺什么 | 能否现在做 |
|---|---|---|
| L4-3 `timestamp` → `timestamptz` | **生产时区语义抽样确认** + 可达 PG；142 列、72 表，且同一列可能混两种语义 | 否 |
| S5-(e) 沙箱部署形态 | 独立容器环境（无 docker socket、仅内网）+ 共享工作区卷 | 否 |
| L3-4 巨型文件拆分 | 无功能收益，剩尾批 | 可以，但优先级最低 |

### 12.6 建议顺序（修订，T6 交付后）

1. ~~**T6 T2 前端闭环**~~ —— **已完成**（§12.3）。P1-3 验收已闭环。
2. **T5 真库冒烟** —— **现在唯一能清风险池的动作**，缺口已增至 6 处（含 T6 新增两条 SQL）。T7 之前必须清掉。
3. **T7 C1** —— **三个产品问题答完再开工**，否则必然返工
4. **L3-4 尾批** —— 有空再说

**当前实际状态（2026-10-29 复核）**：T1/T2/T3/T6/T7 与 P2-2 全系列（P2-2 / P2-2b / P2-2c）**均已交付并进入历史**（`HEAD` = `4300953`）。§12.6 原先的两条线都已关闭：

- **T5 真库冒烟** —— **已由 CI 覆盖，无需追加工作**：`.github/workflows/ci.yml` 的 `integration` job 起真 PG（`pgvector/pgvector:pg16`）、`alembic upgrade head`、注入 `CHIRON_TEST_POSTGRES_DSN`，以 `-run 'Live|Diag'` 执行 `./internal/api/... ./internal/billing/...`。本轮新增的 `TestLiveAdminPackageCRUD` 与权益用例同样落在该模式内。唯一仍缺的是**手工**指标：5 万行导出的 `count(*)` 耗时需 `EXPLAIN ANALYZE` 补一次（写进 CI 会让单次运行变慢，故不自动化）。
- **T7 C1 团队与成员** —— **已完成**（`0008_tenant_members`，复用 `ent_roles`、播种内置角色）。

**因此「在 T5 之前不开新功能」这条约束已解除**：风险池已由 CI 承接，C1 也已落地。C3（P2-3 通知中心）同样已交付（`0011_notifications`）。

**不要做的事**：C3 开工时不要把通知做成「每条事件一张表」或「先查后写」去重 —— 去重必须靠唯一索引撞键跳过，否则并发下会重复推送（§14 第 1 条）；也不要给一次性套餐引入周期性定时任务（§6 决策 1）。

## 13. P2-2 交付后的复核（2026-10-29 核实）

### 13.1 已交付（已进入历史）

| 步骤 | 落点 | 验证 |
|---|---|---|
| ① 权益表 + 迁移 | `migrations/versions/0009_billing_packages.py`：`billing_packages`（目录）+ `user_entitlements`（已售权益快照）+ `payments.package_code` | 真库 `alembic upgrade head` 成功，`current` = `0009_billing_packages (head)`；表/索引/CHECK 逐项核对通过 |
| ② 一次性发放 | `internal/billing/entitlements.go` + `payments.go` 在 `ConfirmPayment` 里 `AddCredits` 成功后发放权益 | 真库用例 PASS：credits 精确到位、权益快照正确、`payment_id` 重复确认不重复发放、窗口移到过去后查不到但行保留 |
| ③ 接入模型范围 | `internal/api/ent_entitlement_gate.go`：与租户策略**取更严者**（交集） | 12 条表驱动子用例 PASS |
| ④ 过期与提示 | `GET /v1/billing/entitlements` 返回**含已过期**条目；三语 21 个 key；`BillingView.vue` 新增「套餐包」tab | `vue-tsc` 通过、`check-i18n-keys` 缺键 0、vitest 97 文件 / 767 用例全绿 |
| ⑤ 权益真正生效（P2-2b） | `internal/api/ent_entitlement_gate.go`（`EffectiveModelSet` / `GuardChatModelAccess`）+ `/v1/models` 过滤 + `proxyOpt.guardBody` 钩子 + 前端三语与「去购买套餐」直达 | `go build/vet ./...` 通过、`internal/api` 3.0s / `internal/billing` 1.0s 全绿、vitest 97 文件 / 770 用例、`vue-tsc` 与 i18n 键集通过 |
| ⑥ 套餐目录可运营（P2-2c） | `internal/api/billing_packages_handler.go`（管理端 CRUD）+ `auth.PermBillingManage` + `routes_admin.go` 五条路由 + `migrations/versions/0010_seed_billing_packages.py` + `frontend-vue/src/views/admin/PackagesView.vue` | 真库 `0010_seed_billing_packages (head)` 且三档套餐落库；`TestLiveAdminPackageCRUD` PASS（创建拿主键、重复 code→`ErrPackageCodeExists`、code 不可改、下架后用户侧不可见但后台仍可见）；`go build/vet ./...` 与 `internal/api`+`internal/billing` 单测全绿；`vue-tsc`、i18n 键集、`check_source_encoding` 通过 |

设计口径（沿用 §6 决策）：有效期从确认时刻起算、不引入 `billing_cycle`、无周期任务、过期只由时间判定；权益挂 `user_id`（用户级）而非租户级，避免一个成员的套餐影响整租户；套餐目录与已售权益**分两张表**，套餐改价不影响已售出权益。

### 13.2 已修：权益判定曾挂在没有调用方的函数上（P2-2b 补上）

**本条记录的是修复前的状态**，保留原文以便追溯为什么新增了 `ent_entitlement_gate.go`。

**修复结果（P2-2b，2026-10-29）**：不再复用 `EnforceEnterprisePolicy`，而是新增一条专供请求路径使用的判定链，原因是它的语义不适用于过滤：

- `EffectiveModelSet(ctx, tenantID, userID) (map[string]struct{}, bool)` —— 第二个返回值 `restricted` 区分「不限制」与「限制为空」。**这个区分是必须的**：`ResolveAllowedModels` 在「不限制」时返回 `admin_model_configs.model_id` 全量清单，而真库实测该表 0 行、`llm_models` 40 行、两者交集 0 —— 直接拿它过滤 `/v1/models` 会得到**一个模型都不返回**的空下拉，用户却以为权限正常。
- `GET /v1/models` 在 `ListModelsForUser` 末尾按 `restricted` 过滤；解析失败 fail-open + `slog.Warn`。
- 对话入口：`proxyOpt` 新增 `guardBody` 钩子，`POST/PUT` 在 `mutateBody` **之后**调用 `GuardChatModelAccess`。顺序很关键——`InjectSessionRuntime` 才会把最终生效 model 写回 body，若排在它之前只看得到前端随手传的字段。
- 越界返回 `403 + code=model_not_allowed + params{model, reason}`，`reason` 分 `entitlement` / `policy`；报文显式 `retryable=false`，避免给出「再点一次」的无效重试。
- 前端三语补齐，`reason=entitlement` 时给「去购买套餐」按钮并直达 `/billing?tab=packages`；`reason=policy` 只给文字指引（用户侧没有可跳转的管理员入口，不做点了不动的按钮）。

**口径重叠已消除（2026-10-29）**：`EnforceEnterprisePolicy`、`ResolveAllowedModels` 及其专用的 `allModelIDs` 已**删除**，请求路径只剩一条判定链——`EffectiveModelSet`（`/v1/models` 过滤）与 `GuardChatModelAccess`（对话准入 + 403 归因），两者共用 `resolveAllowed` / `resolveEntitled` 注入点。删掉的理由是它们与真正的准入判定**语义相反**：`ResolveAllowedModels` 在「不限制」时必须返回 `admin_model_configs` 全量清单（该表真库 0 行），任何人拿它当过滤基准都会得到空下拉。原断言该旧函数的测试已改写为直接断言 `modelAccessReason`（`TestModelAccessTakesStricterLayer`），覆盖未丢。

<details>
<summary>修复前的核实记录</summary>

**核到的事实**（`rg -n "EnforceEnterprisePolicy|ResolveAllowedModels"` 排除 `.worktrees/`、`vendor/`、测试文件，全仓仅 14 处命中，全部在 `ent_policy_handler.go` 自身，即定义 + 包级转发）：

1. `EnforceEnterprisePolicy` / `ResolveAllowedModels` 在非测试代码里**没有任何外部调用方**。
2. `GET /v1/models` 的实际实现是 `ListModelsForUser`（`internal/api/model_discovery.go:342`），直接 `SELECT provider, name, display_name, context_window FROM llm_models WHERE enabled = true`，**不读任何白名单**。
3. 对话入口 `POST /v1/chat/submit` 走通用代理 `chatP(pathFn("/v1/chat/submit"))`（`routes_proxy.go:169`），Go 侧不做 model 准入。
4. `NewEntPolicyHandler().RegisterRoutes(mux, authMW)`（`gateway_router.go:525`）只注册了策略**CRUD**，强制点没接上。

**后果**：租户模型白名单与套餐权益**目前都不生效**。这缺口不是 P2-2 引入的（白名单本来就没接到请求路径上），但 P2-2 的验收「购买后权益生效」因此只做到了「权益能查到」，没做到「模型真的按权益放行/拒绝」。

**这是本轮最重要的一条**：再做套餐目录、通知中心之前必须先补这个，否则所有权益类功能都是在往一个死点上接线。

#### 任务 P2-2b：把白名单与权益真正接到请求路径

1. `ListModelsForUser`：按 `auth.GetClaims` 取 tenant/user，调 `ResolveAllowedModels`，用结果过滤返回列表；查询失败 **fail-open**（与既有约定一致）+ `slog.Warn`。
2. 对话提交入口：解析 `body.model`，调 `EnforceEnterprisePolicy`；不在清单返回 **403 + 机器可读 `code: "model_not_allowed"`**，并在错误里说明是「套餐权益」还是「租户策略」所致（两者文案可执行动作不同：前者「购买/续期套餐」，后者「联系租户管理员」）。
3. 测试：白名单外模型被拒（403）；`/v1/models` 只返回交集；策略查询失败时放行。
4. 三语补 `errors.model_not_allowed`（zh-CN → en-US → ar），前端把 403 落到「去购买套餐 / 找管理员」两个不同动作。

**验收**：用受限账号调 `/v1/models` 只看到权益内模型；直接 POST 一个权益外模型返回 403 且文案指明原因与下一步。

### 13.3 任务 P2-2c：套餐目录后台 CRUD（**已完成**，2026-10-29）

交付前现状：`billing_packages` **只有表**——无 handler、无种子数据、无管理界面，必须手工 `INSERT` 才能卖套餐。

**已交付**：

| 项 | 落点 | 关键决定 |
|---|---|---|
| 权限点 | `internal/auth/jwt.go` 新增 `PermBillingManage = "billing:manage"` | **不复用** `PermAdminWrite`：定价与所含权益决定平台收多少钱、卖什么货，属定价权而非日常运维，独立后可只授予运营。owner/admin 有，`user` 无 |
| 凭据边界 | `internal/auth/apikey.go` 的 `apiKeyDeniedPerms` 加入该权限 | 定价改一次就影响真实收款，不该由长期凭据代持 |
| 管理端 handler | `internal/api/billing_packages_handler.go`：`buildPackage` 纯函数一次回全部校验错误（422）；code 冲突 409；id 不存在 404 | 校验模型名走 `llm_models` 而非 `admin_model_configs`（真库后者 0 行 → 会拒掉全部合法模型名，与 §13.2 同一个坑） |
| 路由 | `routes_admin.go`：`GET/POST /v1/admin/billing/packages`、`PUT/DELETE /{id}`、`POST /{id}/restore` | 读接口也挂 `billingManageMW`：能看到定价草案的人应与能改它的是同一批 |
| 下架语义 | `SetPackageEnabled` 软下架，`DELETE` 表达下架、`POST /{id}/restore` 重新上架 | 不物理删除：已售权益是快照不受影响，但历史订单要靠 code 反查套餐名 |
| seed | `migrations/versions/0010_seed_billing_packages.py`（`down_revision=0009_billing_packages`） | 幂等 `INSERT ... SELECT ... WHERE NOT EXISTS`，播 starter/standard/pro 三档，`allowed_models` 空数组 = 不限 |
| 前端 | `views/admin/PackagesView.vue` + `api/admin.ts` 6 个函数 + 路由 `/admin/packages` + 侧边栏菜单 + 三语各 42 键 | 表格含已下架项；编辑时 code 只读 |

**验证**：真库 `current = 0010_seed_billing_packages (head)`；`TestLiveAdminPackageCRUD` PASS——创建后拿到主键、重复 code 返回 `ErrPackageCodeExists`、`UpdatePackage` 传别的 code 不改写它（刻意的：code 是 `payments.package_code` 的引用值）、下架后用户侧 `ListPackages` 不可见而后台 `AdminListPackages` 仍可见、不存在的 id 返回 `(false, nil)`；`go build/vet ./...` 与前端 `vue-tsc`、i18n 键集、全量 vitest 均通过。

**一处测试自身的前提错误（已纠正，实现无需改）**：最初断言「`UpdatePackage` 改 code 会撞唯一索引」，但实现刻意不在 `UPDATE` 里包含 code，故该场景不可能发生；改为断言「传什么 code 都不会改变它」。

### 13.4 小修补（已核实的确定缺口）

- ~~`paymentResponse`（`billing_handler.go:798-812`）**仍未**回显 `package_code`~~ —— **已修（2026-10-29）**。`paymentResponse` 现于 `created_at` 之后、`qr_code` 之前按非空条件回显 `package_code`，纯充值订单的存量响应形状不变；单测 `TestPaymentResponseEchoesPackageCode` 覆盖「套餐订单回显 / 纯充值不带 / 既有字段不变 / 无二维码省略 `qr_code`」四条。
- ~~`frontend-vue/src/components.d.ts` 的 `OnboardingGuide` 两行~~ —— 已随前述提交进入历史，工作区不再有该改动，无需处理。

### 13.5 建议顺序（P2-2c 交付后重排，2026-10-29）

| 序 | 任务 | 为什么排在这 | 前置 |
|---|---|---|---|
| ~~1~~ | ~~P2-2c 套餐目录后台 CRUD + seed~~ | **已完成**，见 §13.3 | — |
| ~~2~~ | ~~小修补：`paymentResponse` 回显 `package_code`~~ | **已完成**（§13.4）。套餐已能上架售卖，订单凭证不再缺字段 | 无 |
| ~~3~~ | ~~收敛遗留的**双判定路径**~~ | **已完成**（§13.2 末）。`EnforceEnterprisePolicy` / `ResolveAllowedModels` / `allModelIDs` 已删除，请求路径只剩 `EffectiveModelSet` + `GuardChatModelAccess`；原针对性测试改写为 `TestModelAccessTakesStricterLayer`，覆盖未丢 | 无 |
| ~~4~~ | ~~**T5 真库冒烟推 CI**~~ | **无需新增工作**：CI 的 `integration` job 已跑 `go test ./internal/api/... ./internal/billing/... -run 'Live\|Diag'` 并先 `alembic upgrade head`，本轮新增的 `billing_packages` 写路径（`TestLiveAdminPackageCRUD`）与权益测试都被这个模式覆盖 | — |
| ~~2~~ | ~~P2-1 团队与成员~~ | **已完成**：`migrations/versions/0008_tenant_members.py` 已交付（复用 `ent_roles`、播种内置角色、从 `users` 回填）。§4 曾列为结构性缺口，实际落地方式与 §12.4 的五步一致 | — |
| ~~1~~ | ~~P2-3 通知中心~~ | **已完成**（2026-10-30，见 §14）。`0011_notifications` + `internal/notify` 包 + 四个事件源 + 用户侧接口与前端页；到期预警按小时扫描、只发通知不改状态，与「一次性套餐无周期任务」不冲突 | — |
| **2** | D 组技术债（L4-3 / S5-(e) / L3-4 尾批） | 无功能收益，且 L4-3 与 S5-(e) 都缺外部条件（生产时区语义抽样 + 可达 PG、独立容器环境），只能排最后 | — |

排序理由：原先的第 1–4 条（P2-2c、`paymentResponse` 回显、双判定路径收敛、T5 推 CI）已全部清掉；P2-1 与 P2-3 均已交付。因此现在**功能线上不再有待办**，剩下的都是受外部条件阻塞的技术债。

## 14. P2-3 通知中心（**已交付**，2026-10-30：设计与实际取舍）

本节原为「开工前先定形状」，现按实际交付更新；四项参数的最终取值与实现取舍记录在这里，因为它们一旦定错就要动数据。

**已核实的地基**（开工前读过）：
- 已有 SMTP 发信能力（`internal/auth/mail.go`），无需引入新依赖。
- 已有站内消息的读模型先例（notice 行），但那是**对话内**的，不能直接当通知中心用。
- 权益到期时间可从 `user_entitlements.expires_at` 直接取，不需要新表存"何时到期"。

**四项参数的最终取值**（用户已拍板）：

1. **去重键的形状** —— `payment_succeeded:<payment_id>`、`entitlement_expiring:<entitlement_id>:<days_before>`、`quota_changed:<pool_id>:<updated_at_nanos>`、`member_invited:<user_id>`。唯一索引 `(user_id, dedupe_key)`，撞 23505 即跳过，**不做先查后写**（并发下两个请求都能查到"没有"，于是各插一条）。
2. **渠道开关的粒度** —— **用户级**。落为 `user_notification_prefs(user_id, kind, email_enabled)`，**缺行即默认开启**（读路径 `COALESCE(..., TRUE)`），不给每个用户 × 每种事件预置行：后者会让新增事件类型时必须补数据迁移，且存量用户全部漏发。站内信**始终开启**，开关只管邮件——站内是事实留痕，关掉它会让「用户声称没收到」变成无法查证的口径之争。
3. **到期预警的扫描频率与提前量** —— 每小时扫一次，提前 **3 天与 1 天**各一次（两个独立去重键）。档位用 `ceil(剩余小时/24)` 判定，即每档覆盖 `(N-1, N]`，因此文案写「N 天内到期」而非「N 天后」——剩余半天时说「1 天后」是错的。扫描**只发通知、不改状态**：权益是否生效完全由 `starts_at/expires_at` 与当前时间比较决定，没有 status 列要维护，所以漏跑一轮不会让权益错乱。
4. **`tenant_id` 类型** —— `smallint`，与 `tenants.id` 一致，外键可建。列值**由 `users` 表派生**而非调用方传入：传错就把通知挂到别的租户下（跨租户可见），派生天然正确。

**实现中修正了一处原设计**：`member_invited` 原打算在「邀请发出时」通知被邀请人，但 `tenant_invites`（基线 `0001:725`）只有 `code/tenant_id/created_by/max_uses/uses/expires_at`，**没有 email 列**，是通用邀请码而非指向具体人的邀请，创建时没有确定的收件人。因此该事件落在「成员凭邀请码注册成功」的那一刻，通知对象是新成员本人。

**`quota_changed` 的收件人是租户 owner/admin 而非全租户**：配额是租户级资源，广播给全成员时一次调整就写入 N 行（N = 成员数），且绝大多数收件人对此无能为力；普通成员真正需要感知的是「自己额度用完了」，那由调用时的 `quota_exceeded` 提示承担。

**验收结果**：同一支付回调重复到达只产生一条通知（真库验证 `ON CONFLICT` 命中唯一索引）；用户可标记已读（跨用户被 `user_id` 条件挡住）；关掉邮件后仍能在站内看到；到期前 3 天与 1 天各收到一次且重复扫描不重复推送。

## 15. 下一批开发任务（2026-10-30，P2-3 交付后）

功能线（P0/P1/P2）至此全部交付。以下三项是**本轮交付自身暴露出来的缺口**，都已逐处读到实现；第四项起是受外部条件阻塞的技术债，重排理由见 §12.5。

### T1 通知内容本地化（**已交付**，2026-10-30）

**原状（已核实）**：通知的标题与正文在服务端**硬编码中文**——`internal/billing/entitlement_expiry.go:68-69`（「套餐将在 %d 天内到期」）、`internal/api/notify_support.go:114-115`（「已加入团队」）、`internal/billing/payments.go` 的 `notifyPaymentSucceeded`（「支付成功」/「套餐购买成功」）。前端只翻了分类标签（`NotificationsView.vue` 的 `kindLabel()`），正文仍是中文，所以 en-US / ar 用户看到的是「英文标签 + 中文正文」。邮件出口更没有兜底——正文就是那段中文。

**实际做法（与下文原计划有一处偏离，见「为什么没加 render_key」）**：

- **站内信：`kind` + `payload` 在读取时渲染**。新增 `frontend-vue/src/utils/notificationRender.ts`（纯函数，可单测），`NotificationsView.vue` 不再直接显示 `item.title/body`，改调 `renderNotificationTitle` / `renderNotificationBody`；三语各补 `notifications.body.*` 与 `title_*` 模板。于是**历史通知也会跟随语言切换**。
- **邮件：服务端按收件人语言渲染**。新增 `internal/notify/messages.go`（`RenderMail` / `RenderBody` / `RenderTitle` / `RenderPackagePayment` / `QuotaActionLabel`），`sendMail` 先取 `userLocale` 再渲染。四类事件的三语模板与前端逐条对应。
- **用户语言来源**：`PGStore.UserLocale` 读 `users.settings::jsonb ->> 'locale'`（列是 `json`，必须显式转型；未设置返回空串）。前端新增 `utils/localeSync.ts`：`setLocale()` 经 hook 回写 `PUT /v1/auth/profile {settings:{locale}}`（失败静默），登录后仅在本机未显式选择过语言时采用服务端值。
- **为什么 i18n 不直接调 api**：`api/index.ts` 已 import i18n 的 `t`（拦截器翻译错误），反向依赖会成环 —— 改为 i18n 暴露 `setLocaleSyncHook`，由 `main.ts` 注入。同理 `localeSync` 不 import auth store，登录态判定由参数注入。

**为什么没加 `render_key`（偏离原计划）**：原计划写的是「`0012` 加 `render_key` 列并回填」。核实后发现 `payload` 已带齐重建四类正文的全部变量，而 `render_key` 可由 `kind`（+`package_code` 是否存在）完全导出、**反向推不出来**，等于给同一语义维护两份真相；且 `0011` 文件头取舍 6 本就写明「不存渲染后的 HTML，由前端按当前语言渲染」。因此**不新增迁移，head 仍是 `0011`**。

**验收（均已通过）**：切到 en-US 后历史通知正文随之变英文（`notificationRender.spec.ts` 用真实 i18n 实例钉住）；ar 同理；`settings` 无 `locale` 时回落不影响渲染；邮件正文语言与该用户偏好一致（`messages_test.go`）；三语键集对齐 0 缺键。

**顺带修掉的缺陷**：`RenderBody` 原先只校验参数个数，挡不住「个数对、类型错」时 `fmt` 产出 `%!d(string=...)` 半成品 —— 改为渲染后检测 `%!` 即回落兜底文本。

**已知边界**：`package_name` / `tenant_name` 是运营填的业务数据，英文界面原样展示、不翻译；未知 `kind` 走服务端兜底文案而非空串。

### T2 通知表保留期清理（**已交付**，2026-10-30）

**原状（已核实）**：`0011_notifications.py` 只有三个读路径索引（`ux_notifications_user_dedupe`、`idx_notifications_user_created`、未读部分索引），**没有任何删除或保留期机制**；全仓 `DELETE FROM notifications` 只出现在测试清理里。四类事件里 `quota_changed` 会按租户 owner/admin 数量成倍写入，通知表是**只增不减**的。

**实际做法**：

- **迁移 `0012_notifications_retention`**：只加一个 `idx_notifications_created`（btree, `created_at`）单列索引。原计划的「两个索引都以 `user_id` 打头」核实后其实有三个，结论不变：跨用户按时间删除一个都用不上。刻意**不带 WHERE**（清理不区分已读未读，部分索引会让查询用不上它），也不建复合索引、不加 `expires_at` 列（保留期是纯派生规则，加列会多一份可能与 `created_at` 不一致的真相）。
- **`Store.DeleteOlderThan(ctx, cutoff, batchSize)`**：PG 的 `DELETE` 不支持直接写 `LIMIT`，用「子查询按 `created_at` 排序 `LIMIT n` → 按 id 删」一条语句绕开；每批独立事务，不把整轮清理包在长事务里（否则行锁会把并发写入堵住）。`batchSize <= 0` 时防呆为 1000 —— 否则 `LIMIT 0` 会静默什么都不删，调用方误以为已删空。
- **`Manager.SweepRetention` / `StartRetentionSweep`**：单批 1000、单轮最多 50 批（≈5 万行/轮，超出则本轮停、下轮继续，避免长时间占用连接池）；不足一批即提前结束；每批前检查 `ctx`。网关以 24h 周期启动，保留天数由 `NOTIFICATION_RETENTION_DAYS` 控制（默认 90；`<=0` 回落默认值而非「立即删光」）。

**取舍**：**已读与未读一视同仁**。90 天没点开过的通知，保留下去既不会被看也不会被清理，等于永久垃圾；「怕丢重要通知」的诉求应由「重要事件同时发邮件」承担，而不是靠永不删除。

**验收（均已通过）**：真库 120 天前的行被删、89 天前的留下（逐条确认哪一条还在，而非只看数量）；2500 行跨 3 批删空；`pg_indexes` 断言 `idx_notifications_created` 存在且 `EXPLAIN` 可执行；`go test ./...` 21 包全绿。

**与计划的两处偏离**：① 边界用 **120/89 天**而不是 121/90 天 —— 90 天恰好是 cutoff 边界，执行耗时的秒级抖动会让它滑到任一侧，断言会随机变红；② 原写「未装配通知中心时 sweep 为 no-op」，但清理不依赖 notifier（与 `ScanEntitlementExpiry` 不同），「未装配」对它不适用，改为断言 **nil store 会 panic 而非静默返回 0** —— 静默返回 0 会让「清理从没跑过」看起来像「没有过期数据」，是最难排查的一类故障。

### T3 通知入口与偏好可见性（**已交付**，2026-10-30）

**原状（已核实）**：顶栏角标（`AppLayout.vue` 的 `.topbar-notify`）与 `/notifications` 路由已可用，但**账号菜单里没有入口**，用户只能靠点铃铛进入；偏好设置藏在页面下半部分，没有独立锚点。

**核实中发现的计划外缺口**：账号菜单有**两份独立实现** —— `AppLayout.vue` 与 `chat/ChatSidePanel.vue` 各写一份，且已漂移（聊天页那份只有 settings/profile/logout）。而聊天页**隐藏了顶栏**（`isChatPage`），所以聊天过程中的用户点不到任何通知入口。只按原计划改 `AppLayout` 会留下这个洞，而通知最常见的场景正是「对话时收到支付成功 / 套餐到期提醒」。

**实际做法**：

- **抽出 `components/accountMenu.ts`**：`buildAccountMenuItems(tr, {isLocal, includeApiKeys})` 与 `accountMenuRoute(key)` 成为菜单项的唯一定义处；顶栏与聊天侧栏都改为引用它。`api-keys` 在聊天页不展示，但这个差异改为**显式参数** `includeApiKeys: false`，不再是两份实现各自漂移的产物。
- **跳转逻辑收敛**：两个组件的 `handleUserMenuClick` 都改为「logout / settings 特判，其余查 `accountMenuRoute`」，新增菜单项时不会漏一个分支导致点了没反应。
- **偏好区加锚点**：`section#notification-prefs` + 工具栏「邮件设置」按钮滚动过去；支持 `/notifications#notification-prefs` 外部直达（`nextTick` 后滚动，因为偏好区在列表之后可能尚未挂载）。刻意用 `scrollIntoView` 而非 `router.push('#...')` —— 后者会新增历史记录且 hash 残留，纯页内定位不该污染浏览历史。
- 三语补 `notifications.prefs_jump`。

**验收（均已通过）**：账号菜单可见「通知中心」并能进入，顶栏与聊天侧栏都有；本地模式不展示通知/api-keys/登出；`accountMenu.spec.ts` 12 项覆盖可见性规则与三语 label（用真实 i18n 实例，能抓住「加菜单项忘补译文」）；`vue-tsc` 零错误、三语键集对齐。

**顺带修掉**：两份菜单的图标导入已按实际使用清理（`LogoutOutlined` / `UserSwitchOutlined` / `KeyOutlined` 移出 `AppLayout`，`h` / `VNode` 在 `ChatSidePanel` 不再需要）。

**已知边界**：本步（T3）只覆盖菜单构造与路由映射两个纯逻辑点。通知页组件本身的页面级单测**已补齐**：`views/__tests__/NotificationsView.spec.ts` 10 个用例，钉住「列表按当前语言渲染 / 未知 kind 原样显示 / 切未读带 `unread_only` 重新拉取 / 点击标已读并按 link 跳转 / 标读失败不阻止跳转 / 无未读时只提示不刷新 / 加载失败可重试 / 邮件开关乐观更新与失败回滚 / 「邮件设置」按钮滚动且不污染历史 / `#notification-prefs` 直达」。

### T4 起：受外部条件阻塞的技术债

| 项 | 缺什么 | 能否现在做 |
|---|---|---|
| L4-3 `timestamp` → `timestamptz` | 生产时区语义抽样 + 可达 PG；`0001` 基线仍有 **98 处** naive 列，且同一列可能混两种语义 | 否 |
| S5-(e) 沙箱部署形态 | 独立容器（无 docker socket、仅内网）+ 共享工作区卷 | 否 |
| L3-4 巨型文件拆分尾批 | **已改为「可做」，但优先级最低**：本轮补写了 `docs/split-assessment.md`（此前 7 处引用指向一个从未提交过的文件），边界与验收已就位 | 可以 |

**本轮对 T4 的实际推进（2026-10-30）**：三项里两项确有硬阻塞（`docker info` 实测失败；L4-3 缺生产时区抽样，盲改会把一半行移错时刻），因此**不动代码**，改为补写缺失的 `docs/split-assessment.md`：

- 用本会话实测数据重写（Go / Vue / Python 三侧行数与顶层符号数），并发现旧数字已明显过期 —— `runtime.py` 文档旧记 3370→3198，**实测 4498 行**；`ChatView.vue` 旧记 3047，**实测 3966 行**；
- 坐实一件事：`beb7bbb` 的提交信息写着「新增 `docs/split-assessment.md`」，但该提交 `--stat` 只改了 roadmap 一个文件，`git log --all -- docs/split-assessment.md` 返回 **0 条** —— 那 7 处引用（含 `ChatView.spec.ts` 的 §2.5、`main.py` 与 `test_shutdown_lifespan.py` 的 §2.3）一直指向不存在的文件。现已按实测补写，节号有真实落点；
- 文档自带 §6 复算命令与时效性警告，避免再次过期。

### 建议顺序

`T1` → `T2` → `T3` → `T4`。

- **T1 排第一**：它是唯一一处**已经违反项目硬约定**的地方（三语对齐），且邮件出口因此对所有非中文用户不可用，属于「用不了但没提示」。
- **T2 排第二**：不是正确性问题，但 `quota_changed` 的写入倍数让增长比其它事件快；越晚做，首次清理要处理的历史越多。
- **T3 排第三**：纯入口便利，不做也不影响功能可达（铃铛已可用）。
- **T4 部分推进**：L4-3 与 S5-(e) 确有硬阻塞（已实测），故本轮只补写了缺失的拆分评估文档；代码本身未动。

> T1–T3 都不需要新的外部依赖，也不改既有表语义；`0011`（通知中心）与 `0012`（保留期索引）已用掉，**下一个可用号从 `0013` 起**。

## 16. 下一批开发任务（2026-10-30，L3-4 批次 A 之后）

### 16.1 事实基线（已核实，不是推断）

| 维度 | 实测 |
|---|---|
| 功能线 | P0-1/2/3、P1-1/2/3、P2-1/2/3 **全部已交付**（§1 现状表逐行标注） |
| 未提交改动 | **45 项**，跨 P2-2 / P2-3 / T1–T4 / L3-4 六个主题 |
| L3-4 批次 A | 已拆 **11 个文件、2997 行逐字未变外移**（§4.1）；本轮增补 `plugin_handler.go` / `agents.go` / `rpa_ws.go` / `sso_handler.go` |
| 剩余巨型 Go 文件（实测 `wc -l`） | `admin_ops.go` 950 · `submit_handler.go` 935（**跳过**）· `sso_handler.go` 888 · `billing_handler.go` 815 · `sms_handler.go` 739 · `auth.go` 731 · `ent_costcenter_handler.go` 725 · `rpa_ws.go` 708 · `admin.go` 704 · `mail_handler.go` 695 · `plugin_handler.go` 693 · `agents.go` 670 |
| 剩余巨型前端文件 | `ChatView.vue` **3966** · `MediaView.vue` 1842 · `ChatInput.vue` 1628 · `BillingView.vue` 1566 · `WorkflowView.vue` 1364 · `MessageItem.vue` 1291 · `SettingsPanel.vue` 1236 · `ChatSidePanel.vue` 1228 · `AgentsView.vue` 1182 |
| 已知测试盲区 | **已补齐**：`views/__tests__/NotificationsView.spec.ts` 10 用例（全量前端 101 文件 / 824 用例全绿） |

### 16.2 可立即做（无需外部条件）

| 序 | 任务 | 状态 / 为什么排这个位置 |
|---|---|---|
| **1** | **把这 45 项整理成 5 个提交**（P2-2 套餐 / P2-3 通知中心 / T1 本地化 / T2 保留期 / T3 入口 + L3-4 拆分） | **未执行 —— 用户已明确「不要提交」。** 方案见 §16.6，随时可落。风险仍是：45 个文件叠加在一个未提交 diff 上，跨界越多，「哪一行属于哪个主题」越不可考 |
| **2** | **L3-4 批次 A 收尾：再拆 4 个文件** | ✅ **已完成（2026-10-30）**：`python_client.go` 817→631（183 行）· `plugin_handler.go` 837→693（143 行）· `agents.go` 922→670（250 行）· `rpa_ws.go` 860→708（152 行），四处均逐字校验 PASS + 全仓 build/vet/test 绿 |
| **3** | **`NotificationsView.vue` 页面级单测** | ✅ **已完成（2026-10-30）**：`views/__tests__/NotificationsView.spec.ts` 10 用例，全量前端 101 文件 / 824 用例全绿，`vue-tsc -b` 零错误 |
| **4** | **前端巨型文件**（`MediaView` 1842 / `ChatInput` 1628 / `BillingView` 1566） | 未开始。都可以照 `ChatView` 的路径走（抽 composable），但 `ChatView` 本身 3966 行且 §2.5 已标注**需测试网前置**。建议先做 `BillingView`（它有 `billing/*.spec.ts` 现成测试，改坏会被立刻抓住），后做 `ChatInput`（依赖 ConversationStore，需要先补桩） |

**明确跳过**：`submit_handler.go`（935 行 / 仅 17 个顶层符号）。它不是「文件大」，而是少数超长函数承载一次 SSE 回合的主流程；拆函数体会改动执行时序，§2.2 已标注「暂不动」。行数不是唯一指标。

### 16.3 需要你先拍板（产品语义问题，我不替你定）

| 待定项 | 两种选择 | 影响面 |
|---|---|---|
| `AgentsView.vue` 是否隐藏他人共享 Agent 的编辑/删除按钮 | ① 隐藏按钮 ② 允许编辑（原行为）—— **已决策：都不选**，改为「保留但置灰 + 提示仅创建者可编辑」 | 不选 ①：保留可发现性，让用户理解为什么不能点，而不是凭空少两个入口。不选 ②：后端本就不允许 —— `agents.go:367`（update）、`:387`（delete）、`:551`（visibility）三处 SQL 都带 `AND user_id = $N`。实现见 §17.5 第 4 项 |
| `internal/auth/local.go:81 CreateUser` | ① 保留并让注册流程走它 ② 从接口与实现一并删除 | **事实更正（本轮实测）**：它不是「未导出的死函数」——`internal/auth/interface.go:27` 把它声明为 `AuthService` 接口方法，实现也在。但全仓 `.CreateUser(` 调用点为 **0**：注册实际走的是别的路径。删除要从接口 + 实现 + 各自的测试替身三处一起动，不是删一个函数 |

> 原第三项「`migrations/versions/*.pyc` 是否清理」**已无需决策**：实测该目录下 `*.pyc` 数量为 **0**。

### 16.4 仍受外部条件阻塞（不改代码）

| 项 | 缺什么 |
|---|---|
| L4-3 `timestamp` → `timestamptz` | 生产时区语义抽样 + 可达 PG。`0001` 基线仍有 **98 处** naive 列，同一列可能混两种语义，盲改会把一半行移错时刻 |
| S5-(e) 沙箱部署形态 | 独立容器（无 docker socket、仅内网）+ 共享工作区卷。本机 `docker info` 实测失败 |

### 16.5 建议顺序

`1` → `2` → `3` → `4`，理由：

- **第 1 项必须最先**：它不产生新代码，只把已验收的工作固化成节点。之后每一批拆分都能独立提交、独立回滚 —— 这也是继续拆 `python_client.go` 等文件的前提（否则拆分自身又会混进同一个巨型 diff）。
- **第 2 项优先于前端**：同包纯移动的验收成本最低（逐字比对 + 全仓门禁），且边界判据已验证两轮，边际风险最小。
- **第 3 项紧接着**：`NotificationsView` 是唯一「新写却无测试」的页面，趁上下文还热补完，比事后按现象回查便宜。
- **第 4 项最后**：前端 composable 抽取需要先建测试网，是这批里唯一成本不确定的。
- **§16.3 两项与上面并行不冲突**：都不阻塞第 1–4 项，但定得越早，`AgentsView` 的改动越不容易返工。

### 16.6 第 1 项的具体方案：把 45 项拆成 5 个提交

已实测确认：`HEAD` 是 `748a3e7`（P2-3 主体已入库），工作区剩 **23 个修改 + 22 个未跟踪**，其中 4 个文件跨主题、必须按 hunk 分块暂存。

**提交顺序与内容**：

| # | 提交信息 | 文件 |
|---|---|---|
| 1 | `feat(notify): 通知内容按用户语言渲染` | T1 全部：`internal/notify/messages.go`(新) · `messages_test.go`(新) · `locale_live_test.go`(新) · `frontend-vue/src/utils/notificationRender.ts`(新) + 其 spec · `utils/localeSync.ts`(新) + 其 spec · `i18n/index.ts` · `main.ts` · `stores/auth.ts` · `billing/payments.go` · `api/notify_mailer.go`（仅修一个乱码字符） |
| 2 | `feat(notify): 通知表保留期分批清理` | T2 全部：`migrations/versions/0012_notifications_retention.py`(新) · `internal/notify/retention.go`(新) · `retention_test.go`(新) · `retention_live_test.go`(新) · `config/config.go` · `api/gateway_router.go` |
| 3 | `feat(frontend): 统一账号菜单并补通知入口` | T3 全部：`components/accountMenu.ts`(新) + 其 spec · `components/AppLayout.vue` · `components/chat/ChatSidePanel.vue` |
| 4 | `refactor(api,auth): 按职责外移六个巨型文件的子域` | L3-4 批次 A：6 个新文件 + 6 个原文件的删除块 · `docs/split-assessment.md`(新) |
| 5 | `docs(roadmap): 记录 T1–T4 与 L3-4 交付并规划下一批` | `docs/saas-experience-roadmap.md` |

**必须按 hunk 拆的四处**（否则会把两个主题混进一个提交）：

| 文件 | 主题 A 的 hunk | 主题 B 的 hunk | 手法 |
|---|---|---|---|
| `internal/notify/notify.go` | `Store` 接口加 `DeleteOlderThan`（T2） | `sendMail` 改渲染 + `userLocale` + `LocaleResolver`（T1） | 三处位置不重叠，`git add -p` 可直接逐块选 |
| `internal/notify/pgstore.go` | `import "time"` + `DeleteOlderThan` 实现（T2） | `UserLocale` 实现（T1） | ⚠ **`UserLocale` 与 `DeleteOlderThan` 落在同一个 50 行 hunk 内**，`-p` 里要按 `s` 拆再选，或 `e` 手工剪开 |
| `frontend-vue/src/views/NotificationsView.vue` | `prefsSection`/`scrollToPrefs`/`route.hash`/工具栏按钮/`id`+`ref`（T3） | `renderTitle`/`renderBody`/`formatDateOnly` + 模板改渲染（T1） | 两处相邻但可分 |
| `frontend-vue/src/locales/{zh-CN,en-US,ar}/notifications.ts` | `body.*` + `title_*`（T1） | `prefs_jump`（T3） | 两个独立 hunk，可分开 |

`internal/notify/notify_test.go` 的改动**全部属 T2**（`fakeStore` 补 `DeleteOlderThan` 及其计数器），可整文件提交。

**为什么这个顺序能编译**：T1 的 `messages_test.go` 里 `localeAwareStore` 嵌入 HEAD 已有的 `fakeStore`，在 T1 时 `Store` 接口还没有 `DeleteOlderThan`，可编译；到 T2 时该接口加方法、`fakeStore` 同步补上，嵌入者自动满足。反向顺序同样成立，因此 1↔2 可互换 —— 但 **T3 必须排在 T1 之后**，因为 `accountMenu.ts` 的三语 label 依赖 T1 补齐的键集。

**每步的必做验证**（hunk 级拆分最容易留下编译不过的中间态）：

```
go build ./... && go vet ./... && go test ./... -count=1
python scripts/check_source_encoding.py && python scripts/check_md_tables.py
cd frontend-vue && npx vue-tsc -b && node scripts/check-i18n-keys.mjs && npm test
```

提交 4（纯移动）另加逐字校验：`$TMPDIR/l34_verify_{move,cost,identity,sso,paypal,redis,mime}.py` 七个脚本均需 `PASS`。

> 本方案未执行任何 `git add` / `commit` —— 按约定改动留工作区。上面「为什么这个顺序能编译」是基于 hunk 位置与接口依赖的推理，**尚未实际分块验证**；执行时若某步编译不过，先跑该步的验证再继续，不要连做五个提交。

## 17. 下一批开发任务（2026-10-30，L3-4 尾批与前端补测之后）

> 本轮（§16.2 的第 2、3 项）已交付：四个 Go 文件拆成七个（`python_client_addr.go` / `plugin_validation.go` / `agent_context.go` / `rpa_registry.go` 等），共 **728 行逐字未变外移**，全仓 `build + vet + test` 绿；`NotificationsView.vue` 补 10 个页面级用例，全量前端 101 文件 / 824 用例绿。以下为**下一批**的落点，全部基于本会话 `wc -l` 与 `rg` 实测，不含推断。

### 17.1 事实基线（本轮实测）

| 维度 | 实测 |
|---|---|
| L3-4 批次 A 累计 | **11 个文件、2997 行逐字外移**（§4.1 表）；`submit_handler.go` 已按判据排除 |
| 剩余 Go >650 行 | `admin_ops.go` 950 · `submit_handler.go` 935（跳过）· `auth.go` 731 · `rpa_ws.go` 708 · `admin.go` 704 · `plugin_handler.go` 693 · `agents.go` 670 —— **仅剩 7 个**；`billing_handler.go` 632 · `mail_handler.go` 568 · `sso_handler.go` 559 · `sms_handler.go` 524 · `ent_costcenter_handler.go` 487 均已降破 650 |
| 剩余 Go 文件的顶层符号数 | `mail_handler` 32 · `auth` 25 · `billing_handler` 23 · `sms_handler` 22 · `admin` 20 —— **都远高于 `submit_handler` 的 17**，即仍具备「按子域切」的余量 |
| 巨型前端文件及其测试网 | `ChatView` 3966（2 spec）· `MediaView` **1842（0 spec → 新补 6 条冒烟网）** · `ChatInput` 1628（1）· `BillingView` 1566（1，13 用例）· `WorkflowView` 1364（1）· `MessageItem` 1291（1）· `SettingsPanel` **1236（0 spec）** · `ChatSidePanel` 1228（1）· `AgentsView` 1182（1）· `SubAgentPanel` 1096（1） |

**已完成的一刀（2026-10-30）**：`sso_handler.go` 的「身份绑定」子域 —— `handleBindCallback` / `ListIdentities` / `DeleteIdentity` / `setPasswordRequest` / `SetPassword` / `findIdentityUser` / `provisionAndBind` / `resolveRole` 八个符号 **326 行逐字外移到 `sso_identity.go`**，原文件 888 → 559。`l34_verify_any.py` 逐字校验 PASS，全仓 `build + vet + test` 绿。外移时原文件失效的 `crypto/rand` / `encoding/hex` / `golang.org/x/crypto/bcrypt` 三个 import 已按 `rg -c` 零引用核实后删除。

### 17.2 可立即做（按风险从低到高）

| 序 | 任务 | 依据与做法 |
|---|---|---|
| **1** | ✅ **`sso_handler.go` 888 → 559，拆出身份绑定子域** | **已完成（2026-10-30）**：八个符号 326 行逐字外移到 `sso_identity.go`（`provisionAndBind` + `handleBindCallback` 构成「外部身份 ↔ 本地用户」闭环，`resolveRole` 为纯函数），`l34_verify_any.py` PASS，全仓 build/vet/test 绿 |
| **2** | ✅ **`MediaView.vue` 1842 → 1703，拆出批量上传子域** | **已完成（2026-10-30）**：测试网 `views/__tests__/MediaView.smoke.spec.ts` 6 用例先行（默认请求参数、三态互斥、点文件夹带 `parent_id` 重拉、点文件只开详情不重拉）——**建网当场抓到一个既有缺陷**：模板用 `<Progress>` 做上传进度条但从未导入，浏览器里进度条一直是未解析标签，已修。随后把上传簇（2 个状态 + 6 个函数，148 行）**外移到 `composables/useMediaUpload.ts`**（182 行），`parentId` 与 `onUploaded` 由调用方注入 —— 目录上下文与重拉列表属视图职责。6 用例仍全绿，`vue-tsc -b` 零错误，前端全量 102 files / 830 tests 通过 |
| **3** | ✅ **`BillingView.vue` 1566 → 1464，拆出发票申请子域** | **已完成（2026-10-30）**：发票相关的 2 个类型 + 5 个状态 + 5 个函数**外移到 `composables/useBillingInvoices.ts`**（151 行）。`formatTime` 由调用方注入，避免同一格式化逻辑出现两份口径。实测该 spec 为 **12 个用例**（此前记为 13），抽完后仍全绿；`vue-tsc -b` 零错误，前端全量 102 files / 830 tests 通过。**注意**：「支付二维码」未切 —— 它与 `CreatePayment` 主流程双向耦合（`CreatePayment` 直接写 `qrCode`/`orderId`/`qrVisible` 等 6 个状态），拆它要改写入方，不是纯移动 |
| **4** | ✅ **`mail_handler.go` 695 → 568 + `sms_handler.go` 739 → 524** | **已完成（2026-10-30）**：`mail_config_rules.go` 126 行纯函数（配置行边界校验与补丁应用：`validateMailConfigBounds`/`applyMailConfigPatch`/`isHTTPURL`/`derefString`/`ttlMinutes`/`resetTTLSeconds`）、`sms_config.go` 214 行（短信通道配置读取/回显/更新）。`loadConfig` 被发码链路共用，按「谁依赖谁」判据留在原文件 |

**Go 侧到此收尾（2026-10-30）**：本轮共完成 11 个文件的第二次及以后拆分，`>650 行` 的 Go 文件只剩 7 个，其中 `admin_ops.go`（950）、`auth.go`（731）、`admin.go`（704）都是「单个巨型函数撑起文件」，继续拆要动函数体内部时序，风险大于收益 —— 与 `submit_handler.go` 同理。**后续拆分投入应转向前端**。

**明确跳过**：`submit_handler.go`（935 行 / 17 符号）—— 拆函数体会改 SSE 回合的执行时序，§2.2 已标注「暂不动」。

### 17.3 需要你先拍板

| 待定项 | 卡住什么 |
|---|---|
| `AgentsView.vue` 共享 Agent 是否隐藏他人编辑/删除按钮 | 决定前端要不要改、以及后端是否同步拒绝。**这是 §16.3 遗留项，未定之前第 3 项应避开 `AgentsView`** |
| `CreateUser` 保留还是从接口删除 | **已决策（2026-10-30）：删除**。已从 `interface.go:26-27` 声明与 `local.go:81` 实现两处删净（全仓无测试替身实现它，故只动两处）。`go build ./...` + `go vet ./...` + `go test ./internal/auth/` 全通过 |

### 17.4 仍受外部条件阻塞（不改代码）

| 项 | 缺什么 |
|---|---|
| L4-3 `timestamp` → `timestamptz` | 生产时区语义抽样 + 可达 PG（`0001` 基线 98 处 naive 列，同一列可能混两种语义） |
| S5-(e) 沙箱部署形态 | 独立容器 + 共享工作区卷；本机 `docker info` 实测失败 |

### 17.5 建议顺序

**Go 侧已收尾（见 §17.2 末），前端成为唯一主线。**

1. ✅ **`BillingView.vue` 抽 composable —— 已完成（2026-10-30）**，详见 §17.2 第 3 项：发票子域外移到 `composables/useBillingInvoices.ts`（151 行），1566 → 1464。**「支付二维码」未切** —— 它与 `CreatePayment` 主流程双向耦合（`CreatePayment` 直接写 `qrCode`/`orderId`/`qrVisible` 等 6 个状态），拆它要改写入方，不是纯移动。该项就此关闭，不再排期。
2. ✅ **`MediaView.vue` 抽 composable —— 已完成（2026-10-30）**，详见 §17.2 第 2 项：批量上传子域外移到 `composables/useMediaUpload.ts`（182 行），1842 → 1703。**建网当场抓到一个既有缺陷**：模板用 `<Progress>` 做进度条但从未导入，浏览器里一直是未解析标签。「预览 / 列表」两块未独立成 composable —— 它们与 `resolveItemUrl`/`fetchItems` 的 URL 解析和目录上下文深度共享，强行切会造出一个参数比返回值还多的接口。
3. ✅ **`SettingsPanel.vue` 测试网已建（1236 行 / 0 spec → 7 条）** —— `src/components/settings/__tests__/SettingsPanel.smoke.spec.ts`。覆盖前两个子域：「账户资料」4 条（初始值取自登录用户、邮箱只读、保存载荷与成功后刷新、失败不刷新）+「三方账号绑定」3 条（空态、已绑定 provider 从可绑定列表过滤、解绑后绕过缓存重拉）。
   **一条踩坑记录（值得复用）**：antd Tabs 在 vitest 里**未被 `unplugin-vue-components` 解析**（该插件只在 `vite.config.ts`，`vitest.config.ts` 没有），因此所有 `a-tab-pane` 的内容都同时挂在 DOM 上。第一版断言直接 `findAll('input')[0]` 与 `findComponent(EmptyState)`，命中的是「通用」页签里的强调色输入框与空态 —— **测试"通过"了但断言的是别处**。修法是按 `.setting-block` + 标题文案把作用域收窄到目标区块（`EmptyState` 全文 5 处、`Popconfirm` 4 处，全局取第一个必然出错）。
   **后续若要在测试里正确渲染 antd 组件**，可给 `vitest.config.ts` 加同一个 `Components({ resolvers: [AntDesignVueResolver()] })`；当前不改配置，用区块作用域已足够。
   下一步才是按六个子域拆 composable。
   **已完成第一步（2026-10-30）**：「三方账号绑定」已抽出 → `composables/useSettingsBindings.ts`（115 行，8 个导出：4 状态 + `loadBindings`/`openBindings`/`startBind`/`handleUnbind`），`SettingsPanel.vue` 1236 → 1157 行。选它先切是因为它刚被 3 条用例护住，且缓存策略（`openBindings` 延后+命中缓存 vs `loadBindings` 强制绕过）是本子域真正的复杂度，抽出去后视图只剩解构。原文件同步删掉 6 个失效的 auth import（`listIdentities`/`deleteIdentity`/`listPublicSsoProviders`/`ssoLoginURL`/`UserIdentity`/`SsoPublicProvider`），先 `rg -c` 核实均为 1（只剩 import 行）再删。7 条用例仍全绿，`vue-tsc -b` 零错误，前端全量 103 files / 837 tests 通过。「账户资料」子域太小（1 函数 + 2 状态）不值得单独抽。
   **第二步（2026-10-30）**：「手机号绑定」已抽出 → `composables/useSettingsPhone.ts`（124 行，12 个导出：7 状态 + `loadPhone`/`handleSendBindCode`/`handleBindPhone`/`handleUnbindPhone` + `phoneCountdown`），`SettingsPanel.vue` 1157 → 1084 行。这是本面板唯一的状态机：发码成功后按服务端下发的 `interval` 启动 60 秒倒计时，倒计时期间发码按钮禁用。抽时原样保留两处容易改错的口径：① `loadPhone` 失败必须**静默**（短信服务未配置时藏起入口，而不是报错让用户以为系统坏了）；② 发码看 429/403（太频繁／未开通短信）、绑定只看 409（号码已绑其他账号），两类失败分类**不同**。同步删掉 7 个失效 import（`getSmsBind`/`bindPhone`/`unbindPhone`/`sendSmsCode`/`isValidPhone`/`errorStatus`/`useSmsCountdown`），删前 `rg -c` 核实均为 1。7 条用例仍全绿，`vue-tsc -b` 零错误，前端全量 103 files / 837 tests 通过。
   **第三步（2026-10-30）**：「数据管理：分享」已抽出 → `composables/useSettingsShares.ts`（107 行，8 个导出：3 状态 `shares`/`sharesLoading`/`revoking` + `loadShares`/`openShares`/`handleRevokeShare`/`shareURL`/`copyShareURL`，并把 `ShareItem` 接口一并搬走），`SettingsPanel.vue` **1084 → 1018 行**。与三方绑定同构，两套入口仍不可合并（`openShares` 延后+命中缓存 vs `loadShares` 强制绕过）。值得缓存的理由写在 composable 注释里：`GET /v1/shares` 按 `user_id` 扫 `conversation_shares` 全表（基线只有主键索引），还要对每行 `array_length` 聚合算消息数，而用户多半只是路过设置页。注意**撤销分享是按会话维度**的（`DELETE /v1/conversations/{session_id}/share` 会把该会话所有有效分享一起标记 revoked），不是按 `share_id` 单条删——这点容易看反。`tallyCache`/`scheduleTally`/`serverErrorMessage`/`api` 仍被会话块使用，故保留 import，只 `ShareItem` 引用归零。7 条用例仍全绿，`vue-tsc -b` 零错误，前端全量 103 files / 837 tests 通过。
   **第四步（2026-10-30）**：「数据管理：会话导出与删除」已抽出 → `composables/useSettingsConversations.ts`（193 行，13 个导出：5 状态 + `allSelected`/`selectedCount` 两个 computed + `loadConversations`/`openConversations`/`conversationToMarkdown`/`exportSelected`/`deleteSelected`/`toggleSelectAll`，`ConversationItem`/`ExportMessage` 一并搬走），`SettingsPanel.vue` **1018 → 883 行**。这是四块里唯一有**跨子域依赖**的：`deleteSelected` 末尾原本直接调 `loadShares()`（删掉会话，挂在它上面的分享自然不该再显示）。没有让两个 composable 互相 import，而是按 `useMediaUpload({ parentId, onUploaded })` 的既有做法改成注入回参 `useSettingsConversations({ onSessionsDeleted: loadShares })` —— 两个子域各自独立可测，谁也不需要知道对方内部怎么实现。
   抽时原样保留两个容易改错的口径：① **导出串行**逐会话拉详情（低频操作，串行避免瞬时打满后端）；② **删除部分失败要如实报告**（后端无批量接口，逐个删，`failed.length` 非空时走 `deleted_ok_failed_failed` 提示成功/失败分开计数，并把 `selectedConvIds` 置为失败项，不谎报全部成功）。同步清理 3 个失效 import（`tallyCache`/`scheduleTally`/`computed`），删前 `rg -n` 逐行核对确认只剩 import 行。7 条用例仍全绿，`vue-tsc -b` 零错误，前端全量 103 files / 837 tests 通过。
   **至此 `SettingsPanel.vue` 的四个大子域（三方绑定 / 手机号 / 分享 / 会话）全部抽出**，1236 → 883 行。剩余在视图内的只有「账户资料」（1 函数 2 状态）、「设置密码」（1 函数 2 状态）、「朗读音色」（3 函数 3 状态）与主题偏好 —— 这三块都太小，抽出去后 composable 比留在视图里更难读，**判断为不值得拆**。
4. **`AgentsView.vue`** —— ✅ **已完成（2026-10-31）**。按 §17.3 的决策，编辑/删除按钮保留但置灰 + 提示「仅创建者可编辑」。
   - 新增 `canManage(a: AgentRow)` 与 `currentUserId`（取自 `useAuthStore().user?.id`），编辑、删除两个 `MenuItem` 各加 `:disabled="!canManage(a)"` 与 `:title`。
   - **两处刻意不误锁**：① `a.user_id` 缺失（老数据 / 后端未回填）→ 放行；② 当前用户 id 拿不到 → 放行。宁可多一次 403 提示，也不要让所有者因为拿不到自己的 id 被锁在门外。这个取舍来自「前端隐藏但接口仍放行等于假安全」的反面：前端置灰只是提示，**真正的权限边界仍在后端**（`agents.go:367`/`:387`/`:551` 三处 `AND user_id = $N`）。
   - 三语新增键 `agent.only_creator_can_edit`（zh-CN/en-US/ar），`check-i18n-keys` 通过。
   - 测试：`AgentsView.spec.ts` 3 → **6 条**。新增 3 条直测 `canManage` 而非去 DOM 里找置灰的 `MenuItem` —— antd 组件在 `vitest.config.ts` 下未配 `unplugin-vue-components`，`MenuItem` 不会解析成真实组件，`disabled` 无从断言。同时给 `mountView()` 补装 pinia（此前没装，引入 `useAuthStore` 后三条既有用例全挂）。
   - `vue-tsc -b` 零错误，前端全量 103 files / **840 tests** 通过，后端 `go build ./...` + `go vet ./...` 通过。
4. **`AgentsView` 在 §17.3 定下来之前不动**：改它等于替用户决定「共享」的语义 —— **已解除**，决策已落（见上）。

**为什么 `BillingView` 排在 `MediaView` 之前**：两者的拆分本身风险相当，差别在验证成本 —— `MediaView` 现有 6 条冒烟只覆盖列表与导航，抽「上传」时无网可依；`BillingView` 的 13 个用例正对着要切的那两块。先做有网的那个，能把「拆分手法」本身先在一个安全场子里验证一遍。

**明确不动**：`ChatView.vue`（3966 行，2 个 spec 覆盖不足，风险远大于收益）；`submit_handler.go`、`admin_ops.go`、`auth.go`、`admin.go`（单巨型函数撑起文件）。

### 17.6 每项的必做验证（与批次 A 同口径）

```
# Go 纯移动
go build ./... && go vet ./... && go test ./... -count=1
python "$TMPDIR/l34_verify_any.py" <HEAD路径> <新文件路径> <符号逗号列表>   # 必须 PASS
python scripts/check_source_encoding.py && python scripts/check_md_tables.py

# 前端
cd frontend-vue && npx vue-tsc -b && node scripts/check-i18n-keys.mjs && npm test
```

> 逐字校验脚本只认**符号名**、不认行号 —— 行号在 HEAD 与工作区之间必然漂移，硬编码行号会直接 ABORT。

## 18. 下一批开发任务（2026-10-31，§17 四项全部交付之后）

> **§17 已全部落地**：1 `BillingView` 发票子域 ✅、2 `MediaView` 上传子域 ✅、3 `SettingsPanel` 四个大子域 ✅、4 `AgentsView` 属主判定 ✅。另修掉三处 CI 门禁漂移（见 §18.4）。以下为**下一批**，全部基于本会话 `wc -l` 与 `rg` 实测。

### 18.1 事实基线（2026-10-31 实测）

**判据先说清楚：行数不是排序依据。** 真正决定「好不好拆」的是**脚本段行数 × 既有用例数** —— 脚本段才是能外移的部分，模板段基本原样留在视图里；用例数决定动手时有没有网。

| 文件 | 总行数 | script / template / style | spec 用例 | 判据 |
|---|---|---|---|---|
| `MessageItem.vue` | 1291 | **597** / 361 / 331 | **34** | **首选**：脚本段最大且网最厚 |
| `ChatInput.vue` | 1628 | **941** / 420 / 264 | 6 | 脚本段最大，但网偏薄 |
| `WorkflowView.vue` | 1364 | 388 / **846** / 127 | **2** | 846 行是模板 ⇒ 能外移的只有 388 行；用例仅覆盖模板市场 |
| `ChatSidePanel.vue` | 1228 | 420 / 546 / 258 | 13 | 可选，脚本段中等 |
| `MediaView.vue` | 1703 | — | 6 | 已切一刀（上传），见 §17.2 |
| `BillingView.vue` | 1464 | — | 12 | 已切一刀（发票），见 §17.2 |
| `ChatView.vue` | 3966 | — | 2 | **明确不动**（网太薄，见末段） |

**这张表纠正了 §17.1 的一个误判**：§17.1 按总行数把 `WorkflowView`（1364）排在 `MessageItem`（1291）前面。实测 `WorkflowView` 的 846 行模板是不可外移的，真正可拆的脚本只有 388 行，且只有 2 个用例 —— 按总行数排会先啃最硬的那块。

### 18.2 可立即做（按「网厚度」从厚到薄）

| 序 | 任务 | 依据与做法 |
|---|---|---|
| **1** | **`MessageItem.vue` 597 行脚本抽三块** | 34 个用例兜底，是当前唯一「网够厚、脚本够大」的文件。三个簇按符号即可切开：① **打字机 ink**（`inkChars`/`inkBatchStart`/`inkIndex`）② **等待倒计时**（`waitSeconds`/`waitLeft`/`stopWaitTimer`/`waitLabel`/`canRetry`）③ **附件 URL 解析**（`attachmentResolved`/`attachmentNameParts`/`attName`/`attachmentUrl`/`resolveAttachmentUrls`/`onAttachmentImgError`）。倒计时簇带 timer，必须在 `onUnmounted` 里清 —— 抽出去后由 composable 自己管生命周期，不要留给视图 |
| **2** | **`ChatInput.vue` 941 行脚本**，但须**先补网** | 脚本段是全部候选里最大的，6 个用例只够覆盖输入主路径。开工第一步是补用例（附件、快捷键、IME、发送失败），**补完再切**，否则等于无网作业 |
| **3** | **`WorkflowView.vue` 节点配置簇** | `edit*` 那 20 个 `ref`（`editLabel`…`editMaxTurns`）+ `applyNodeConfig` + `onNodeClick` 构成「选中节点 → 编辑 → 回写」闭环，是最干净的边界。但现有 2 个用例如实只覆盖模板市场弹窗，**建网成本最高**，排最后 |

### 18.3 建议顺序与理由

1. **`MessageItem` 三块** —— 唯一「不用先写测试就能安全动」的文件（34 个用例），且三块边界由符号天然划开。先做它能把「抽 composable」的手法在厚网上再验证一遍。
2. **`ChatInput` 补网 + 切** —— 补网这步本身有独立价值：941 行脚本只有 6 个用例是当前最大的覆盖缺口。
3. **`WorkflowView`** —— 收益明确（846 行模板依赖的 20 个 `edit*` 状态会一起搬走），但必须先建网，成本最高。

**为什么不再碰 Go 侧**：§17.2 末已收尾 —— `>650 行` 只剩 7 个，其中 `admin_ops.go`（950）、`auth.go`（731）、`admin.go`（704）、`submit_handler.go`（935）都是单巨型函数撑起文件，继续拆要动函数体内部时序。这个结论未变。

### 18.4 门禁现状（本轮修掉的三处漂移）

**关键事实：`check:ui` 是 CI 的硬门禁。** `package.json:19` 的 `build` = `check:ui && vue-tsc -b && vite build`，而 `.github/workflows/ci.yml:162` 的前端 job 跑的就是 `pnpm run build` —— 所以任何一处门禁违规都会让整个前端 job 红。

本轮修掉三处（都是「新写代码没跟上契约」，不是存量债）：

1. `components/chat/OnboardingGuide.vue` —— `color: #fff` 裸色值 → `var(--color-on-primary, #fff)`。同文件另外 13 处色值早已是 `var(--token, 回退值)` 形式，只有这行漏了。
2. `views/NotificationsView.vue` —— `transition: background 0.15s ease` → `var(--dur-fast) var(--ease-out)`。`check-motion-tokens.mjs` 的 `BASELINE = {}` 已全清零，任何裸时长即失败；`0.15s` = 150ms 正落在 `--dur-fast`（≤150ms，颜色/边框 hover）区间。
3. `views/NotificationsView.vue` —— 通知列表项 `<li @click>` 键盘不可达 → 补 `role="button"` + `tabindex="0"` + 单一 `@keydown`。**不能写成 `@keydown.enter` + `@keydown.space` 两个**：同名事件映射到同一个 `onKeydown` prop，后声明的覆盖前者（`ChatEmptyHero.vue` 里记过同一条坑）。

**教训**：这三处都是「新写页面时只跑了 `npm test`，没跑 `check:ui`」。`npm test` 全绿不代表能过 CI —— 两者的检查项不重叠。

### 18.5 明确不做

- **`ChatView.vue`（3966 行 / 2 用例）** —— 网太薄，任何拆分都无法验证。要动它必须先把用例补到能覆盖会话主流程，那是独立一档工作量。
- **`BillingView` 支付二维码簇** —— 与 `CreatePayment` 双向耦合（直接写 `qrCode`/`orderId`/`qrVisible` 等 6 个状态），拆它要改写入方，不是纯移动。
- **`MediaView` 预览 / 列表两块** —— 与 `resolveItemUrl`/`fetchItems` 的 URL 解析和目录上下文深度共享，强行切会造出参数比返回值还多的接口。
- **`SettingsPanel` 剩余三小块**（账户资料 / 设置密码 / 朗读音色）—— 抽出去后 composable 比留在视图里更难读。

### 18.6 每项的必做验证

```
# 前端（与 §17.6 同口径）
cd frontend-vue && pnpm check:ui && npx vue-tsc -b && npm test

# 只跑单个 spec 时
cd frontend-vue && npx vitest run src/views/__tests__/<name>.spec.ts && npx vue-tsc -b

# 文档改动
python scripts/check_md_tables.py && python scripts/check_source_encoding.py
```

> 前端改动后**必须跑一次完整 `pnpm check:ui`**（九个脚本），不能只跑 `npm test` —— 理由见 §18.4 的教训。
