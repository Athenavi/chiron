import { describe, expect, it } from 'vitest'
import {
  DEFAULT_RESIDENT_TAIL_TURNS,
  headerKey,
  itemKey,
  projectTranscript,
  toolGroupKind,
  toolGroupSummary,
} from '../transcriptProjection'
import type { ChatItem } from '../chat-types'

const user = (id: string, turnId: string): ChatItem => ({ kind: 'text', role: 'user', content: 'q', id, turnId })
const assistant = (id: string, turnId: string): ChatItem => ({ kind: 'text', role: 'assistant', content: 'a', id, turnId })
const reasoning = (id: string, turnId: string): ChatItem => ({ kind: 'reasoning', content: 'r', id, turnId })
const call = (id: string, name: string, turnId: string): ChatItem => ({ kind: 'tool_call', id, name, arguments: '{}', status: 'done', turnId })
const result = (toolCallId: string, turnId: string): ChatItem => ({ kind: 'tool_result', toolCallId, content: '{}', isError: false, turnId })

/** 两个回合：第一个含思考 + 两次读取工具（相邻，应合成一个 explore 组），第二个只有正文。 */
function sample(): ChatItem[] {
  return [
    user('m0', 'turn-1'),
    reasoning('m0', 'turn-1'),
    call('c1', 'read_file', 'turn-1'),
    result('c1', 'turn-1'),
    call('c2', 'grep_files', 'turn-1'),
    result('c2', 'turn-1'),
    assistant('m1', 'turn-1'),
    user('m2', 'turn-2'),
    assistant('m3', 'turn-2'),
  ]
}

