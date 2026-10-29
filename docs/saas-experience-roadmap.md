# SaaS 体验与功能规划

**口径**：每条都写「现状（已核实）→ 做法 → 验收」。凡是没读到实现、只有推断的，一律标「待确认」，不当作结论。

## 0. 为什么现在做这件事

工程侧已经不再是瓶颈：`docs/development-roadmap.md` §2 的待办只剩三条技术债（L4-3 时间列类型、S5-(e) 沙箱部署形态、L3-4 巨型文件拆分），全量门禁长期全绿。也就是说，继续加功能之前，"用户能不能顺畅地用起来、看得懂花了多少钱"变成了更划算的投入方向。

## 1. 现状盘点（已核实）

| 维度 | 现状 | 影响 |
|---|---|---|
| 计费模型 | 纯预付费 credits（`internal/billing/billing.go`：`Deduct`/`AddCredits`/`DeductTokens`），支付宝 + 微信 | 没有周期性套餐；用户每次都要手动充值 |
| 用量视图 | `GET /v1/billing/usage` 仅返回近 30 天**按日聚合**的 `tx_count` / `credits_spent` / `credits_added` | 用户看不出"钱花在哪个模型/哪个功能上"，这是最直接的信任缺口 |
| 账单导出 | 未找到 CSV/发票相关实现（`invoice`/`发票` 仅命中 `media_internal.go` 无关处） | 报销与对账无法自助 |
| 组织形态 | `users.tenant_id` 是 `smallint`；无 invite / member / team 表 | 单人账号制，无团队协作；改主键类型是破坏性迁移 |
| 身份能力 | 已有 OIDC（`internal/auth/oidc.go`）、SMTP（`internal/auth/mail.go`）、RBAC（`internal/enterprise/rbac.go`） | SSO 有地基，缺产品化 |
| 错误文案 | `quota_exceeded: '已达配额上限，请联系管理员'` | 自建部署口吻，SaaS 用户看到会以为系统坏了 |
| 新用户引导 | 未找到独立 onboarding 流程 | 首次登录到发出第一轮对话的路径靠用户自己摸索 |
| 自助入口 | `ApiKeysView.vue` 位于 `views/admin/`；用户侧路由仅见 `/billing` | 用户侧自助能力偏弱（**待确认**：路由可能由后台菜单动态生成） |

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

### P2-1 团队与成员

这是当前最大的结构性缺口。**不改** `tenant_id` 的 `smallint` 类型——那会波及所有带 `tenant_id` 的表。做法是新增 `tenant_members(tenant_id, user_id, role)` + 邀请表（邮件邀请复用已有 SMTP），角色直接接 `internal/enterprise/rbac.go`。

**风险**：这是多租户数据隔离的地基，一旦设计错，后面所有共享资源（知识库、Agent、插件）都要返工。需单独设计评审。

**验收**：同一 tenant 下多用户可见共享资源；角色可分配；邀请可撤销且过期失效。

### P2-2 一次性套餐包

**做法**（已按 2026-10-28 决策定为一次性，见 §6）：套餐 = 一次性发放的 credits + 权益快照（模型可用范围 / 并发 / 存储配额），到期只让权益失效，**不续期、不扣款、不发周期任务**。落地形态就是一次 `credit_transactions` 入账 + 一份带有效期的权益记录。

**为什么不做订阅**：支付宝/微信的周期扣款需要单独的签约资质，是产品与合规问题而非工程问题；而且周期任务会引入"扣款失败怎么重试""欠费如何处理"一整类状态机。一次性套餐包能用同样的表结构承载，实现量小一个量级。

**验收**：购买后 credits 与权益一次性到位；权益过期后按预期失效并提示；不出现"用不了但没提示"。

### P2-3 通知中心

额度预警、支付成功、配额变更、团队邀请统一进一张通知表，站内 + 邮件两个出口，每类事件有去重键。

**验收**：同一事件不重复推送；用户可标记已读；渠道可单独关闭。

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

### A 组：补齐首批验收缺口（先做，都是已承诺的东西）

**A1 引导完成率可查**
`GET /v1/admin/onboarding/funnel`：`SELECT step, count(*) FILTER (WHERE completed_at IS NOT NULL), count(*) FILTER (WHERE skipped_at IS NOT NULL AND completed_at IS NULL) FROM user_onboarding GROUP BY step`，分母取 `SELECT count(DISTINCT user_id)`（= 走过新注册流程的人数）。索引 `idx_user_onboarding_step` 已建，`GROUP BY step` 正是当初按行存的设计意图，不需要改表。管理端放一张漏斗卡片。
**验收**：三个步骤的完成率/跳过率可读，且分母不是全站用户数。

**A2 usage breakdown 后端限量**
给 `getBreakdownUsage` 加 `LIMIT`（建议 100，按 cost 倒序已是现有排序）+ 响应加 `truncated` 布尔。理由：模型/会话数多时 90 天全量聚合会一次性返回上千行，前端分页只是视觉上分页。
**验收**：构造 >100 个 bucket 时响应行数受限且 `truncated=true`，前端提示"仅显示花费最高的 N 项"。

