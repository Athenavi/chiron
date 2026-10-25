import { describe, expect, it } from 'vitest'
import { fileTypeBadge, splitFileName } from '../fileName'

describe('文件名双向中间截断（CDS v1/02 §2.5.3）', () => {
  it('短名字整段留在 tail，不做无意义的切分', () => {
    expect(splitFileName('a.ts')).toEqual({ head: '', tail: 'a.ts' })
    expect(splitFileName('readme.md')).toEqual({ head: '', tail: 'readme.md' })
  })

  it('长名字让位只发生在中间：扩展名与尾部词干完整保留', () => {
    const name = 'deep_reinforcement_learning_policy_gradient_v3.tsx'
    const { head, tail } = splitFileName(name)

    // 断言结构而不是数出来的字符位置：切分点可以调（TAIL_KEEP），契约不能变。
    expect(head + tail).toBe(name)              // 无损：两段拼起来还是原名
    expect(tail.startsWith('_')).toBe(false)    // 不该在下划线前切断
    expect(tail.endsWith('.tsx')).toBe(true)    // 扩展名必须看得见
    expect(tail.includes('v3')).toBe(true)      // 版本号留在尾部，能认出是哪一版
    expect(head.startsWith('deep_reinforcement')).toBe(true)
    expect(head.length).toBeGreaterThan(0)
  })

  it('无扩展名时整段按词干处理，不会造出一个假扩展名', () => {
    const name = 'a_very_long_filename_without_extension'
    const { head, tail } = splitFileName(name)
    expect(head + tail).toBe(name)
    expect(tail).toBe('_extension')            // 尾部词干可见
    expect(tail.includes('.')).toBe(false)     // 没有扩展名就不该凭空出现点
  })

  it('以点开头的名字不把点当扩展名分隔符', () => {
    // .gitignore 里的点是名字的一部分，不是扩展名
    expect(splitFileName('.gitignore')).toEqual({ head: '', tail: '.gitignore' })
    expect(fileTypeBadge('.gitignore')).toBe('FILE')
  })

  it('多个点时以最后一段为扩展名', () => {
    const name = 'archive_2026_quarterly_report_final.tar.gz'
    const { head, tail } = splitFileName(name)
    expect(head + tail).toBe(name)
    expect(tail.endsWith('.tar.gz') || tail.endsWith('.gz')).toBe(true)
  })

  it('空值与畸形输入不抛错', () => {
    expect(splitFileName('')).toEqual({ head: '', tail: '' })
    expect(splitFileName(undefined as unknown as string)).toEqual({ head: '', tail: '' })
    // 结尾是点：不构成扩展名，整段当词干
    const dotEnded = 'weird_name_that_is_long_enough.'
    const { head, tail } = splitFileName(dotEnded)
    expect(head + tail).toBe(dotEnded)
    expect(tail.endsWith('enough.')).toBe(true)
  })
})

describe('文件类型徽标（CDS v1/02 §2.5.4）', () => {
  it('后缀转大写', () => {
    expect(fileTypeBadge('main.ts')).toBe('TS')
    expect(fileTypeBadge('script.py')).toBe('PY')
    expect(fileTypeBadge('report.pdf')).toBe('PDF')
    expect(fileTypeBadge('data.csv')).toBe('CSV')
  })

  it('超过 4 字符的后缀截断', () => {
    expect(fileTypeBadge('page.html')).toBe('HTML')
    expect(fileTypeBadge('sheet.xlsx')).toBe('XLSX')
    expect(fileTypeBadge('font.woff2')).toBe('WOFF')
  })

  it('认不出后缀时给 FILE 而不是留空', () => {
    expect(fileTypeBadge('LICENSE')).toBe('FILE')
    expect(fileTypeBadge('.gitignore')).toBe('FILE')
    expect(fileTypeBadge('')).toBe('FILE')
    expect(fileTypeBadge(undefined as unknown as string)).toBe('FILE')
  })
})
