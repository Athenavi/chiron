/**
 * 429 / 503 限流退避（CDS v1/06 §6.7 的 `useRateLimitBackoff`）。
 *
 * 官方公式：`min(max(base, 5000) * 2^(n-1) * (1 ± J), 600000)`，J = 15% 全抖动，
 * 连续 3 次之后不再自动重试、必须交人工 —— 无限自动重试会把账号打进更深的限流。
 *
 * 为什么要有抖动：多个标签页 / 多个客户端同时被限流时，固定延迟会让它们在
 * 同一毫秒一起回来，形成二次冲击。
 *
 * 纯函数放在这里（便于单测），接进 axios 的那一层见 api/index.ts。
 */

/** 基准延迟；低于 5s 的退避在真实的速率窗口面前没有意义。 */
export const BACKOFF_BASE_MS = 5000
/** 封顶 10 分钟：再长就不如让用户自己决定什么时候重试。 */
export const BACKOFF_CAP_MS = 600_000
/** 自动重试上限，超过即交人工。 */
export const BACKOFF_MAX_RETRIES = 3
/** ±15% 全抖动 */
export const BACKOFF_JITTER = 0.15

/** 只有这两个状态码算"限流/暂时不可用"，其它错误走原有处理，绝不能盲目重试。 */
export function isRateLimitStatus(status?: number | null): boolean {
  return status === 429 || status === 503
}

/**
 * 服务端显式说的"能不能再发一次"。只有**真布尔**才算结论 ——
 * 字符串 `'true'`、数字 `1`、`null` 一律视为"没说"，转交给状态码回退逻辑去判，不猜。
 */
export function readRetryable(value: unknown): boolean | undefined {
  return typeof value === 'boolean' ? value : undefined
}

/**
 * 重试决策：**服务端契约字段优先，状态码回退**（后向兼容）。
 *
 * 分层依据：`code` 负责"发生了什么"（i18n 与分支），`retryable` 负责"要不要再发一次"。
 * 此前只有状态码可看，于是 409 `session_busy`（等上一轮跑完就能过，重试是**对的策略**）
 * 被当成不可重试而直接甩给用户；反过来 500 里的 provider 暂时性故障又被当成不可重试。
 * 现在服务端说了就听它的；它没说（字段缺失 = 调用方自判）才用旧的 429/503 判断。
 */
export function shouldRetryResponse(status?: number | null, retryable?: boolean): boolean {
  if (retryable !== undefined) return retryable
  return isRateLimitStatus(status)
}

/**
 * 第 `attempt` 次重试前该等多久（attempt 从 1 开始）。
 *
 * @param attempt 已重试次数（1 = 第一次重试）
 * @param random 注入随机源，便于测试断言确定性
 */
export function backoffDelay(attempt: number, random: () => number = Math.random): number {
  const n = Math.max(1, Math.floor(attempt))
  const raw = Math.max(BACKOFF_BASE_MS, BACKOFF_BASE_MS) * Math.pow(2, n - 1)
  const jitter = 1 + (random() * 2 - 1) * BACKOFF_JITTER
  return Math.min(Math.round(raw * jitter), BACKOFF_CAP_MS)
}

/** 还允许自动重试吗？用 `attempt` 表示"已经重试过几次"。 */
export function canAutoRetry(attempt: number): boolean {
  return attempt < BACKOFF_MAX_RETRIES
}

/**
 * 解析服务端给的 `Retry-After`。**优先于本地退避计算** ——
 * 服务端知道速率窗口什么时候开，本地只能猜。
 *
 * 支持两种格式：秒数（`Retry-After: 30`）与 HTTP 日期（`Retry-After: Wed, 21 Oct 2026 07:28:00 GMT`）。
 * 解析不出来或不合法时返回 null，交给本地公式。
 */
export function parseRetryAfter(header: unknown, now: number = Date.now()): number | null {
  if (header == null) return null
  const raw = String(header).trim()
  if (!raw) return null

  // 纯数字 = 秒
  if (/^\d+$/.test(raw)) {
    const seconds = Number(raw)
    return Number.isFinite(seconds) ? Math.max(0, seconds * 1000) : null
  }

  // HTTP 日期
  const at = Date.parse(raw)
  if (Number.isNaN(at)) return null
  return Math.max(0, at - now)
}

/**
 * 最终该等多久：Retry-After 优先，否则本地指数退避；
 * 两者都受 10 分钟封顶约束（避免服务端给出荒唐的大值把页面卡死）。
 */
export function resolveWaitMs(
  attempt: number,
  retryAfterHeader?: unknown,
  random: () => number = Math.random,
  now: number = Date.now(),
): number {
  const fromServer = parseRetryAfter(retryAfterHeader, now)
  const wait = fromServer ?? backoffDelay(attempt, random)
  return Math.min(Math.max(0, wait), BACKOFF_CAP_MS)
}

/**
 * 从错误体里取服务端建议的等待时长（秒）：`params.retry_after_seconds`。
 *
 * 这是错误码 + 结构参数的既有用法（`session_busy` / `session_recover` 都已经在传），
 * 与 `Retry-After` 头同义但走 body —— 有些代理会吃掉响应头，body 不会被改。
 */
export function parseRetryAfterParams(body: unknown): number | null {
  const raw = (body as { params?: { retry_after_seconds?: unknown } } | undefined)?.params
    ?.retry_after_seconds
  const seconds = typeof raw === 'string' ? Number(raw) : raw
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return null
  return seconds > 0 ? seconds * 1000 : null
}

/**
 * 重试前实际等待：`Retry-After` 头 > body 的 `params.retry_after_seconds` > 本地退避，
 * 都受 10 分钟封顶。两道服务端口径共存是因为历史路径只写其一（限流中间件写头，
 * `session_busy` 写参数），客户端该两处都认。
 */
export function resolveResponseWaitMs(
  attempt: number,
  retryAfterHeader: unknown,
  body: unknown,
  random: () => number = Math.random,
  now: number = Date.now(),
): number {
  const fromHeader = parseRetryAfter(retryAfterHeader, now)
  const fromBody = fromHeader == null ? parseRetryAfterParams(body) : null
  const wait = fromHeader ?? fromBody ?? backoffDelay(attempt, random)
  return Math.min(Math.max(0, wait), BACKOFF_CAP_MS)
}
