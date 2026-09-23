import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { beforeAll, describe, expect, it } from 'vitest'

/**
 * 会话地图的**追问卡投影**（用户要求：追问卡片必须保留，生命周期随会话消失）。
 *
 * 背景：追问是一次性问答（宿主用特定提示词格式标记），此前不落任何地图数据 ——
 * 只在 app.js 的 `state.pendingReplies` 里有个临时态，回合结束即消失，用户看不到
 * 自己问过什么、答了什么（"追问卡片完成后永久丢失"）。
 *
 * 现在由 adapter 把会话消息里的**追问对**投影成追加卡片（app.js 的规则是"每条 user
 * 消息一张卡"，同一会话的卡自动串成链），数据源就是会话消息 ⇒ 会话删除、卡片消失。
 *
 * adapter.js 是 `public/` 下的浏览器脚本（不经打包、没有模块导出），所以测试**真的把它
 * 载入 jsdom** 再调用它挂出来的 `window.__chironAdapter` —— 验证的是线上那份代码，
 * 而不是复制一份逻辑来"自证"。
 */
type AdapterApi = {
  followupQuestion: (text: unknown) => string | null
  followupMessages: (list: Array<Record<string, unknown>>) => Array<Record<string, unknown>>
  cardMessages: (node: Record<string, unknown>, list: Array<Record<string, unknown>>) => Array<Record<string, unknown>>
  clampFollowupText: (text: unknown, max: number) => string
  FOLLOWUP_MAX: number
}

let api: AdapterApi

beforeAll(() => {
  // vitest 的 cwd 是 frontend-vue（不能用 import.meta.url —— 在 vite-node 下它不是 file: scheme）
  const src = readFileSync(resolve(process.cwd(), 'public/sessionmap/adapter.js'), 'utf8')
  // jsdom 环境：window/document/console 都在；adapter 只做包装与监听，不发请求
  ;(window as unknown as { eval: (code: string) => void }).eval(src)
  api = (window as unknown as { __chironAdapter: AdapterApi }).__chironAdapter
  expect(api, 'adapter 应该挂出 __chironAdapter 出口').toBeTruthy()
})

const FOLLOWUP = (text: string) => `（【请简短回答问题】:(${text})）`
/** 现在宿主发送的格式（不带外层全角括号）—— 两种都必须认得，库里两种都有 */
const FOLLOWUP_NEW = (text: string) => `【请简短回答问题】:(${text})`

function msg(role: string, content: string, seq: number) {
  return { role, content, created_at: '2026-09-23T10:00:00Z', seq }
}

/** 与 adapter.loadMessages 的输出形状一致（它会把 role 也带上） */
function loaded(role: string, content: string, seq: number) {
  return {
    id: 'm' + seq,
    kind: role === 'user' ? 'user' : 'assistant',
    role,
    text: content,
    at: '2026-09-23T10:00:00Z',
    sourceSeq: seq,
  }
}

describe('追问的提示词解析', () => {
  it('解析出用户的原话（卡片要显示原话，不是那串标记）', () => {
    expect(api.followupQuestion(FOLLOWUP('这次改动影响了哪些文件？'))).toBe('这次改动影响了哪些文件？')
  })

  it('普通消息不算追问', () => {
    expect(api.followupQuestion('帮我重构一下 utils.py')).toBeNull()
    expect(api.followupQuestion('')).toBeNull()
    expect(api.followupQuestion(undefined)).toBeNull()
  })

  it('原文里带括号/换行也不截断', () => {
    expect(api.followupQuestion(FOLLOWUP('(a) 与 (b)\n第二行'))).toBe('(a) 与 (b)\n第二行')
  })

  it('库里两种历史格式都认：宿主现在发的（无外层括号）与早先 adapter 发的（有）', () => {
    expect(api.followupQuestion(FOLLOWUP_NEW('只改这一处够吗？'))).toBe('只改这一处够吗？')
    expect(api.followupQuestion(FOLLOWUP('只改这一处够吗？'))).toBe('只改这一处够吗？')
  })
})

