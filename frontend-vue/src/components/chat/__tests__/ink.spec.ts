import { describe, expect, it } from 'vitest'

import { INK_MAX_CHARS, INK_MAX_STAGGER, inkCharsFor, inkIndexFor } from '../ink'

describe('ink 批内延迟计算', () => {
  it('只按本批新增字符计时，并封顶到 12 档', () => {
    // 整条都是本批：线性递增
    expect(inkIndexFor(0, 0)).toBe(0)
    expect(inkIndexFor(5, 0)).toBe(5)
    // 超过封顶：一次返回一大段也只铺开 12 档
    expect(inkIndexFor(20, 0)).toBe(INK_MAX_STAGGER)
    // 本批从 488 起：第 500 个字只排第 12 档，第 490 个排第 2 档
    expect(inkIndexFor(500, 488)).toBe(12)
    expect(inkIndexFor(490, 488)).toBe(2)
    // 早于本批（理论边界）夹到 0
    expect(inkIndexFor(1, 5)).toBe(0)
  })

  it('超过 2000 字退回整条插值', () => {
    expect(inkCharsFor('a'.repeat(INK_MAX_CHARS))).toHaveLength(INK_MAX_CHARS)
    expect(inkCharsFor('a'.repeat(INK_MAX_CHARS + 1))).toBeNull()
    expect(inkCharsFor('')).toBeNull()
  })
})
