import { describe, expect, it, vi, beforeEach } from 'vitest'

const mocks = vi.hoisted(() => ({
  post: vi.fn(),
  updateConversation: vi.fn(),
  forkConversation: vi.fn(),
}))

vi.mock('../../../api', () => ({
  api: { post: mocks.post, get: vi.fn() },
  updateConversation: mocks.updateConversation,
}))

vi.mock('../api', () => ({ forkConversation: mocks.forkConversation }))

import {
  appendNode,
  createSession,
  followupContent,
  forkSession,
  moveNode,
  removeNode,
  sendMessage,
  toggleNodeCollapsed,
  updateSessionMeta,
} from '../useMapActions'
import type { SessionMapNode } from '../types'

function node(overrides: Partial<SessionMapNode> & { id: string }): SessionMapNode {
  return { x: 0, y: 0, ...overrides }
}

beforeEach(() => {
  // mock 在所有用例间共享，不清空的话 `calls[0]` 会取到上一个用例的调用。
  vi.clearAllMocks()
})

describe('followupContent', () => {
  it('套上协议前缀 —— 这是历史数据与引擎都依赖的文本格式，不随语言变化', () => {
    expect(followupContent('继续')).toBe('【请简短回答问题】:(继续)')
  })
})

describe('createSession', () => {
  it('标题为空时用本地化的默认标题', async () => {
    mocks.post.mockResolvedValue({ data: { data: { id: 's1' } } })

    const created = await createSession('   ')

    expect(mocks.post).toHaveBeenCalledWith('/v1/conversations', expect.objectContaining({ title: '新对话' }))
    expect(created).toEqual({ id: 's1', title: '新对话' })
  })

  it('透传宿主的 llm_config，避免地图侧另造一套默认值', async () => {
    mocks.post.mockResolvedValue({ data: { data: { id: 's1' } } })

    await createSession('标题', { buildLlmConfig: () => ({ model: 'gpt-x', tools_mode: 'auto' }) })

    expect(mocks.post).toHaveBeenCalledWith('/v1/conversations', {
      title: '标题',
      llm_config: { model: 'gpt-x', tools_mode: 'auto' },
    })
  })

  it('响应缺少 id 时抛错，不返回指向不存在会话的假 id', async () => {
    mocks.post.mockResolvedValue({ data: { data: {} } })

    await expect(createSession('x')).rejects.toThrow('创建会话失败')
  })
})

describe('sendMessage', () => {
  it('普通消息原样发送，并带上发送侧幂等 id', async () => {
    mocks.post.mockResolvedValue({ data: {} })

    await sendMessage('s1', '你好')

    const [url, body] = mocks.post.mock.calls[0]
    expect(url).toBe('/submit')
    expect(body.content).toBe('你好')
    expect(body.session_id).toBe('s1')
    expect(typeof body.llm_config.client_msg_id).toBe('string')
  })

  it('追问加上协议前缀', async () => {
    mocks.post.mockResolvedValue({ data: {} })

    await sendMessage('s1', '继续', { followup: true })

    expect(mocks.post.mock.calls[0][1].content).toBe('【请简短回答问题】:(继续)')
  })

  it('请求失败向上抛，不静默吞掉', async () => {
    mocks.post.mockRejectedValue(new Error('boom'))

    await expect(sendMessage('s1', 'x')).rejects.toThrow('boom')
  })
})

describe('forkSession', () => {
  it('把分叉点作为 from_index 传下去', async () => {
    mocks.forkConversation.mockResolvedValue({ session_id: 's2', parent_session_id: 's1', branch_from_seq: 7, copied: 7 })

    const created = await forkSession('s1', 7, '分支')

    expect(mocks.forkConversation).toHaveBeenCalledWith('s1', { from_index: 7, title: '分支' })
    expect(created).toEqual({ id: 's2', title: '分支' })
  })

  it('分叉点 <= 0 时就地拒绝（后端会 400，提前拦下让错误就近显示）', async () => {
    await expect(forkSession('s1', 0, 'x')).rejects.toThrow('分叉点无效')
    expect(mocks.forkConversation).not.toHaveBeenCalled()
  })

  it('响应缺少 session_id 时抛错', async () => {
    mocks.forkConversation.mockResolvedValue({})

    await expect(forkSession('s1', 3, 'x')).rejects.toThrow('分叉失败')
  })
})

describe('updateSessionMeta', () => {
  it('透传 alias / tag（后端 conversation.go 接受这两个字段）', async () => {
    mocks.updateConversation.mockResolvedValue(undefined)

    await updateSessionMeta('s1', { alias: '备注', tag: 'urgent' })

    expect(mocks.updateConversation).toHaveBeenCalledWith('s1', { alias: '备注', tag: 'urgent' })
  })
})

describe('画布节点操作', () => {
  it('appendNode 生成 id，并把新节点放在父节点右侧', () => {
    const nodes = [node({ id: 'p', session_id: 's1', x: 80, y: 80 })]

    const next = appendNode(nodes, { sessionId: 's2', parentNodeId: 'p', branchFromSeq: 4 })
    const added = next.at(-1)!

    expect(added.id).toMatch(/^n-/)
    expect(added.parent_node_id).toBe('p')
    expect(added.edge_kind).toBe('branch')
    expect(added.x).toBeGreaterThan(80)
    expect(added.branch_from_seq).toBe(4)
    // 不可变：原数组未被改动
    expect(nodes).toHaveLength(1)
  })

  it('appendNode 没有父节点时落在原点附近，且不产生血缘字段', () => {
    const added = appendNode([], { sessionId: 's1' })[0]

    expect(added.parent_node_id).toBeUndefined()
    expect(added.branch_from_seq).toBeUndefined()
  })

  it('removeNode 连带删除子节点，不留悬空父引用', () => {
    const nodes = [
      node({ id: 'r' }),
      node({ id: 'a', parent_node_id: 'r' }),
      node({ id: 'a1', parent_node_id: 'a' }),
      node({ id: 'b' }),
    ]

    expect(removeNode(nodes, 'r').map(n => n.id)).toEqual(['b'])
  })

  it('removeNode 遇到环不会死循环', () => {
    const nodes = [
      node({ id: 'a', parent_node_id: 'b' }),
      node({ id: 'b', parent_node_id: 'a' }),
    ]

    expect(removeNode(nodes, 'a')).toEqual([])
  })

  it('moveNode 取整坐标（拖拽落点是小数）', () => {
    const next = moveNode([node({ id: 'a' })], 'a', { x: 12.7, y: -3.2 })

    expect(next[0]).toMatchObject({ x: 13, y: -3 })
  })

  it('toggleNodeCollapsed 翻转单个节点', () => {
    const nodes = [node({ id: 'a' }), node({ id: 'b', collapsed: true })]

    expect(toggleNodeCollapsed(nodes, 'a')[0].collapsed).toBe(true)
    expect(toggleNodeCollapsed(nodes, 'b')[1].collapsed).toBe(false)
  })
})
