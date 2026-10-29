import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  exportRangeFor,
  exportPath,
  exportFileName,
  blobErrorMessage,
  downloadBlob,
  CSV_MIME,
} from '../transactionExport'

/**
 * 交易明细导出的前端工具测试。
 *
 * 这些行为都**不依赖网络**，却决定了导出"能不能用"：区间算错会导致对账缺几天、
 * 文件名带冒号在 Windows 上无法保存、Blob 错误体不解析则用户看不到
 * "请缩小时间范围"这句唯一有用的提示。
 */
describe('transactionExport', () => {
  const NOW = new Date('2026-10-29T12:34:00.000Z')

  describe('exportRangeFor', () => {
    it('按天数回溯，返回 RFC3339（ISO）区间', () => {
      const { from, to } = exportRangeFor(30, NOW)

      expect(to).toBe('2026-10-29T12:34:00.000Z')
      expect(from).toBe('2026-09-29T12:34:00.000Z')
      // 后端用 time.Parse(time.RFC3339) 解析，格式必须是可解析的
      expect(Number.isNaN(Date.parse(from))).toBe(false)
      expect(Number.isNaN(Date.parse(to))).toBe(false)
    })

    it('两端都归一成 UTC 序列化', () => {
      const { from, to } = exportRangeFor(7, NOW)
      // ISO 串以 Z 结尾 —— 不把本地时区偏移丢给后端，
      // 因为 credit_transactions.created_at 是无时区 timestamp（按 UTC 解释）
      expect(from.endsWith('Z')).toBe(true)
      expect(to.endsWith('Z')).toBe(true)
    })

    it('区间长度正确，且 from 恒早于 to', () => {
      for (const days of [7, 30, 90]) {
        const { from, to } = exportRangeFor(days, NOW)
        const spanDays = (Date.parse(to) - Date.parse(from)) / 86_400_000
        expect(spanDays).toBe(days)
      }
    })
  })

  describe('exportPath', () => {
    it('带上编码后的 from/to 查询参数', () => {
      const path = exportPath(30, NOW)

      expect(path.startsWith('/v1/billing/transactions.csv?')).toBe(true)
      // 冒号在查询串里必须被编码，否则部分代理会截断
      expect(path).toContain('from=2026-09-29T12%3A34%3A00.000Z')
      expect(path).toContain('to=2026-10-29T12%3A34%3A00.000Z')
    })
  })

  describe('exportFileName', () => {
    it('是 .csv，且只含 ASCII 与非冒号字符', () => {
      const name = exportFileName(30, NOW)

      expect(name).toBe('transactions_20260929T1234_20261029T1234.csv')
      expect(name.includes(':')).toBe(false)
      // Windows 上其余非法字符也一并挡掉
      expect(/[\\/:*?"<>|]/.test(name)).toBe(false)
      for (const ch of name) {
        expect(ch.charCodeAt(0)).toBeLessThan(128)
      }
    })
  })

  describe('blobErrorMessage', () => {
    const blobOf = (s: string) => new Blob([s], { type: 'application/json' })

    it('优先取 error 字段（Go 网关的错误体）', async () => {
      const msg = await blobErrorMessage(blobOf(
        JSON.stringify({ error: 'too many transactions (60000) in range, please narrow the date range' }),
      ))
      expect(msg).toBe('too many transactions (60000) in range, please narrow the date range')
    })

    it('回退到 detail（FastAPI 习惯）', async () => {
      const msg = await blobErrorMessage(blobOf(JSON.stringify({ detail: 'from must be RFC3339' })))
      expect(msg).toBe('from must be RFC3339')
    })

    it('再回退到 message', async () => {
      const msg = await blobErrorMessage(blobOf(JSON.stringify({ message: 'something broke' })))
      expect(msg).toBe('something broke')
    })

    it('空 body 返回 null', async () => {
      expect(await blobErrorMessage(blobOf(''))).toBeNull()
    })

    it('非 JSON（代理的 HTML 错误页）返回 null，不把 HTML 塞给用户', async () => {
      expect(await blobErrorMessage(blobOf('<html><body>502 Bad Gateway</body></html>'))).toBeNull()
    })

    it('JSON 但字段为空字符串时继续往下找', async () => {
      const msg = await blobErrorMessage(blobOf(JSON.stringify({ error: '  ', message: 'fallback' })))
      expect(msg).toBe('fallback')
    })
  })

  describe('downloadBlob', () => {
    let createObjectURL: ReturnType<typeof vi.fn>
    let revokeObjectURL: ReturnType<typeof vi.fn>
    let clicked: HTMLAnchorElement | null

    beforeEach(() => {
      createObjectURL = vi.fn(() => 'blob:mock-url')
      revokeObjectURL = vi.fn()
      // jsdom 没有实现 createObjectURL/revokeObjectURL，必须补上
      Object.defineProperty(URL, 'createObjectURL', {
        value: createObjectURL,
        writable: true,
        configurable: true,
      })
      Object.defineProperty(URL, 'revokeObjectURL', {
        value: revokeObjectURL,
        writable: true,
        configurable: true,
      })
      clicked = null
      // 拦截 a.click，避免 jsdom 报 "Not implemented: navigation"
      vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
        this: HTMLAnchorElement,
      ) {
        clicked = this
      })
    })

    afterEach(() => {
      vi.restoreAllMocks()
    })

    it('设置 download 与 href，并触发点击', () => {
      downloadBlob(new Blob(['a'], { type: CSV_MIME }), 'transactions.csv')

      expect(createObjectURL).toHaveBeenCalledTimes(1)
      expect(clicked).not.toBeNull()
      expect(clicked!.download).toBe('transactions.csv')
      expect(clicked!.href).toContain('blob:mock-url')
    })

    it('用完立刻释放 object URL（否则大文件会一直占内存）', () => {
      downloadBlob(new Blob(['a']), 'x.csv')
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:mock-url')
    })

    it('点击后把 a 从文档里移走，不留垃圾节点', () => {
      downloadBlob(new Blob(['a']), 'x.csv')
      expect(document.querySelectorAll('a[download]').length).toBe(0)
    })
  })
})
