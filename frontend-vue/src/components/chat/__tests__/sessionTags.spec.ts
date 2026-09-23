import { describe, expect, it } from 'vitest'

import { mergeTagOptions, normalizeTag, PRESET_TAGS, TAG_MAX_LEN } from '../sessionTags'

/**
 * 会话标签：把"标签恒定"改成"预设 + 自定义"（用户报告的问题）。
 *
 * 规则本身是纯函数，所以这组测试盯的是**规则**：候选怎么合并、输入怎么合法化 ——
 * UI 只负责渲染与输入。
 */
describe('normalizeTag', () => {
  it('去首尾空白（空串 = 清除标签）', () => {
    expect(normalizeTag('  工作  ')).toBe('工作')
    expect(normalizeTag('   ')).toBe('')
    expect(normalizeTag('')).toBe('')
    expect(normalizeTag(undefined)).toBe('')
    expect(normalizeTag(42)).toBe('')
  })

  it('超长按码点截断（中文不会被截成半个字）', () => {
    const long = '标'.repeat(TAG_MAX_LEN + 20)
    const out = normalizeTag(long)
    expect(Array.from(out)).toHaveLength(TAG_MAX_LEN)
    expect(out).toBe('标'.repeat(TAG_MAX_LEN))
  })

  it('emoji 这类代理对也算一个字符', () => {
    const raw = '🚀'.repeat(10)
    expect(normalizeTag(raw)).toBe(raw)
  })
})

describe('mergeTagOptions', () => {
  it('预设始终在候选里（没有自定义标签时也有可选项）', () => {
    expect(mergeTagOptions([])).toEqual([...PRESET_TAGS])
  })

  it('已使用过的标签自动进入候选（这是"自定义"能生效的关键）', () => {
    const out = mergeTagOptions(['重构', '工作'], ['工作', '学习'])
    expect(out).toEqual(['工作', '学习', '重构'])
  })

  it('去重且忽略空白项', () => {
    expect(mergeTagOptions([' 工作 ', '', '  '], ['工作'])).toEqual(['工作'])
  })

  it('顺序稳定：预设在前，其后按使用顺序', () => {
    expect(mergeTagOptions(['b', 'a', 'c'], ['z'])).toEqual(['z', 'b', 'a', 'c'])
  })
})