**A3 `quota_exceeded` 的可点落点 + 重试按钮按 `retryable` 渲染**
配额耗尽的动作是"找租户管理员"或"升级套餐"，前者当前无处可去。最小做法：文案旁给一个「联系管理员」的复制邮箱/说明入口；同时让「重试」按钮受 `retryable` 约束（`retryable=false` 时不渲染，或降级为"稍后再试"）。
**验收**：`quota_exceeded` 不再出现"点了必然再失败"的重试按钮；三语同步。

### B 组：P1 自助与账务

**B1 交易明细 CSV 导出** —— 先同步 + 行数上限（如 5 万行）+ 流式写出，不引异步任务：仓库现有队列能力未核实到可复用（`asynq|Enqueue` 命中分散在 `admin_extras.go`/`broadcast/hub.go`，不是统一的账单导出队列）。若实测 10 万行会超时，再单独评估异步。
**验收**：Excel 直接打开无乱码（UTF-8 BOM）；超上限时明确报错而不是静默截断。

**B2 发票申请（工单式）** —— 新表 `invoice_requests`，管理员审核。不接第三方开票平台。
**验收**：同一笔交易重复申请被拦。

**B3 用户级 API Key 自助** —— 注意：与 `views/admin/ApiKeysView.vue` 无关，那是 LLM 提供商密钥（详见 §10.3）。真正要做的是给已存在且已用于鉴权的 `api_keys` 表补 CRUD。
**验收**：普通用户可自助增删轮换；越权访问他人 key 返回 404。

### C 组：P2 结构化新增（需单独立项评审）

**C1 团队与成员** —— 新增 `tenant_members(tenant_id, user_id, role)`，角色接 `internal/enterprise/rbac.go`；邀请复用已有 `tenant_invites` 与 SMTP。**不改** `tenant_id` 的 `smallint`。
**C2 一次性套餐包** —— 一次 `credit_transactions` 入账 + 带有效期的权益快照；不引入 `billing_cycle`。
**C3 通知中心** —— 新表 + 去重键，站内/邮件两个出口。

### D 组：既有技术债（`docs/development-roadmap.md`）

L4-3 时间列 `timestamp` → `timestamptz`（`credit_transactions.created_at` 正在用 `timestamp`，见 §8）、S5-(e) 沙箱部署形态、L3-4 巨型文件拆分。无功能收益，放最后。

### 顺序建议

A1 → A3 → A2（三个都是验收缺口，改动都在已有文件上，风险低）；然后 B1（用户可感知度最高）；C1 单独立项评审后再动。

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
1. 新表 `tenant_members(tenant_id smallint, user_id varchar(36), role varchar(16), joined_at, PRIMARY KEY (tenant_id, user_id))`，`role ∈ owner/admin/member`。
2. 注册事务内**同时**写 `users.tenant_id` 与 `tenant_members`（两条 INSERT 在同一 tx），保证两者不会不一致；存量用户需要一条回填迁移（`INSERT ... SELECT tenant_id, id, CASE WHEN role='owner' THEN 'owner' ELSE 'member' END FROM users ON CONFLICT DO NOTHING`）。
3. 先修 §10.1 ② 的邀请超发，再接"邀请 → 成员"。
4. 权限判定：**优先读 `tenant_members`**，`users.tenant_id` 保留为兼容字段（不删、不改类型）。这样灰度期两套数据都在，出问题可回退。

**要先回答的三个问题**（只有产品能定）：
- 一个用户能否属于多个 tenant？（建议一期**不能**，否则 `users.tenant_id` 单值字段立刻失真）
- 成员的角色与 `ent_roles` 是什么关系？（建议一期团队角色只用于**成员管理**，不接 `ent_roles`；后者是功能权限点，混在一起会让"谁是管理员"出现两个真相）
- 移除成员时，他创建的会话/知识库归谁？（建议一期**保留归属、禁止访问**，不做资产转移 —— 转移是另一套状态机）

**验收**：同 tenant 下多用户可见共享资源；角色可分配；邀请可撤销且过期失效；移除成员后其 API key 与登录会话同时失效。

**落地形态（按现有迁移规范）**：新建 `migrations/versions/0006_tenant_members.py`（`Revises: 0005_user_onboarding`），与 `0004_usage_events` / `0005_user_onboarding` 同级。灰度顺序是**先加表与回填、后切读路径**：① 建表 + 从 `users` 回填（幂等，`ON CONFLICT DO NOTHING`）；② 注册事务开始双写；③ 权限判定改读 `tenant_members`；④ 观察一轮后再考虑是否纳入更广的资源隔离。前三步任一步出问题都能靠"继续读 `users.tenant_id`"回退，不会出现两套数据都不完整的窗口。

### 10.6 建议的执行顺序（修订）

1. **B1**（无依赖、用户可感知，半天到一天）
2. **B3**（无依赖，但价值取决于是否有 API 用户；可与 B1 并行）
3. **§10.1 ② 邀请并发修复**（存量 bug，本身很小，是 C1 的前置）
4. **B2**（需要先定"部分开票/退款"的产品口径）
5. **C1**（单独立项评审；上面三个问题不定就不要开工）

**不建议**把 C1 和 B 组混在一批做：C1 动的是多租户隔离地基，一旦设计错，共享资源全部要返工。

