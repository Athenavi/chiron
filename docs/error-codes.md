# 错误码契约

面向 **Go 网关实现者、Python 引擎与第三方调用方**。前端的消费端约定（渲染优先级）见
[frontend i18n 约定](../frontend-vue/src/i18n/README.md#后端错误文案)，两侧共用的错误码常量以本文为准。

> 本文所有断言都能由 `internal/api/error_codes.go`、`internal/api/response.go` 与
> `frontend-vue/src/utils/apiError.ts` 的当前代码核对；引用格式为 `文件:行`。

## 为什么有稳定错误码

`APIResponse.Error` 是给集成方看的英文/中文原文（`internal/api/response.go:9-15` 的统一常量
即由此而来），它**不随界面语言变化**，也不适合直接呈现给终端用户。因此失败响应额外携带一个
**稳定 `code`**（与可选的结构化 `params`），由客户端按当前语言渲染文案 ——
前端落在 `frontend-vue/src/locales/<lang>/errors.ts`。

兼容性约束：`error` 字段**保持原有语义不变**（旧客户端继续工作），`code` / `params` 为新增字段。
内部日志（`slog`）不受本机制影响，仍记录原文，便于排障与检索。

## 响应形状

```jsonc
{
  "success": false,
  "error": "tenant token quota exceeded",   // 兼容兜底：面向集成方的原文
  "code": "quota_exceeded",                 // 稳定契约：客户端据此选文案
  "params": { "limit": 20 }                 // 可选：供文案插值（如 errors.http_status 的 {status}）
}
```

字段定义见 `internal/api/response.go:17-27`（`APIResponse`）。成功响应不带 `code` / `params`。

## `code` 从哪里来

有且仅有两条路径：

### 1. 显式指定（新代码推荐）

```go
api.JSONWithCode(w, http.StatusTooManyRequests, api.CodeQuotaExceeded,
    map[string]interface{}{"limit": 20}, "tenant token quota exceeded")
```

`JSONWithCode` 见 `internal/api/error_codes.go:97-99`。注意它**当前没有任何调用点** ——
线上响应里的 `code` 全部走路径 2。

### 2. 按文案自动推断（既有调用点的默认行为）

`JSON()` 在「`success=false` 且 `code` 为空」时调用 `codeForMessage(error)` 补码
（`internal/api/response.go:35-45`）。推断分三步，见 `internal/api/error_codes.go:79-93`：

| 步骤 | 规则 | 位置 |
|---|---|---|
| a | 空文案 → 返回空串（**不写** `code` 字段） | `error_codes.go:80-82` |
| b | `knownMessageCodes` 精确匹配（整串小写后比对） | `error_codes.go:37-48` |
| c | `messageCodeRules` 关键字**子串包含**匹配，**按序先到先得** | `error_codes.go:52-75` |
| d | 都没命中 → `request_failed` | `error_codes.go:92` |

**规则顺序是契约的一部分**：`messageCodeRules` 必须从"更具体"排到"更宽泛"
（`internal/api/error_codes.go:51` 的注释即此意）。例如 `"insufficient credit"` 必须排在
`"credit"` 之前，否则前者永远被后者截胡 —— 二者当前恰好同码，但语义更细的关键字仍应先写。

因此**只要 `error` 非空，`code` 必然非空**（最差是 `request_failed`）。这一点决定了前端的
兜底分支极少生效，见下文「前端如何消费」。

## 错误码表

`Code*` 常量定义于 `internal/api/error_codes.go:22-33`。

| `code` | 常量 | 触发来源（`error` 文案） | 观测到的 HTTP 状态 |
|---|---|---|---|
| `auth_required` | `CodeAuthRequired` | `ErrAuthRequired` = `authentication required` | 401（`auth.go:480`、`billing_handler.go:98`） |
| `forbidden` | `CodeForbidden` | `insufficient permissions`、`insufficient enterprise permissions` | 403（`middleware.go:463`、`ent_costcenter_handler.go:537`） |
| `invalid_request` | `CodeInvalidRequest` | `ErrInvalidReq` = `invalid request body`；或文案含 `invalid` / `required` | 400（`response.go:63` 的 `BadRequest`） |
| `not_found` | `CodeNotFound` | `ErrNotFound` = `resource not found`；或文案含 `not found` | 404（`response.go:67`） |
| `rate_limited` | `CodeRateLimited` | `rate limit exceeded`（`response.go:100`）；或文案含 `rate limit` / `too many request` | 429（`response.go:99-101`） |
| `quota_exceeded` | `CodeQuotaExceeded` | `tenant token quota exceeded`、`tenant concurrency quota exhausted`；或文案含 `quota` | 429（`gateway_router.go:660`） |
| `insufficient_credits` | `CodeInsufficientCredits` | `insufficient credits — please recharge in Billing`；或文案含 `credit` | 402（`gateway_router.go:640`） |
| `service_unavailable` | `CodeServiceUnavailable` | `ErrDBUnavailable` = `service temporarily unavailable`；或文案含 `unavailable` / `redis down` / `timeout` | **500 或 503**（见下节） |
| `internal_error` | `CodeInternal` | 文案含 `internal` | 500（经 `InternalError`，`response.go:71-73`） |
| `request_failed` | `CodeRequestFailed` | 兜底：非空但未命中任何规则的文案 | 任意（随调用点） |

> `knownMessageCodes` 另含 `task already running for this session` → `invalid_request`
> （`error_codes.go:47`），用于同会话重复提交。

## `code` 与 HTTP 状态是两条独立的轴

`code` 由**文案**推断，HTTP 状态由**调用点**决定，二者不强制一致，也不在同一个地方维护。
最直观的证据是 `service_unavailable`：

| 调用点 | 状态 | `code` |
|---|---|---|
| `internal/api/auth.go:292`（`logAndRespond(..., 500, ErrDBUnavailable)`） | 500 | `service_unavailable` |
| `internal/api/ent_policy_handler.go:347`（`ServiceUnavailable(w, ErrDBUnavailable)`） | 503 | `service_unavailable` |

两条路径的 `error` 文案相同、`code` 相同，状态却不同。**调用方不要假定 `code` 与状态一一对应**：
判分支请用 `code`（语义稳定），决定重试策略再用状态码。

同一状态也可能对应多个 `code`：任何 400 的 `error` 文案若含 `not found`，得到的 `code` 会是
`not_found`。若需要精确语义，请用 `JSONWithCode` 显式指定。

## 前端如何消费

优先级实现在 `frontend-vue/src/utils/apiError.ts:59-85`（`describeApiError`）：

1. **后端 `code` 的本地化文案**（`errors.<code>`，见 `locales/<lang>/errors.ts`）；
2. **已知状态码的文案**（`statusMessage`，映射见 `apiError.ts:25-41`）；
3. 后端 `error` / `message` 原文；
4. 调用方兜底文案。

5xx 是例外：仍把后端原文附在括号里（`apiError.ts:70-74`），因为那是定位服务端问题的唯一线索。

两处需要留意的事实：

- 因为「`error` 非空 ⇒ `code` 非空」，**第 2 步（状态码兜底）实际上只在响应不带 `error` 文案
  时才生效**；服务端已给出 `code` 时，状态码映射不会参与。映射表因此主要服务于
  「无 `code` 的老接口 / 网关前置组件（如 nginx）直接生成的响应」。
- 状态码映射把 `500/502/503/504` 统一指向 `service_unavailable`（`apiError.ts:35-38`），
  比服务端更粗 —— 服务端有 `code` 时以 `code` 为准。

## `errors.ts` 的键集合

三个语言各一份，键必须与后端 `Code*` **一一对应**，且**三语言键集完全相同**：

| 类别 | 键 | 产生方 |
|---|---|---|
| 后端稳定码 | `auth_required` `forbidden` `invalid_request` `not_found` `rate_limited` `quota_exceeded` `insufficient_credits` `service_unavailable` `internal_error` `request_failed` | `internal/api/error_codes.go:22-33` |
| 前端专用（后端不产生） | `timeout` `network_error` `payload_too_large` `http_status` | 客户端网络层，`apiError.ts:32-33,79,82-83` |

`http_status` 是**唯一带参数**的键，用命名插值：`t('errors.http_status', { status })`
（`apiError.ts:79`）。

## 新增 / 修改错误码的清单

1. **Go 侧**：在 `internal/api/error_codes.go:22-33` 增加 `Code*` 常量；
   若该码应对既有高频文案自动生效，同时补进 `knownMessageCodes`（精确）或
   `messageCodeRules`（关键字，**注意插到合适的位置**，更具体的前置）；
2. **三语言同步**（硬要求）：`frontend-vue/src/locales/{zh-CN,en-US,ar}/errors.ts` 各补一条，
   键完全相同；缺键时客户端只能回退 `error` 原文 —— 这条同步要求同时写在
   `internal/api/error_codes.go:21` 与 `frontend-vue/src/i18n/README.md`；
3. **不要改已有码的键名**：它是与客户端约定的契约（`frontend-vue/src/locales/zh-CN/errors.ts`
   文件头注释）。
4. `zh-CN` 是源语言：先写 `zh-CN`，再补 `en-US` / `ar`。

## 验证

契约已由 `internal/api/error_codes_test.go` 钉成回归门禁 —— 覆盖精确匹配、大小写不敏感、
关键字规则、**规则顺序**、空文案不写 `code`、`JSON()` 自动补码、显式 `code` 不被覆盖、
成功响应不带 `code`：

```bash
# Go 侧：错误码推断与自动补码
go test -mod=mod ./internal/api/ -run "TestCodeForMessage|TestJSON_" -count=1

# 前端：消费优先级与状态码映射
cd frontend-vue && pnpm exec vitest run src/utils/__tests__/apiError.spec.ts
```

> **仍未覆盖**：三语言 `errors.ts` 与 `Code*` 常量的**键集一致性**目前靠人工核对（见上节的同步清单）。
> 自动化比对随 `ar` / `en-US` 译文补齐一并落地。
