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

> 本节只记录**已核实的事实**与**可独立验收的任务**。所有改动仍在工作区未提交（`HEAD` = `74fa7fc`，T1/T2/T3 全部为未提交变更）。

### 12.1 进度盘点（已核实）

| 任务 | 后端 | 前端 | 迁移 | 状态 |
|---|---|---|---|---|
| T1 邀请码并发超发 | `consumeInviteQuery` 单条 `UPDATE ... RETURNING`（`auth.go:67`），窄接口 `inviteStore` | — | 无 | 代码完成；**并发未在真库验证**（`auth_invite_live_test.go` 需 `CHIRON_TEST_POSTGRES_DSN`，本机 docker 未运行 → skip） |
| T2 用户级 API Key | `GET/POST/DELETE /v1/api-keys`（`api_keys_handler.go:104-106`），越权 404、明文只回一次、每用户上限 50 | 用户侧 `views/UserApiKeysView.vue` + `/api-keys` 路由 + 账号菜单入口（T6 新建，**不是**从 admin 挪来的，见 §12.3） | `0006_api_keys_key_hash_index`（`key_hash` 部分唯一索引） | 前后端完成，P1-3 验收已闭环；`last_used_at` 已按每 key 每分钟一次节流写入 |
| T3 发票申请 | 用户 `GET/POST /v1/billing/invoices` + `GET /v1/billing/invoiceable-orders`；管理端 `GET /invoices`、`POST /invoices/{id}/review` | 用户侧 `BillingView` 发票 tab；管理端 `InvoiceReviewView` + `/admin/invoices` + 侧栏入口 | `0007_invoice_requests` | 前后端完成，9 条新用例全绿；**`INSERT ... SELECT` 原子性未在真库验证** |

迁移链现状：`0001 → 0007`，head = `0007_invoice_requests`。**下一个可用号是 `0008`**。

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

### 12.4 任务 T7：C1 团队与成员（**三个问题已答，可开工**）

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

**当前实际状态（2026-10-29）**：T1/T2/T3/T6 全部代码完成，全工作区改动**均未提交**（`HEAD` 仍是 `74fa7fc`）。剩下的两条线状态已经不同：

- **T5** 冒烟用例已预置（§12.2.1），本机因无 Docker/PG 只能 skip。**但不必等本机环境**：`.github/workflows/ci.yml` 的 `integration` job 会起真 PG 并以 `-run 'Live|Diag'` 执行，注入 `CHIRON_TEST_POSTGRES_DSN`。所以 T5 的剩余工作是**把改动推 CI 并看那个 job 的结果**，而不是在本机装数据库。5 万行导出的 `count(*)` 耗时仍需手工 `EXPLAIN ANALYZE` 补一次。
- **T7** 三个产品问题**已全部答复**（§12.4），不再是阻塞项。它现在纯粹排在 T5 之后，理由从「语义未定」变成「每加一条 SQL 都在扩大待验池」。

**因此「在 T5 之前不开新功能」这条约束依然成立**，但性质变了：不是「无法开工」，而是**主动排队**。一旦 CI 的 `Real-stack integration` 把 6 处缺口跑绿，C1 就可以按 §12.4 的五步直接开工。

**不要做的事**：不要在 T5 之前再加新的 SQL 功能；C1 开工时不要把 `tenant_members` 做成「再加一个 role 字符串字段」—— 产品已定复用 `ent_roles`，绕过它会立刻出现两套角色真相（详见 §12.4 的「强制成本」）。

## 13. P2-2 交付后的复核（2026-10-29 核实）

### 13.1 已交付（代码完成，未提交）

