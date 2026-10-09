import { describe, it, expect } from 'vitest'
import { toolSummary } from '../toolFamily'

/**
 * `utils/toolFamily.ts` 的测试（2026-10-09）—— 此前零测试 ✗。
 *
 * 第 145 轮把「人口」扫描扩到 `api / composables / utils / stores / router`：
 * **24 个模块、2,631 行**没有任何测试 import ✓，本轮挑 `toolFamily.ts`（102 行）——
 * 它是**纯逻辑**、且**用户可见**（每个工具行的摘要就是它生成的）✓，
 * 正好是第 142 轮 `ToolCallCard`（钉的是**接线**）的**逻辑那一半** ✓。
 *
 * **这个文件自己的注释就把判据写全了**（`:11-19`），所以用例直接照它写：
 * - `read_file {path, offset, limit}` **不能**摘成 `src/a.ts · 0`（第二个是无意义的 offset）✓；
 * - `shell_exec {command, timeout}` **不能**带上 timeout ✓；
 * - `run_code {code, language}` **不能**把整段代码塞进摘要 ✓；
 * - 未登记工具走**通用兜底**（前两个短字段），**与既有行为一致 ⇒ 零退化** ✓；
 * - 并且**绝不用正则猜类型**（引 ZCode 的踩坑：`TodoWrite` 里的 `Write` 会被当成文件写入）✓。
 */

describe('toolSummary（单条工具行的摘要）', () => {
  it('★ read_file 只取 path —— 不能把无意义的 offset 也摘进去', () => {
    const s = toolSummary('read_file', JSON.stringify({ path: 'src/a.ts', offset: 0, limit: 100 }))
    expect(s).toBe('src/a.ts')
    expect(s).not.toContain('0')
  })

  it('★ shell_exec 只取 command —— 不能带上 timeout', () => {
    const s = toolSummary('shell_exec', JSON.stringify({ command: 'npm test', timeout: 30000 }))
    expect(s).toBe('npm test')
    expect(s).not.toContain('30000')
  })

  it('★ run_code 不把整段代码塞进摘要：有 language 就用它，没有才退到"行数"', () => {
    const code = 'line1\nline2\nline3'
    // 有 language：摘要就是它（短、有意义）——**绝不是**整段代码
    const withLang = toolSummary('run_code', JSON.stringify({ code, language: 'python' }))
    expect(withLang).toBe('python')
    expect(withLang).not.toContain('line1')

    // 没有 language：才走 BULK_FIELDS，只报规模
    const noLang = toolSummary('run_code', JSON.stringify({ code }))
    expect(noLang).toContain('3')
    expect(noLang).not.toContain('line1')
  })

  it('★ 未登记的工具走通用兜底：前两个短字段，用 · 连接（零退化）', () => {
    const s = toolSummary('some_new_tool', JSON.stringify({ alpha: 'A', beta: 'B', gamma: 'C' }))
    expect(s).toBe('A · B')
    expect(s).not.toContain('C')
  })

  it('★ 参数不是合法 JSON 时不崩，原样返回（截断的半成品很常见）', () => {
    expect(toolSummary('read_file', '{"path": "a.tx')).toBe('{"path": "a.tx')
    expect(toolSummary('read_file', '')).toBe('')
  })

  it('超长值被裁剪并带省略号（maxLen 可调）', () => {
    const long = 'x'.repeat(200)
    const s = toolSummary('read_file', JSON.stringify({ path: long }))
    expect(s.length).toBeLessThanOrEqual(61)   // 60 + 省略号
    expect(s.endsWith('…')).toBe(true)

    expect(toolSummary('read_file', JSON.stringify({ path: long }), 10)).toBe(`${'x'.repeat(10)}…`)
  })

  it('空值/空白值被跳过，落到下一个候选字段', () => {
    const s = toolSummary('read_file', JSON.stringify({ path: '   ', file_path: 'b.ts' }))
    expect(s).toBe('b.ts')
  })
})
