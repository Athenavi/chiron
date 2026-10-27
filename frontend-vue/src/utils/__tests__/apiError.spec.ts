import { describe, expect, it } from 'vitest'
import { describeApiError, statusMessage } from '../apiError'

const withStatus = (status: number, data?: Record<string, string>) => ({
  response: { status, data },
})

describe('describeApiError（错误文案翻译）', () => {
  it('402 给出可执行的充值指引，而不是 HTTP 状态码', () => {
    const text = describeApiError(withStatus(402, { error: 'insufficient credits — please recharge in Billing' }))
    expect(text).toContain('额度已用尽')
    expect(text).toContain('充值')
    expect(text).not.toContain('status code')
    expect(text).not.toContain('insufficient credits')
  })

  it('常见状态码都有中文说明', () => {
    expect(describeApiError(withStatus(401))).toContain('登录状态已失效')
    expect(describeApiError(withStatus(429))).toContain('请求过于频繁')
    expect(describeApiError(withStatus(503))).toContain('服务暂时不可用')
  })

  it('未知状态码优先展示后端文案（便于定位）', () => {
    expect(describeApiError(withStatus(418, { error: 'teapot is busy' }))).toBe('teapot is busy')
    expect(describeApiError(withStatus(418, { message: '另一个字段' }))).toBe('另一个字段')
  })

  it('未知状态码且无后端文案时退化为带状态码的提示', () => {
    expect(describeApiError(withStatus(418))).toBe('请求失败（HTTP 418）')
  })

  it('网络错误与超时给出各自的说明', () => {
    expect(describeApiError({ message: 'Network Error' })).toContain('网络连接失败')
    expect(describeApiError({ code: 'ECONNABORTED', message: 'timeout of 30000ms exceeded' })).toContain('请求超时')
  })

  it('裸 axios 文案不会原样透出（宁可给兜底文案）', () => {
    expect(describeApiError({ message: 'Request failed with status code NaN' })).toBe('请求失败，请稍后重试')
  })

  it('无法识别时回退到调用方给的兜底文案', () => {
    expect(describeApiError({}, '发送失败')).toBe('发送失败')
    expect(describeApiError(null, '发送失败')).toBe('发送失败')
  })

  it('statusMessage 对未收录状态码返回 null', () => {
    expect(statusMessage(402)).not.toBeNull()
    expect(statusMessage(599)).toBeNull()
    expect(statusMessage(undefined)).toBeNull()
  })
})

describe('describeApiError（服务端错误要能定位）', () => {
  const withStatus = (status: number, data?: Record<string, string>) => ({ response: { status, data } })

  it('5xx 附带后端给出的具体原因，而不是只留一句"服务器错误"', () => {
    expect(describeApiError(withStatus(500, { error: 'failed to create payment order' })))
      .toBe('服务暂时不可用，请稍后重试（failed to create payment order）')
    expect(describeApiError(withStatus(500, { error: '支付下单失败' })))
      .toBe('服务暂时不可用，请稍后重试（支付下单失败）')
  })

  it('5xx 没有后端文案时只给通用说明', () => {
    expect(describeApiError(withStatus(500))).toBe('服务暂时不可用，请稍后重试')
    expect(describeApiError(withStatus(502))).toContain('服务暂时不可用')
    expect(describeApiError(withStatus(503))).toContain('服务暂时不可用')
  })

  it('本地后端（FastAPI）把原因放在 detail 里，也必须带出', () => {
    // desktop-local 是 FastAPI：503 的原因在 `detail`，不是 Go 网关的 `error`。
    // 少读一个字段，本地模式下就只剩没有信息量的兜底文案。
    expect(describeApiError(withStatus(503, { detail: '本地 PostgreSQL 未启动（见 /readyz 的 pg 字段）' })))
      .toContain('本地 PostgreSQL 未启动')
  })

  it('取值链与 serverErrorDetail 一致：detail 优先于 error 与 message', () => {
    expect(describeApiError(withStatus(500, { detail: '来自 detail', error: '来自 error', message: '来自 message' })))
      .toContain('来自 detail')
  })

  it('客户端错误（4xx）不把后端英文原文附在中文说明后，保持可读', () => {
    const text = describeApiError(withStatus(402, { error: 'insufficient credits — please recharge in Billing' }))
    expect(text).toContain('额度已用尽')
    expect(text).not.toContain('insufficient credits')
  })
})
