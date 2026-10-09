import { describe, it, expect, afterEach } from 'vitest'
import { formatDate, formatDateTime, formatMonthDay, formatSmart, formatTime } from '../datetime'
import { setLocale } from '../../i18n'
import { DEFAULT_LOCALE } from '../../i18n/languages'

/**
 * `utils/datetime.ts` 的测试（2026-10-09）—— 此前零测试 ✗。
 *
 * 第 145 轮把「人口」扫描扩到 `api / composables / utils / stores / router`（24 模块 / 2,631 行）✓，
 * 本轮挑 `datetime.ts`（50 行）：它是**纯逻辑**、**用户可见**，而且它的文件头把**要防的 bug**
 * 写得很清楚（`:4-6`）—— **项目里原有 20+ 处 `toLocaleString('zh-CN')`**，locale 写死在调用点，
 * 切到 en-US/ar 之后**日期仍是中文格式**；并称这是"i18n 最常见的漏网之鱼" ✓。
 *
 * **三条不变量**：
 * ① **非法/空输入返回 fallback，绝不吐出 `Invalid Date`**（`:15-19` 的 `parse`）——
 *    列表里出现 `Invalid Date` 是用户直接看得见的瑕疵 ✓；
 * ② **格式串是契约**（`YYYY-MM-DD HH:mm` 等）—— 表格/列表都按它排版 ✓；
 * ③ **不接收 locale 参数，结果自动跟随界面语言**（`:8-9`）—— 这正是它存在的理由 ✓，
 *    所以用**真实的 `setLocale()`** 来验证，而不是直接调 dayjs ✓。
 */

// 各用例之间恢复源语言（与 LanguageSwitcher.spec.ts 同款）
afterEach(() => setLocale(DEFAULT_LOCALE))

const ISO = '2026-09-15T10:49:00'

describe('utils/datetime（i18n 统一入口）', () => {
  it('★ 非法/空输入一律返回 fallback —— 绝不吐出 Invalid Date', () => {
    for (const bad of [null, undefined, '', 'not-a-date', 'x']) {
      expect(formatDateTime(bad)).toBe('-')
      expect(formatDate(bad)).toBe('-')
      expect(formatTime(bad)).toBe('-')
      expect(formatSmart(bad)).toBe('-')
    }
    // fallback 可定制（调用方按场景给不同占位）
    expect(formatDateTime(null, '—')).toBe('—')
    expect(formatDateTime('', '暂无')).toBe('暂无')
  })

  it('★ 三种基础格式是契约', () => {
    expect(formatDateTime(ISO)).toBe('2026-09-15 10:49')
    expect(formatDate(ISO)).toBe('2026-09-15')
    expect(formatTime(ISO)).toBe('10:49')
  })

  it('★ 跟随界面语言：同一天在 zh-CN 与 en-US 下月日写法不同', () => {
    setLocale('zh-CN')
    const zh = formatMonthDay(ISO)
    setLocale('en-US')
    const en = formatMonthDay(ISO)

    expect(zh).toContain('9月')      // zh-CN：9月15日
    expect(en).toContain('Sep')      // en-US：Sep 15
    expect(zh).not.toBe(en)          // 关键：**不是**写死一种
  })

  it('★ formatSmart：今天只显示时间，别的日子显示月日+时间', () => {
    const today = new Date()
    const hh = String(today.getHours()).padStart(2, '0')
    const mm = String(today.getMinutes()).padStart(2, '0')

    // 今天：只有 HH:mm（不含月日）
    expect(formatSmart(today)).toBe(`${hh}:${mm}`)

    // 明确不是今天：应含月日与时间（时间部分仍为 10:49）
    const other = formatSmart(ISO)
    expect(other).not.toBe('10:49')
    expect(other).toContain('10:49')
  })

  it('接受 Date / 数字时间戳 / 字符串三种入参（同一个瞬间结果一致）', () => {
    const d = new Date(ISO)
    expect(formatDateTime(d)).toBe('2026-09-15 10:49')
    expect(formatDateTime(d.getTime())).toBe('2026-09-15 10:49')
    expect(formatDateTime(ISO)).toBe('2026-09-15 10:49')
  })
})
