import { describe, expect, it } from 'vitest'
import { agentResultToMarkdown, parseAgentResult, resultText } from '../agentResultMarkdown'

const session = (result: string | undefined, task = '总结这份文档', agent_name = '归档助手') =>
  ({ agent_name, task, result })

const asJson = (payload: unknown) => JSON.stringify(payload)

describe('parseAgentResult（宽松解析 session.result）', () => {
  it('空值返回空对象', () => {
    expect(parseAgentResult(undefined)).toEqual({})
    expect(parseAgentResult('')).toEqual({})
  })

  it('解析后端存的 JSON 字符串', () => {
    const parsed = parseAgentResult(asJson({ output: '结果', duration: 1.5, tool_calls: [1, 2] }))
    expect(parsed.output).toBe('结果')
    expect(parsed.duration).toBe(1.5)
    expect(parsed.tool_calls).toHaveLength(2)
  })

  it('解析失败时把原文当 output（脏数据不该让结果无法展示）', () => {
    expect(parseAgentResult('纯文本结果').output).toBe('纯文本结果')
  })

  it('非对象 JSON 不当作字段容器（保持既有页面行为）', () => {
    expect(parseAgentResult(asJson([]))).toEqual({})
    expect(parseAgentResult('123')).toEqual({})
  })
})

describe('resultText（取文优先级）', () => {
  it('error 优先于 output', () => {
    expect(resultText({ error: '炸了', output: '半截结果' })).toBe('炸了')
  })

  it('无 error 时取 output', () => {
    expect(resultText({ output: '结果' })).toBe('结果')
  })

  it('两者都空返回空串', () => {
    expect(resultText({})).toBe('')
    expect(resultText({ output: '   ' })).toBe('')
  })
})

describe('agentResultToMarkdown（运行结果沉淀为知识库文档）', () => {
  it('产出标题、任务与输出', () => {
    const md = agentResultToMarkdown(session(asJson({ output: '归档完成，共 12 个文件' })))
    expect(md.startsWith('# 运行结果 归档助手')).toBe(true)
    expect(md).toContain('## 任务')
    expect(md).toContain('总结这份文档')
    expect(md).toContain('## 输出')
    expect(md).toContain('归档完成，共 12 个文件')
  })

  it('显式标题优先于默认标题', () => {
    const md = agentResultToMarkdown(session(asJson({ output: 'x' })), '  自定义标题  ')
    expect(md.startsWith('# 自定义标题')).toBe(true)
  })

  it('只有错误时标注为「错误」而不是「输出」', () => {
    const md = agentResultToMarkdown(session(asJson({ error: '模型超时' })))
    expect(md).toContain('## 错误')
    expect(md).not.toContain('## 输出')
    expect(md).toContain('模型超时')
  })

  it('错误与输出并存时仍标为「输出」，内容取错误（与页面展示一致）', () => {
    const md = agentResultToMarkdown(session(asJson({ error: '部分失败', output: '半截' })))
    expect(md).toContain('## 输出')
    expect(md).toContain('部分失败')
  })

  it('耗时与工具调用数进入元信息行，缺失则不产出', () => {
    const withMeta = agentResultToMarkdown(session(asJson({ output: 'ok', duration: 2.34, tool_calls: [1, 2, 3] })))
    expect(withMeta).toContain('耗时 2.3s')
    expect(withMeta).toContain('工具调用 3 次')

    const withoutMeta = agentResultToMarkdown(session(asJson({ output: 'ok' })))
    expect(withoutMeta).not.toContain('耗时')
    expect(withoutMeta).not.toContain('工具调用')
    expect(withoutMeta).not.toContain('---')
  })

  it('解析失败时退化为纯文本 output', () => {
    const md = agentResultToMarkdown(session('这是没包成 JSON 的结果'))
    expect(md).toContain('## 输出')
    expect(md).toContain('这是没包成 JSON 的结果')
  })

  it('没有 agent 名字时用兜底标题', () => {
    const md = agentResultToMarkdown({ task: '任务', result: undefined, agent_name: undefined } as never)
    expect(md.startsWith('# 运行结果 Agent')).toBe(true)
  })

  it('任务与结果都为空时返回空串（调用方据此禁止上传）', () => {
    expect(agentResultToMarkdown(session(undefined, ''))).toBe('')
    expect(agentResultToMarkdown(session(asJson({ output: '   ' }), ''))).toBe('')
  })

  it('产出以换行结尾，便于追加', () => {
    expect(agentResultToMarkdown(session(asJson({ output: 'x' }))).endsWith('\n')).toBe(true)
  })
})