| 步骤 | 落点 | 验证 |
|---|---|---|
| ① 权益表 + 迁移 | `migrations/versions/0009_billing_packages.py`：`billing_packages`（目录）+ `user_entitlements`（已售权益快照）+ `payments.package_code` | 真库 `alembic upgrade head` 成功，`current` = `0009_billing_packages (head)`；表/索引/CHECK 逐项核对通过 |
| ② 一次性发放 | `internal/billing/entitlements.go` + `payments.go` 在 `ConfirmPayment` 里 `AddCredits` 成功后发放权益 | 真库用例 PASS：credits 精确到位、权益快照正确、`payment_id` 重复确认不重复发放、窗口移到过去后查不到但行保留 |
| ③ 接入模型范围 | `internal/api/ent_entitlement_gate.go`：与租户策略**取更严者**（交集） | 12 条表驱动子用例 PASS |
| ④ 过期与提示 | `GET /v1/billing/entitlements` 返回**含已过期**条目；三语 21 个 key；`BillingView.vue` 新增「套餐包」tab | `vue-tsc` 通过、`check-i18n-keys` 缺键 0、vitest 97 文件 / 767 用例全绿 |
| ⑤ 权益真正生效（P2-2b） | `internal/api/ent_entitlement_gate.go`（`EffectiveModelSet` / `GuardChatModelAccess`）+ `/v1/models` 过滤 + `proxyOpt.guardBody` 钩子 + 前端三语与「去购买套餐」直达 | `go build/vet ./...` 通过、`internal/api` 3.0s / `internal/billing` 1.0s 全绿、vitest 97 文件 / 770 用例、`vue-tsc` 与 i18n 键集通过 |

设计口径（沿用 §6 决策）：有效期从确认时刻起算、不引入 `billing_cycle`、无周期任务、过期只由时间判定；权益挂 `user_id`（用户级）而非租户级，避免一个成员的套餐影响整租户；套餐目录与已售权益**分两张表**，套餐改价不影响已售出权益。

### 13.2 已修：权益判定曾挂在没有调用方的函数上（P2-2b 补上）

**本条记录的是修复前的状态**，保留原文以便追溯为什么新增了 `ent_entitlement_gate.go`。

**修复结果（P2-2b，2026-10-29）**：不再复用 `EnforceEnterprisePolicy`，而是新增一条专供请求路径使用的判定链，原因是它的语义不适用于过滤：

- `EffectiveModelSet(ctx, tenantID, userID) (map[string]struct{}, bool)` —— 第二个返回值 `restricted` 区分「不限制」与「限制为空」。**这个区分是必须的**：`ResolveAllowedModels` 在「不限制」时返回 `admin_model_configs.model_id` 全量清单，而真库实测该表 0 行、`llm_models` 40 行、两者交集 0 —— 直接拿它过滤 `/v1/models` 会得到**一个模型都不返回**的空下拉，用户却以为权限正常。
- `GET /v1/models` 在 `ListModelsForUser` 末尾按 `restricted` 过滤；解析失败 fail-open + `slog.Warn`。
- 对话入口：`proxyOpt` 新增 `guardBody` 钩子，`POST/PUT` 在 `mutateBody` **之后**调用 `GuardChatModelAccess`。顺序很关键——`InjectSessionRuntime` 才会把最终生效 model 写回 body，若排在它之前只看得到前端随手传的字段。
- 越界返回 `403 + code=model_not_allowed + params{model, reason}`，`reason` 分 `entitlement` / `policy`；报文显式 `retryable=false`，避免给出「再点一次」的无效重试。
- 前端三语补齐，`reason=entitlement` 时给「去购买套餐」按钮并直达 `/billing?tab=packages`；`reason=policy` 只给文字指引（用户侧没有可跳转的管理员入口，不做点了不动的按钮）。

**遗留的一点口径重叠**：`EnforceEnterprisePolicy` / `ResolveAllowedModels` 现在仍无外部调用方（只有定义、包级转发与测试）。它们与新的 `EffectiveModelSet` 是两套并行判定，**建议后续二选一并删除另一套**，否则以后有人接到它们上会得到与线上不一致的行为。

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

### 13.3 任务 P2-2c：套餐目录后台 CRUD

现状：`billing_packages` **只有表**——无 handler、无种子数据、无管理界面。目前必须手工 `INSERT` 才能卖套餐。