describe('transcriptProjection（行投影）', () => {
  it('flat 模式与 items 一一对应：key、顺序、下标都不变（投影层不引入几何变化）', () => {
    const items = sample()
    const flat = projectTranscript({ items })
    expect(flat.keys).toEqual(items.map((item, index) => itemKey(item, index)))
    expect(flat.rows.map(row => row.index)).toEqual(items.map((_, index) => index))
    expect(flat.rows.some(row => row.header)).toBe(false)
    expect(flat.keys.some(key => key.startsWith('h:'))).toBe(false)
  })

  it('flat 模式保留窗口化的 kind 口径（estimateRowSize 的输入）', () => {
    const items = sample()
    const flat = projectTranscript({ items })
    expect(flat.rows.map(row => row.kind)).toEqual(items.map(item => item.kind))
  })

  it('尾部常驻按回合判定，且不因折叠而改变', () => {
    const items = sample()
    const flat = projectTranscript({ items, residentTailTurns: 1 })
    // 只有最后一个回合常驻
    expect(flat.rows.map(row => row.resident)).toEqual([false, false, false, false, false, false, false, true, true])
    const grouped = projectTranscript({ items, residentTailTurns: 1, mode: 'grouped' })
    expect(grouped.rows.find(row => row.key === headerKey('t:turn-1'))?.resident).toBe(false)
    expect(grouped.rows.find(row => row.key === itemKey(items[7]!, 7))?.resident).toBe(true)
  })

  it('无回合身份的行（实时流 / 旧数据）不按回合常驻，交给按行兜底', () => {
    const items: ChatItem[] = [
      { kind: 'text', role: 'assistant', content: 'a', id: 'n1' },
      { kind: 'text', role: 'assistant', content: 'b', id: 'n2' },
    ]
    expect(projectTranscript({ items }).rows.every(row => !row.resident)).toBe(true)
  })

  it('默认常驻回合数覆盖活跃回合', () => {
    const flat = projectTranscript({ items: sample() })
    expect(DEFAULT_RESIDENT_TAIL_TURNS).toBe(2)
    expect(flat.rows.slice(7).every(row => row.resident)).toBe(true)
  })

  it('grouped 模式：回合头插在首个非用户行之前，统计思考/工具/正文', () => {
    const items = sample()
    const { rows } = projectTranscript({ items, mode: 'grouped' })
    const head = rows.find(row => row.key === headerKey('t:turn-1'))
    expect(head?.kind).toBe('turn_header')
    expect(head?.header?.scope).toBe('turn')
    expect(head?.header?.summary).toBe('思考 ×1 · 工具 ×2 · 正文')
    // 用户消息仍在该回合头之前
    expect(rows.findIndex(row => row.key === headerKey('t:turn-1'))).toBeGreaterThan(rows.findIndex(row => row.key === itemKey(items[0]!, 0)))
  })

  it('grouped 模式：相邻的同类工具合成一组，组头给出分档统计', () => {
    const items = sample()
    const { rows } = projectTranscript({ items, mode: 'grouped' })
    const group = rows.find(row => row.kind === 'tool_group_header')
    expect(group?.header?.fold).toBe('t:turn-1:g0')
    expect(group?.header?.group).toBe('explore')
    expect(group?.header?.summary).toBe('读取 1 · 搜索 1')
  })

  it('完成的回合里工具组默认收起：只留组头，组内行不进入渲染序列', () => {
    const items = sample()
    const { rows } = projectTranscript({ items, mode: 'grouped' })
    const keys = rows.map(row => row.key)
    expect(keys).toContain(headerKey('t:turn-1:g0'))
    expect(keys).not.toContain(itemKey(items[2]!, 2))
    expect(keys).not.toContain(itemKey(items[3]!, 3))
  })

  it('展开工具组后，tool_call 与配对的 tool_result 一起出现（不拆散配对）', () => {
    const items = sample()
    const folds = new Map([['t:turn-1:g0', true]])
    const { rows } = projectTranscript({ items, mode: 'grouped', folds })
    const keys = rows.map(row => row.key)
    expect(keys).toEqual(expect.arrayContaining([
      itemKey(items[2]!, 2),
      itemKey(items[3]!, 3),
      itemKey(items[4]!, 4),
      itemKey(items[5]!, 5),
    ]))
  })

  it('折叠整个回合时用户消息仍然可见（折叠不能藏起提问）', () => {
    const items = sample()
    const folds = new Map([['t:turn-1', false]])
    const { rows } = projectTranscript({ items, mode: 'grouped', folds })
    const keys = rows.map(row => row.key)
    expect(keys).toContain(itemKey(items[0]!, 0))
    expect(keys).toContain(headerKey('t:turn-1'))          // 头行作为「已收起」的入口
    expect(keys).not.toContain(itemKey(items[1]!, 1))      // 思考被收起
    expect(keys).not.toContain(itemKey(items[6]!, 6))      // 正文被收起
  })

  it('活跃回合的工具组默认展开（完成回合才默认收起）', () => {
    const items: ChatItem[] = [user('m0', 'turn-1'), call('c1', 'read_file', 'turn-1'), result('c1', 'turn-1')]
    // `running: true` = 该轮**仍在跑**（§4.7 起活跃判据由**生命周期**给，不再只看位置）
    const { rows } = projectTranscript({ items, mode: 'grouped', running: true })
    expect(rows.map(row => row.key)).toContain(itemKey(items[1]!, 1))
  })

  /**
   * §4.7 的回归：**不带 token 的 `done` 也必须终结该轮**。
   *
   * 原先活跃判据是**位置**（`turn.to === items.length`），而 `ChatView` 只在 `done` **带 token** 时
   * 才压入 `turn_stats` ⇒ 一次不带 token 的 `done` 会让最后一轮**永远** `status: 'running'`
   * 且工具组默认展开 ✗（用户看得见的 bug）。现在由 `running`（`MessageList` 传自己的 `loading`）
   * 提供生命周期信号 ⇒ `done` / `cancelled` / `error` 之后 `loading=false` ⇒ 该轮终结 ✓。
   *
   * 注意下面这组 items **没有 `turn_stats` 行** —— 那正是"不带 token 的 done"的形态。
   */
  it('★ 没有 token 的 done 也终结轮次（§4.7）：工具组收起', () => {
    const items: ChatItem[] = [user('m0', 'turn-1'), call('c1', 'read_file', 'turn-1'), result('c1', 'turn-1')]
    const done = projectTranscript({ items, mode: 'grouped', running: false })
    expect(done.rows.map(r => r.key)).not.toContain(itemKey(items[1]!, 1))

    // 对照：仍在运行时必须展开（否则就是"修好了 bug 但把展开也修没了"）
    const running = projectTranscript({ items, mode: 'grouped', running: true })
    expect(running.rows.map(r => r.key)).toContain(itemKey(items[1]!, 1))
  })

  it('纯正文回合不产生折叠入口（折起来也看得见全部内容）', () => {
    const items: ChatItem[] = [user('m0', 'turn-1'), assistant('m1', 'turn-1')]
    const { rows } = projectTranscript({ items, mode: 'grouped' })
    expect(rows.some(row => row.kind === 'turn_header')).toBe(false)
    expect(rows.map(row => row.key)).toEqual([itemKey(items[0]!, 0), itemKey(items[1]!, 1)])
  })

  it('回填的折叠态包含投影补出的默认值，供渲染层画 chevron', () => {
    const { folds } = projectTranscript({ items: sample(), mode: 'grouped' })
    expect(folds.get('t:turn-1')).toBe(true)
    expect(folds.get('t:turn-1:g0')).toBe(false)
  })

  it('跨过正文的工具结果另开一组（折叠一组不会连带藏掉中间那段思考）', () => {
    const items: ChatItem[] = [
      user('m0', 'turn-1'),
      call('c1', 'read_file', 'turn-1'),
      reasoning('m0', 'turn-1'),
      result('c1', 'turn-1'),
    ]
    const folds = new Map([['t:turn-1:g0', true]])
    const { rows } = projectTranscript({ items, mode: 'grouped', folds })
    // 第二个组（孤立 result）默认收起，思考行不受影响
    expect(rows.map(row => row.key)).toContain(itemKey(items[2]!, 2))
    expect(rows.some(row => row.header?.fold === 't:turn-1:g1')).toBe(true)
  })

  it('itemKey 口径与 MessageList 现状一致（无 turnId 走 t:- 兜底，无 id 用下标）', () => {
    expect(itemKey({ kind: 'text', role: 'user', content: 'x', id: 'm1', turnId: 'turn-1' }, 0)).toBe('t:turn-1:text:m1')
    expect(itemKey({ kind: 'reasoning', content: 'x' }, 3)).toBe('t:-:reasoning:idx3')
  })
})

