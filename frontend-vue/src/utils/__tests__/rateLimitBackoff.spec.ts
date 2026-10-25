import { describe, expect, it } from 'vitest'
import {
  BACKOFF_CAP_MS,
  BACKOFF_JITTER,
  backoffDelay,
  canAutoRetry,
  isRateLimitStatus,
  parseRetryAfter,
  resolveWaitMs,
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
