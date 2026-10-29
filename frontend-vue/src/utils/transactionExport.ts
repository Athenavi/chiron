/**
 * 交易明细 CSV 导出的前端工具（P1-1 / roadmap §10.2）。
 *
 * 为什么单独抽一个文件：导出这条链路有三个容易做错、且都**不依赖网络**的环节——
 * 时间范围怎么算、文件名怎么生成、失败时怎么把 Blob 里的错误读出来。
 * 把它们做成纯函数，就能在 jsdom 里锁住行为，而视图只负责点一下。
 */

/** 导出的 CSV 内容类型（后端固定返回这个，前端拼 Blob 时保持一致）。 */
export const CSV_MIME = 'text/csv;charset=utf-8'

/**
 * 按"最近 N 天"算出导出用的 RFC3339 区间。
 *
 * 用 `subtract(days, 'day')` 而不是毫秒运算：跨夏令时时后者会偏一小时，
 * 而用户对账时看到"最早的记录差一小时"只会以为数据错了。
 *
 * 两端的时区都归一成 UTC 再序列化：后端的 `credit_transactions.created_at`
 * 是无时区的 `timestamp`，按 UTC 解释（见 roadmap §10.2 与 D 组 L4-3）。
 */
export function exportRangeFor(days: number, now: Date = new Date()): { from: string; to: string } {
  const to = new Date(now.getTime())
  const from = new Date(now.getTime())
  from.setDate(from.getDate() - days)
  return { from: from.toISOString(), to: to.toISOString() }
}

/**
 * 拼接导出请求的路径。
 *
 * 文件名不依赖后端的 `Content-Disposition`：跨域场景下浏览器默认只暴露
 * 少数几个简单响应头，读不到那个头（桌面端是 file:// 跨站），
 * 与其做两套分支，不如前端自己按同一个规则生成 —— 两边格式一致即可。
 */
export function exportPath(days: number, now: Date = new Date()): string {
  const { from, to } = exportRangeFor(days, now)
  return `/v1/billing/transactions.csv?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`
}

/** 把 Date 压成文件名里能用的紧凑 UTC 串：`20261029T1200`。 */
function compactUtc(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}T${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}`
}

/**
 * 生成下载文件名。
 *
 * 纯 ASCII、不含冒号：冒号在 Windows 文件系统上非法，用户下载后改名会很烦。
 * 与后端 `exportDisposition` 的规则一致，只是前端用 `_` 分隔便于阅读。
 */
export function exportFileName(days: number, now: Date = new Date()): string {
  const from = new Date(now.getTime())
  from.setDate(from.getDate() - days)
  return `transactions_${compactUtc(from)}_${compactUtc(now)}.csv`
}

/**
 * 从 Blob 解析出的错误体。
 *
 * `code`/`params` 是后端 `JSONWithCode` 给出的结构化信息（i18n 用）；`message`
 * 是 `detail → error → message` 链上的原文（兼容兜底）。
 */
export interface BlobErrorPayload {
  code?: string
  params?: Record<string, unknown>
  message?: string
}

/**
 * 解析 Blob 里的错误体。
 *
 * 为什么需要：请求用 `responseType: 'blob'` 时，4xx/5xx 的响应体**也是 Blob**，
 * axios 的 `error.response.data` 拿不到 `.error` 字段，直接 `describeApiError`
 * 只会得到"请求失败"这种没有信息量的兜底文案。而导出最常见的失败恰恰是
 * "范围里记录太多，请缩小范围"—— 用户必须看到这句话才知道该怎么办。
 *
 * 解析顺序与 `serverErrorDetail` 保持一致（`detail → error → message`）。
 * Blob 读不出来、或不是 JSON 时返回 null，由调用方回退到通用错误文案。
 */
export async function blobErrorPayload(blob: Blob): Promise<BlobErrorPayload | null> {
  try {
    const text = await blob.text()
    if (!text.trim()) return null
    const parsed = JSON.parse(text) as unknown
    if (!parsed || typeof parsed !== 'object') return null

    const body = parsed as {
      code?: unknown
      params?: unknown
      detail?: unknown
      error?: unknown
      message?: unknown
    }

    const payload: BlobErrorPayload = {}
    if (typeof body.code === 'string' && body.code) payload.code = body.code
    if (body.params && typeof body.params === 'object' && !Array.isArray(body.params)) {
      payload.params = body.params as Record<string, unknown>
    }
    for (const value of [body.detail, body.error, body.message]) {
      if (typeof value === 'string' && value.trim()) {
        payload.message = value
        break
      }
    }
    // 什么都没解析出来 —— 当作不可解析，让调用方走通用兜底。
    if (!payload.code && !payload.message) return null
    return payload
  } catch {
    // 非 JSON（例如代理返回的 HTML 错误页）—— 不该把整页 HTML 塞给用户。
    return null
  }
}

/** 只要错误原文时的便捷封装（保持既有调用方与测试的签名不变）。 */
export async function blobErrorMessage(blob: Blob): Promise<string | null> {
  const payload = await blobErrorPayload(blob)
  return payload?.message ?? null
}

/**
 * 触发浏览器下载。
 *
 * a 元素要挂到 body 上再点：Firefox 对未挂载到文档的 a 不触发下载。
 * 用完立刻 revokeObjectURL：Blob 不释放会一直占着内存，导出大文件时尤其明显。
 */
export function downloadBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = fileName
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}
