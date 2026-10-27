import { describe, expect, it } from 'vitest'
import {
  BACKOFF_CAP_MS,
  BACKOFF_JITTER,
  backoffDelay,
  canAutoRetry,
  isRateLimitStatus,
  parseRetryAfter,
  parseRetryAfterParams,
  readRetryable,
  resolveResponseWaitMs,
  resolveWaitMs,
  shouldRetryResponse,
} from '../rateLimitBackoff'

/** 确定性"随机源"：0 → 下沿，1 → 上沿，0.5 → 无抖动（便于断言） */
const noJitter = () => 0.5
const minJitter = () => 0
const maxJitter = () => 1

describe('限流退避（CDS v1/06 §6.7）', () => {
  it('只把 429 / 503 当限流，其它状态码绝不走重试', () => {
    expect(isRateLimitStatus(429)).toBe(true)
    expect(isRateLimitStatus(503)).toBe(true)
    expect(isRateLimitStatus(500)).toBe(false)
    expect(isRateLimitStatus(401)).toBe(false)
    expect(isRateLimitStatus(undefined)).toBe(false)
    expect(isRateLimitStatus(null)).toBe(false)
  })

  it('延迟按 2 的幂增长，基准 5 秒', () => {
    expect(backoffDelay(1, noJitter)).toBe(5000)
    expect(backoffDelay(2, noJitter)).toBe(10000)
    expect(backoffDelay(3, noJitter)).toBe(20000)
  })

  it('抖动幅度正好是 ±15%', () => {
    expect(backoffDelay(1, minJitter)).toBe(Math.round(5000 * (1 - BACKOFF_JITTER)))
    expect(backoffDelay(1, maxJitter)).toBe(Math.round(5000 * (1 + BACKOFF_JITTER)))
  })

  it('封顶 10 分钟（否则会算出几十分钟的等待）', () => {
    expect(backoffDelay(20, noJitter)).toBe(BACKOFF_CAP_MS)
    expect(backoffDelay(99, maxJitter)).toBe(BACKOFF_CAP_MS)
  })

  it('连续 3 次之后不再自动重试', () => {
    expect(canAutoRetry(1)).toBe(true)
    expect(canAutoRetry(2)).toBe(true)
    expect(canAutoRetry(3)).toBe(false)
    expect(canAutoRetry(4)).toBe(false)
  })

  it('Retry-After 同时支持秒数与 HTTP 日期', () => {
    expect(parseRetryAfter('30')).toBe(30000)
    expect(parseRetryAfter('0')).toBe(0)
    const now = Date.parse('2026-10-24T00:00:00Z')
    expect(parseRetryAfter('Sat, 24 Oct 2026 00:00:20 GMT', now)).toBe(20000)
    // 已经过去的时间点不该算出负值
    expect(parseRetryAfter('Sat, 24 Oct 2026 00:00:00 GMT', now + 5000)).toBe(0)
  })

  it('Retry-After 解析不出来时返回 null（交给本地公式）', () => {
    expect(parseRetryAfter(undefined)).toBeNull()
    expect(parseRetryAfter(null)).toBeNull()
    expect(parseRetryAfter('')).toBeNull()
    expect(parseRetryAfter('   ')).toBeNull()
    expect(parseRetryAfter('soon')).toBeNull()
  })

  it('服务端给了 Retry-After 就优先听它的，但仍受 10 分钟封顶约束', () => {
    expect(resolveWaitMs(1, '2', noJitter)).toBe(2000)
    // 荒唐的大值不能把页面卡死
    expect(resolveWaitMs(1, '99999', noJitter)).toBe(BACKOFF_CAP_MS)
    // 没给就退回本地指数退避
    expect(resolveWaitMs(3, undefined, noJitter)).toBe(20000)
  })
})

// ── `retryable` 契约：服务端说了就听，没说才回退状态码 ─────────────────
//
// 分层：`code` 负责 i18n 与分支，`retryable` 只负责重试决策。
// 「没这个字段」与「它是 false」是两件事：前者语义是"调用方自判"。
describe('服务端显式 retryable（优先级高于状态码推断）', () => {
  it('只认真布尔，字符串 / 数字 / null 一律当"没说"', () => {
    expect(readRetryable(true)).toBe(true)
    expect(readRetryable(false)).toBe(false)
    expect(readRetryable(undefined)).toBeUndefined()
    expect(readRetryable(null)).toBeUndefined()
    expect(readRetryable('true')).toBeUndefined()
    expect(readRetryable(1)).toBeUndefined()
  })

  it('有 retryable 则按它走，不看状态码', () => {
    // 409 session_busy：旧逻辑按状态码会判"不重试"，服务端说该重试
    expect(shouldRetryResponse(409, true)).toBe(true)
    // 500 里的 provider 暂时性故障：同上
    expect(shouldRetryResponse(500, true)).toBe(true)
    // 参数非法：服务端确认重试无用，即使 429 也不重试
    expect(shouldRetryResponse(429, false)).toBe(false)
    expect(shouldRetryResponse(503, false)).toBe(false)
  })

  it('没有 retryable 则完全回退到旧的 429 / 503 判断（后向兼容）', () => {
    expect(shouldRetryResponse(429, undefined)).toBe(true)
    expect(shouldRetryResponse(503, undefined)).toBe(true)
    expect(shouldRetryResponse(409, undefined)).toBe(false)
    expect(shouldRetryResponse(500, undefined)).toBe(false)
    expect(shouldRetryResponse(undefined, undefined)).toBe(false)
  })

  it('等待时长认 body 里的 params.retry_after_seconds（会话忙会传）', () => {
    expect(parseRetryAfterParams({ params: { retry_after_seconds: 60 } })).toBe(60000)
    expect(parseRetryAfterParams({ params: { retry_after_seconds: '30' } })).toBe(30000)
    expect(parseRetryAfterParams({ params: { retry_after_seconds: 0 } })).toBeNull()
    expect(parseRetryAfterParams({ params: {} })).toBeNull()
    expect(parseRetryAfterParams({})).toBeNull()
    expect(parseRetryAfterParams(undefined)).toBeNull()
  })

  it('等待优先级：Retry-After 头 > body 参数 > 本地退避，均受封顶', () => {
    const body = { params: { retry_after_seconds: 60 } }
    expect(resolveResponseWaitMs(1, '2', body, noJitter)).toBe(2000)
    expect(resolveResponseWaitMs(1, undefined, body, noJitter)).toBe(60000)
    expect(resolveResponseWaitMs(3, undefined, undefined, noJitter)).toBe(20000)
    expect(resolveResponseWaitMs(1, undefined, body, noJitter, undefined)).toBeLessThanOrEqual(
      BACKOFF_CAP_MS,
    )
  })
})