describe('工具语义分组（对应 python-engine 工具注册表）', () => {
  it('按真实工具名归类', () => {
    expect(toolGroupKind('read_file')).toBe('explore')
    expect(toolGroupKind('kb_search')).toBe('explore')
    expect(toolGroupKind('write_file')).toBe('modify')
    expect(toolGroupKind('remember')).toBe('modify')
    expect(toolGroupKind('subagent')).toBe('delegate')
    expect(toolGroupKind('image_generate')).toBe('delegate')
    expect(toolGroupKind('shell_exec')).toBe('shell')
    expect(toolGroupKind('browser_navigate')).toBe('shell')
    expect(toolGroupKind('run_in_background')).toBe('shell')
  })

  it('未识别工具（MCP/插件）保守归 modify，且统计进「其它」而不是「编辑」', () => {
    expect(toolGroupKind('my_mcp_server_tool')).toBe('modify')
    expect(toolGroupSummary('modify', ['my_mcp_server_tool'])).toBe('其它 1')
  })

  it('组头摘要按语义分档计数', () => {
    expect(toolGroupSummary('explore', ['read_file', 'grep_files', 'read_file'])).toBe('读取 2 · 搜索 1')
    expect(toolGroupSummary('modify', ['write_file', 'edit_file'])).toBe('写入 1 · 编辑 1')
    expect(toolGroupSummary('delegate', ['subagent', 'graph_run'])).toBe('委派 2')
    expect(toolGroupSummary('shell', ['shell_exec', 'run_in_background'])).toBe('命令 1 · 作业 1')
  })
})