1. 管理端 CRUD（建议权限 `billing:manage`），下架用 `enabled=false` 而不是物理删除。
2. 至少一条 seed（`duration_days > 0`、`price_cents >= 0` 受 CHECK 约束，`credits` 可 0 表示纯权益包）。
3. `code` 有 `ux_billing_packages_code` 唯一索引，重复创建返回 409 而不是 500。

**验收**：后台可增/改/上下架；下架后用户侧 `GET /v1/billing/packages` 不再返回；重复 code 返回 409。

### 13.4 小修补（已核实的确定缺口）

- `paymentResponse`（`billing_handler.go:798`）不回显 `package_code`，订单详情看不出买的是哪个套餐。一行修复。
- `frontend-vue/src/components.d.ts` 有本会话 `vue-tsc -b` 自动生成的两行改动（`OnboardingGuide` 注册），属生成物，提交时应一并带上而不是丢弃。

### 13.5 建议顺序（P2-2b 交付后重排）

| 序 | 任务 | 为什么排在这 | 前置 |
|---|---|---|---|
| 1 | **P2-2c 套餐目录后台 CRUD + seed** | P2-2b 让权益真正生效了，但**线上此刻仍然一个套餐都卖不了**：`billing_packages` 只有表，无 handler、无 seed、无界面，必须手工 `INSERT`。功能链路已通而入口缺失，这是当前最刺眼的一段 | 无 |
| 2 | 小修补：`paymentResponse` 回显 `package_code` | 用户买了套餐，订单详情却看不出买的是哪个；一行改动。与 1 同批做最省事 | 无 |
| 3 | 收敛遗留的**双判定路径** | `EnforceEnterprisePolicy` / `ResolveAllowedModels` 与新的 `EffectiveModelSet` 并存且口径不同（见 §13.2 末）。留着就会有人接错 | 无 |
| 4 | **T5 真库冒烟** | 新增了两条 SQL 路径（`billing_packages` / `user_entitlements`）与一条 403 路径，待验池扩大；按 §12.6 走 CI | 1 |
| 5 | **P2-3 通知中心** | 支付成功/权益到期预警天然接这里。注意：到期**预警**需要定时扫描，与「一次性套餐无周期任务」不冲突——扫描只发通知、不改状态 | 1 |

补一条排序理由：为什么不先做 P2-3？通知中心会用到「某笔支付成功了」和「某个权益要到期了」两类事件，而这两类事件的**源头当前还只有手工插入的套餐**。先把 1 做完，事件源才是真实的，否则通知中心只能拿假数据测。

## 14. P2-3 通知中心的前置设计（未开工，先定形状）

写在这里是为了避免 P2-3 开工时临时拍脑袋定表结构——`tenant_id` 类型、去重键、渠道开关三者一旦定错，改起来都要动数据。

**已核实的地基**（本会话读过）：
- 已有 SMTP 发信能力（`internal/auth/mail.go`），无需引入新依赖。
- 已有站内消息的读模型先例（notice 行），但那是**对话内**的，不能直接当通知中心用。
- 权益到期时间可从 `user_entitlements.expires_at` 直接取，不需要新表存"何时到期"。

**待定（开工前必须拍板）**：
1. **去重键的形状**：按事件类型 + 业务主键（如 `payment_succeeded:<payment_id>`、`entitlement_expiring:<entitlement_id>:<days_before>`）。用唯一索引保证幂等，撞键即跳过而不是先查后写（先查后写在并发下会重复推送）。
2. **渠道开关的粒度**：用户级（每个用户自己关）还是租户级（管理员统一关）？两者语义不同，混在一起会让"我关了邮件但还在收"这类投诉无从排查。
3. **到期预警的扫描频率与提前量**：建议每日一次、提前 3 天与 1 天各一次（两个去重键）。频率过高会变成骚扰。
4. **`tenant_id` 类型**：`tenants.id` 是 `smallserial`（不是 `smallint` 别名之外的推论，基线 `0001:735` 已核实），通知表若带 `tenant_id` 必须用同类型，否则外键建不上。

**验收草案**：同一支付回调重复到达只产生一条通知；用户可标记已读；关掉邮件后仍能在站内看到；到期前 3 天与 1 天各收到一次且不重复。
