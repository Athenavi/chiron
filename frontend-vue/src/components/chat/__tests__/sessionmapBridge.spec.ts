import { describe, expect, it, vi } from 'vitest'

import {
  FOLLOWUP_TEMPLATE,
  handleMapRpc,
  isMapRpc,
  type MapRpcHost,
} from '../sessionmapBridge'

/**
 * 会话地图 → 宿主的 RPC 桥。
 *
 * 回归背景（用户报告"地图中的会话无法产生有效的请求，包括追问"）：
 * 地图用 `dshRpc(...)`（postMessage + 等带 requestId 的回执）发起会话操作，
 * 而宿主从未实现回执 → 每次操作都空等 20 秒后报"会话服务未在规定时间内响应"。
 */
function makeHost(overrides: Partial<MapRpcHost> = {}) {
  const posted: Record<string, unknown>[] = []
  const host: MapRpcHost = {
    post: (payload) => posted.push(payload),
    createSession: vi.fn(async () => ({ id: 's-new', title: '新对话' })),
    sendMessage: vi.fn(async () => undefined),
    forkSession: vi.fn(async () => ({ id: 's-fork', title: '分支' })),
    updateSession: vi.fn(async () => undefined),
    ...overrides,
  }
  return { host, posted }
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0))

describe('sessionmapBridge', () => {
  it('非 RPC 消息不会被接管（宿主自己的分支继续处理）', () => {
    const { host, posted } = makeHost()
    expect(handleMapRpc({ type: 'synapse:activate-session' }, host)).toBe(false)
    expect(handleMapRpc(null, host)).toBe(false)
    expect(posted).toEqual([])
    expect(isMapRpc('synapse:send-message')).toBe(true)
    expect(isMapRpc('synapse:whatever')).toBe(false)
  })

  it('create-session：建会话并回执（地图靠 session.id 才能继续发消息）', async () => {
    const { host, posted } = makeHost()
    expect(handleMapRpc({ type: 'synapse:create-session', requestId: 'r1' }, host)).toBe(true)
    await flush()

    expect(host.createSession).toHaveBeenCalledWith('新对话')
    expect(posted).toEqual([
      { type: 'synapse:created-session', requestId: 'r1', session: { id: 's-new', title: '新对话' } },
    ])
  })

  it('send-message：把文本与 mode 原样交给宿主（追问走 followup）', async () => {
    const { host, posted } = makeHost()
    handleMapRpc(
      { type: 'synapse:send-message', requestId: 'r2', sessionId: 's1', text: '这次改动影响到哪些文件？', mode: 'followup' },
      host,
    )
    await flush()

    expect(host.sendMessage).toHaveBeenCalledWith('s1', '这次改动影响到哪些文件？', 'followup')
    expect(posted).toEqual([
      { type: 'synapse:message-sent', requestId: 'r2', session: { id: 's1' } },
    ])
  })

  it('fork-session：回执里必须带 id（地图用它记住分叉锚点）', async () => {
    const { host, posted } = makeHost()
    handleMapRpc(
      { type: 'synapse:fork-session', requestId: 'r3', sessionId: 's1', atSeq: 4, title: '分支' },
      host,
    )
    await flush()

    expect(host.forkSession).toHaveBeenCalledWith('s1', 4, '分支')
    expect(posted).toEqual([
      { type: 'synapse:forked-session', requestId: 'r3', session: { id: 's-fork', title: '分支' } },
    ])
  })

  it('缺参数时不打宿主，直接回错误（可见地失败而不是空等 20 秒）', async () => {
    const { host, posted } = makeHost()
    handleMapRpc({ type: 'synapse:send-message', requestId: 'r4', sessionId: '', text: 'hi' }, host)
    handleMapRpc({ type: 'synapse:send-message', requestId: 'r5', sessionId: 's1', text: '   ' }, host)
    handleMapRpc({ type: 'synapse:fork-session', requestId: 'r6', sessionId: '' }, host)
    await flush()

    expect(host.sendMessage).not.toHaveBeenCalled()
    expect(host.forkSession).not.toHaveBeenCalled()
    expect(posted.map((p) => p.type)).toEqual([
      'synapse:bridge-error',
      'synapse:bridge-error',
      'synapse:bridge-error',
    ])
    // requestId 必须带回，否则地图那边的 promise 永远 settle 不了
    expect(posted.map((p) => p.requestId)).toEqual(['r4', 'r5', 'r6'])
  })

  it('update-session：重命名/别名/标签经宿主写库（trim 后空串=清除）', async () => {
    const { host, posted } = makeHost()
    handleMapRpc(
      { type: 'synapse:update-session', requestId: 'r8', sessionId: 's1', alias: '  我的备注  ', tag: '工作' },
      host,
    )
    await flush()

    expect(host.updateSession).toHaveBeenCalledWith('s1', { alias: '我的备注', tag: '工作' })
    expect(posted).toEqual([
      {
        type: 'synapse:session-updated',
        requestId: 'r8',
        session: { id: 's1', alias: '我的备注', tag: '工作' },
      },
    ])
  })

  it('update-session：清空别名/标签也走同一条通道（空串透传）', async () => {
    const { host, posted } = makeHost()
    handleMapRpc({ type: 'synapse:update-session', requestId: 'r9', sessionId: 's1', alias: '' }, host)
    await flush()

    expect(host.updateSession).toHaveBeenCalledWith('s1', { alias: '' })
    expect(posted[0].type).toBe('synapse:session-updated')
  })

  it('update-session：没有字段 / 没有会话都是可见的失败', async () => {
    const { host, posted } = makeHost()
    handleMapRpc({ type: 'synapse:update-session', requestId: 'r10', sessionId: 's1' }, host)
    handleMapRpc({ type: 'synapse:update-session', requestId: 'r11', alias: 'x' }, host)
    await flush()

    expect(host.updateSession).not.toHaveBeenCalled()
    expect(posted.map((p) => [p.type, p.requestId])).toEqual([
      ['synapse:bridge-error', 'r10'],
      ['synapse:bridge-error', 'r11'],
    ])
  })

  it('宿主抛错时也回 bridge-error（把真实原因交给地图显示）', async () => {
    const { host, posted } = makeHost({
      sendMessage: vi.fn(async () => {
        throw new Error('会话正在运行中')
      }),
    })
    handleMapRpc({ type: 'synapse:send-message', requestId: 'r7', sessionId: 's1', text: 'hi' }, host)
    await flush()

    expect(posted).toEqual([
      { type: 'synapse:bridge-error', requestId: 'r7', message: '会话正在运行中' },
    ])
  })

  it('追问提示词格式与地图侧正则约定一致（改这里要同步 adapter 的 FOLLOWUP_RE）', () => {
    expect(FOLLOWUP_TEMPLATE('继续说明')).toBe('【请简短回答问题】:(继续说明)')
  })
})