describe('追问卡投影', () => {
  it('把追问对投影成"问题 + 回答"两条消息（= 一张卡）', () => {
    const list = [
      loaded('user', '帮我看看这个 bug', 1),
      loaded('assistant', '问题在 utils.py:12', 2),
      loaded('user', FOLLOWUP('只改这一处够吗？'), 3),
      loaded('assistant', '够，但建议补一个测试', 4),
    ]
    const out = api.followupMessages(list)

    expect(out).toHaveLength(2)
    expect(out[0]).toMatchObject({ kind: 'user', text: '只改这一处够吗？', sourceSeq: 3 })
    expect(out[1]).toMatchObject({ kind: 'assistant', text: '够，但建议补一个测试', sourceSeq: 3 })
  })

  it('回答取"最后一条真正的助手消息"，跳过 tool 回显', () => {
    const list = [
      loaded('user', FOLLOWUP('跑一下测试'), 1),
      loaded('tool', '工具没有输出', 2),
      loaded('assistant', '先回答一半', 3),
      loaded('assistant', '最终结论：全绿', 4),
    ]
    const out = api.followupMessages(list)
    expect(out).toHaveLength(2)
    expect(out[1].text).toBe('最终结论：全绿')
  })

  it('追问还没回答时只出问题（卡片等回答，配合 live-reply 流式显示）', () => {
    const list = [loaded('user', FOLLOWUP('在吗'), 1)]
    const out = api.followupMessages(list)
    expect(out).toHaveLength(1)
    expect(out[0]).toMatchObject({ kind: 'user', text: '在吗' })
  })

  it('没有追问就不产生额外卡片（保持"一会话一主卡"）', () => {
    const list = [loaded('user', '普通提问', 1), loaded('assistant', '普通回答', 2)]
    expect(api.followupMessages(list)).toEqual([])
  })

  it('多轮追问按顺序各成一张卡', () => {
    const list = [
      loaded('user', FOLLOWUP('第一问'), 1),
      loaded('assistant', '第一答', 2),
      loaded('user', '中间插一句普通对话', 3),
      loaded('assistant', '普通回答', 4),
      loaded('user', FOLLOWUP('第二问'), 5),
      loaded('assistant', '第二答', 6),
    ]
    const out = api.followupMessages(list)
    expect(out.map((m) => m.text)).toEqual(['第一问', '第一答', '第二问', '第二答'])
  })

  it('超长问答被截断（卡片只给一眼可读的量，完整内容去对话页）', () => {
    const long = 'x'.repeat(api.FOLLOWUP_MAX + 50)
    const out = api.followupMessages([
      loaded('user', FOLLOWUP(long), 1),
      loaded('assistant', long, 2),
    ])
    expect(String(out[0].text)).toHaveLength(api.FOLLOWUP_MAX + 1) // +1 = 省略号
    expect(String(out[1].text).endsWith('…')).toBe(true)
  })
})

describe('合成卡片消息', () => {
  it('主卡是"标题 + 最新助手回复"，追问卡挂在它后面', () => {
    const node = { title: '会话标题' }
    const base = [
      loaded('user', '帮我看 bug', 1),
      loaded('assistant', '摘要用的最后一条回复', 2),
    ]
    expect(api.cardMessages(node, base).map((m) => m.text)).toEqual([
      '会话标题',
      '摘要用的最后一条回复',
    ])

    // 追加一轮追问后：主卡的摘要仍是"最新一条助手回复"，追问卡跟在主卡之后
    const withFollowup = base.concat([
      loaded('user', FOLLOWUP('只改一处够吗'), 3),
      loaded('assistant', '够', 4),
    ])
    const out = api.cardMessages(node, withFollowup)
    expect(out.slice(0, 2).map((m) => m.text)).toEqual(['会话标题', '够'])
    expect(out.slice(2).map((m) => m.text)).toEqual(['只改一处够吗', '够'])
  })

  it('没有标题时给一个占位标题（画布上不出现空卡）', () => {
    const out = api.cardMessages({}, [])
    expect(out[0].text).toBe('(未命名会话)')
  })

  // ── 回归：消息正文的字段名 ──
  //
  // 实测缺陷：cardMessages 的入参是 loadMessages **映射后**的列表（正文在 `text`），
  // 而旧实现的 summarizeSession 只读 `content` —— 于是**主卡的摘要恒为空**，
  // 卡片上永远只剩标题。这里同时钉住两种形状。
  it('正文用 content 字段时摘要照样取得到', () => {
    const raw = [
      { role: 'user', content: '问题' },
      { role: 'assistant', content: '摘要文本' },
    ]
    expect(api.cardMessages({ title: 't' }, raw).map((m) => m.text)).toEqual(['t', '摘要文本'])
  })

  it('正文用 content 字段时追问也能投影', () => {
    const raw = [
      { role: 'user', content: FOLLOWUP('问一句') },
      { role: 'assistant', content: '答一句' },
    ]
    expect(api.followupMessages(raw).map((m) => m.text)).toEqual(['问一句', '答一句'])
  })
})
