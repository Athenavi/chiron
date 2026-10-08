# 邮件通道（SMTP / 晴辰云邮 HTTP API）

> **溯源（2026-10-08）**：本文件**原先缺失** —— 仓库里 4 个文件引用它，其中 `internal/auth/mail.go`
> 一处就有 8 处带**小节号**的引用（"第八节：网络超时"、"第九节：指数退避，最大间隔 60 秒"、
> "要求 5：附件自动转 Base64"、"/api/v1 路径前缀"）。现按**代码与注释**重建，**只写代码能证明的内容**；
> 与代码冲突时以代码为准。**本节编号沿用代码注释里的引用**，方便逐条对照。
> 门禁：`python scripts/check_doc_links.py` 拦住"引用了不存在的文档"这类断链。

## 1. 两种通道

| 通道 | 常量 | 说明 |
|---|---|---|
| 通用 SMTP | `smtp` | `none` / `starttls` / `ssl` 三种安全模式（见 §2） |
| 晴辰云邮 | `qingchen` | **HTTP API**（§3–§9） |

`MailKnownProviders()` / `IsKnownMailProvider()` / `IsKnownMailSecurity()` 是配置校验的入口（通道与安全模式的取值以它们为准）。

## 2. 投递配置（`MailConfig`）

| 字段 | 适用 | 说明 |
|---|---|---|
| `provider` | 全部 | `smtp` / `qingchen` |
| `host` · `port` · `username` · `password` · `security` · `insecure_skip_verify` | SMTP | `security` = `none` / `starttls`（通常 587）/ `ssl`（通常 465） |
| `base_url` · `api_key` | 晴辰云邮 | HTTP 端点与令牌（§3） |
| `from` · `from_name` · `reply_to` | 全部 | 发件地址/显示名/回复地址（`MailMessage.From` 为空时用配置值） |
| `timeout_seconds` | 晴辰云邮 | 见 §8 |

密钥在库里是**密文**（`encryptSecret` / `decryptSecret`），构造发送器前解密。

## 3. 晴辰云邮：端点与鉴权

```
POST {base_url}/send
Authorization: Bearer {api_key}
Content-Type: application/json
Accept: application/json
```

`base_url` **应当带上路径前缀 `/api/v1`**：服务端 404 几乎总是"服务地址少了路径前缀"造成的。

## 4. 请求体

| 字段 | 说明 |
|---|---|
| `to` | 收件人，**多个以逗号连接**（`strings.Join(msg.To, ",")`） |
| `subject` · `body` | `body` 放的是 **HTML** 正文 |
| `from` | `MailMessage.From` 优先，空则用配置的默认发件人 |
| `template_id` · `variables` · `channel_id` | 模板通道（§7） |
| `attachments` | `[{filename, content?, url?, content_type?}]`（§6） |

本地先做三项校验（不满足直接 `ErrMailConfigInvalid`，不发请求）：`base_url` 非空 · `api_key` 非空 ·
**主题 / 正文 / `template_id` 不能同时为空**。

## 5. 响应与错误

| 状态码 | 含义 | 处理 |
|---|---|---|
| **202** | 已入队（异步发送） | ✅ 成功，返回 `nil` |
| **429** | 请求频率超限 | 退避重试（§9）；重试耗尽 ⇒ `ErrMailSendFailed` |
| 其它 | 失败 | `ErrMailSendFailed`，附 `{"error": "..."}` 文本 |

错误描述提取顺序：响应体 `{"error": "..."}` → 否则取响应体前 **200** 字符 → 否则 `empty response`。
响应体读取上限 **64KB**（错误响应不应拖垮网关）；网络层失败归为 `ErrMailUnreachable`。
三类错误分开是为了让业务层回不同的状态码/文案。

## 6. 附件要求

按序（编号与代码注释里的"要求 N"一致）：

1. 附件对象字段：`filename` · `content_type` · **`content` 或 `url` 二选一**；
2. `content` 是 **Base64 内联**内容；
3. `url` 用于服务端自行抓取的场景；
4. 单个附件上限 **10 MiB**（`MaxMailAttachmentBytes`，与服务商限制保持一致，**本地先拦一次**，
   避免把 10MB+ 的请求白发出去）；
5. **本地文件（`MailAttachment.FilePath`）在发送前自动读盘并转 Base64**。

> ⚠ **安全约束**：`FilePath` 只能来自服务端自身的配置/代码，**绝不可由请求参数拼出** ——
> 否则等于开放任意文件读取（代码注释里明确写了这一条）。

## 7. 模板通道

`template_id > 0` 时由**服务端模板**决定内容，`subject` / `body` 可省略；`variables` 用于替换模板里的
`{{.key}}` 占位符；`channel_id` 用于选择服务商的发送渠道。

Chiron 侧自己的邮件模板另有一套：`RenderMailTemplate` / `ValidateMailTemplate`（`internal/auth/mail_template.go`）
负责渲染与校验，验证码由 `GenerateMailCode` 生成。

## 8. 超时

单次请求超时 = `timeout_seconds`；**未配置（≤0）时默认 30 秒**（`mailHTTPDefaultTTL`，
即代码注释所说的"文档建议的网络超时"）。超时按请求上下文施加，重试等待期间同样可被上下文取消打断。

## 9. 重试与退避（仅针对 429）

- 最多重试 **3** 次（`mailHTTPMaxRetries`）；
- 等待时长：服务端给了 `Retry-After`（**秒数或 HTTP 日期**）就**以它为准**，否则按**指数退避**
  `1s << attempt`；
- **上限 60 秒**（`mailHTTPMaxBackoff`）——`Retry-After` 同样受该上限约束；
- 等待期间上下文取消 ⇒ 立即返回 `ctx.Err()`。

## 10. Chiron 侧的邮件用途与端点

公开（限流，无需登录）：

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/v1/auth/email/status` | 是否开启邮箱验证（注册流程据此决定要不要验证码） |
| `POST` | `/v1/auth/email/code` | 发送邮箱验证码 |
| `POST` | `/v1/auth/email/login` | 邮箱验证码登录 |
| `POST` | `/v1/auth/password/reset/request` · `POST /v1/auth/password/reset/confirm` | 密码重置 |

管理端（需 `sso:manage` 权限）：

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` / `PUT` | `/v1/ent/mail/config` | 读取 / 更新投递配置（密钥加密存储） |
| `POST` | `/v1/ent/mail/test` | 发送测试邮件 |

另有注册欢迎信（`SendWelcomeAsync`，异步发送）。

## 11. 实现落点

| 环节 | 位置 |
|---|---|
| 通道类型 / 配置 / 发送 | `internal/auth/mail.go`（`HTTPMailSender`、`sendSMTP`、`sendQingchen`、`postQingchen`、`backoffDelay`、`buildQingchenAttachments`） |
| MIME 组装 | 同文件（`buildMIMEMessage` / `writeAlternativeBody` / `writeAttachmentPart`） |
| 模板与验证码 | `internal/auth/mail_template.go`（`RenderMailTemplate` / `ValidateMailTemplate` / `GenerateMailCode`） |
| HTTP 端点与配置读写 | `internal/api/mail_handler.go`（`RegisterPublicRoutes` / `RegisterAdminRoutes` / `sendMail` / `senderConfig`） |
| 前端配置界面 | `frontend-vue/src/views/admin/MailView.vue` |
