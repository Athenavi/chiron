import { describe, expect, it } from 'vitest'

import {
  ancestorsToExpand,
  buildMapGraph,
  findCardBySession,
  type MapSessionMeta,
} from '../useSessionMapGraph'
import type { SessionMapNode } from '../types'

/** 造一个地图节点；只写关心的字段，其余取后端默认值。 */
function node(overrides: Partial<SessionMapNode> & { id: string }): SessionMapNode {
  return { x: 0, y: 0, edge_kind: 'manual', ...overrides }
}

function sessionMap(...metas: MapSessionMeta[]): Map<string, MapSessionMeta> {
  return new Map(metas.map(m => [m.id, m]))
}

describe('buildMapGraph', () => {
  it('把节点投影成卡片，并保留后端持久化的坐标', () => {
    const graph = buildMapGraph([node({ id: 'n1', session_id: 's1', x: 120, y: -40 })])

    expect(graph.cards).toHaveLength(1)
    expect(graph.cards[0]).toMatchObject({ id: 'n1', sessionId: 's1', x: 120, y: -40, hasSession: true })
  })

  it('标题优先级：别名 > 节点标题 > 会话标题', () => {
    // 两个会话分开造：带别名的会话无法用来验证「回落到会话标题」，
    // 因为别名本身就压过标题 —— 曾经把两者混在一个用例里，断言与实现都没问题，
    // 是这条测试自己测错了对象。
    const sessions = sessionMap(
      { id: 's1', alias: '别名', title: '会话标题' },
      { id: 's2', title: '只有标题' },
    )

    // 有别名：别名胜出，节点标题与会话标题都不参与
    expect(buildMapGraph([node({ id: 'n1', session_id: 's1', title: '节点标题' })], { sessions }).cards[0].title)
      .toBe('别名')
    expect(buildMapGraph([node({ id: 'n3', session_id: 's1' })], { sessions }).cards[0].title)
      .toBe('别名')
    // 无别名：节点标题胜过会话标题
    expect(buildMapGraph([node({ id: 'n4', session_id: 's2', title: '节点标题' })], { sessions }).cards[0].title)
      .toBe('节点标题')
  })

  it('别名与节点标题都为空时回落到会话标题', () => {
    const graph = buildMapGraph(
      [node({ id: 'n1', session_id: 's1' })],
      { sessions: sessionMap({ id: 's1', title: '会话标题' }) },
    )

    expect(graph.cards[0].title).toBe('会话标题')
  })

  it('找不到会话元数据时仍渲染卡片，标题回落到会话 id', () => {
    // 热层快照可能比会话列表新：节点已存、会话还没出现在本次列表里。
    const graph = buildMapGraph([node({ id: 'n1', session_id: 's-orphan' })])

    expect(graph.cards).toHaveLength(1)
    expect(graph.cards[0].title).toBe('s-orphan')
  })

  it('没有 session_id 的占位卡保留，但标记为不可发消息', () => {
    const graph = buildMapGraph([node({ id: 'n1' })])

    expect(graph.cards[0]).toMatchObject({ hasSession: false, title: '' })
  })

  it('隐藏节点（后端 hidden）不进画布', () => {
    const graph = buildMapGraph([
      node({ id: 'n1', session_id: 's1' }),
      node({ id: 'n2', session_id: 's2', hidden: true }),
    ])

    expect(graph.cards.map(c => c.id)).toEqual(['n1'])
  })

  it('连线透传 edge_kind，且只连两端都可见的边', () => {
    const graph = buildMapGraph([
      node({ id: 'root', session_id: 's1' }),
      node({ id: 'a', session_id: 's2', parent_node_id: 'root', edge_kind: 'branch' }),
      node({ id: 'b', session_id: 's3', parent_node_id: 'gone' }),
    ])

    // root→a 保留；b 的父节点不在快照里，因此不产生悬空边
    expect(graph.edges).toEqual([
      { id: 'root->a', source: 'root', target: 'a', kind: 'branch' },
    ])
  })

  it('edge_kind 为空时回落为 manual（与后端 COALESCE 默认值同源）', () => {
    const graph = buildMapGraph([
      node({ id: 'root' }),
      node({ id: 'a', parent_node_id: 'root', edge_kind: '' }),
    ])

    expect(graph.edges[0].kind).toBe('manual')
  })

  it('统计直接子节点数与全部后代数', () => {
    const graph = buildMapGraph([
      node({ id: 'root' }),
      node({ id: 'a', parent_node_id: 'root' }),
      node({ id: 'b', parent_node_id: 'root' }),
      node({ id: 'a1', parent_node_id: 'a' }),
    ])
    const byId = new Map(graph.cards.map(c => [c.id, c]))

    expect(byId.get('root')).toMatchObject({ childCount: 2, descendantCount: 3 })
    expect(byId.get('a')).toMatchObject({ childCount: 1, descendantCount: 1 })
    expect(byId.get('b')).toMatchObject({ childCount: 0, descendantCount: 0 })
  })

  it('折叠时隐藏整棵子树，但折叠根自己留在画布上', () => {
    // 折叠根必须可见，否则用户失去「展开」入口 —— 这是旧实现踩过的坑
    // （app.js:880-882 的注释里写明必须把折叠根从 hidden 集合里救回来）。
    const graph = buildMapGraph(
      [
        node({ id: 'root' }),
        node({ id: 'a', parent_node_id: 'root' }),
        node({ id: 'a1', parent_node_id: 'a' }),
        node({ id: 'b', parent_node_id: 'root' }),
      ],
      { collapsed: new Set(['a']) },
    )

    expect(graph.cards.map(c => c.id)).toEqual(['root', 'a', 'b'])
    expect(graph.cards.find(c => c.id === 'a')?.collapsed).toBe(true)
  })

  it('折叠后不残留指向被隐藏节点的悬空边', () => {
    const graph = buildMapGraph(
      [
        node({ id: 'root' }),
        node({ id: 'a', parent_node_id: 'root' }),
        node({ id: 'a1', parent_node_id: 'a' }),
      ],
      { collapsed: new Set(['a']) },
    )

    expect(graph.edges.map(e => e.id)).toEqual(['root->a'])
  })

  it('父子关系成环时不递归爆栈，后代计数有限且有界', () => {
    // 脏数据：a→b→a 互为父节点。旧实现用了一整套显式栈来做环检测（app.js:884-938），
    // 新实现简化成「路径上遇到过就不再深入」：环内的计数不精确（各自把对方算一次），
    // 但**必然终止**且不超过节点总数 —— 这是刻意的取舍，UI 只需要一个能渲染的数字。
    const nodes = [
      node({ id: 'a', parent_node_id: 'b' }),
      node({ id: 'b', parent_node_id: 'a' }),
    ]
    const graph = buildMapGraph(nodes)

    expect(graph.cards).toHaveLength(2)
    for (const card of graph.cards) {
      expect(Number.isFinite(card.descendantCount)).toBe(true)
      expect(card.descendantCount).toBeLessThanOrEqual(nodes.length)
    }
  })

  it('visibleOnly 用于搜索过滤，同时裁掉被过滤节点参与的边', () => {
    const graph = buildMapGraph(
      [
        node({ id: 'root' }),
        node({ id: 'a', parent_node_id: 'root' }),
      ],
      { visibleOnly: new Set(['a']) },
    )

    expect(graph.cards.map(c => c.id)).toEqual(['a'])
    expect(graph.edges).toEqual([])
  })

  it('running 按 session_id 标记，占位卡永远不显示运行态', () => {
    const graph = buildMapGraph(
      [node({ id: 'n1', session_id: 's1' }), node({ id: 'n2' })],
      { running: new Set(['s1', '']) },
    )
    const byId = new Map(graph.cards.map(c => [c.id, c]))

    expect(byId.get('n1')?.running).toBe(true)
    expect(byId.get('n2')?.running).toBe(false)
  })

  it('坐标非有限值时归零，避免 Vue Flow 拿到 NaN 后整块画布不渲染', () => {
    const graph = buildMapGraph([node({ id: 'n1', x: Number.NaN, y: Number.POSITIVE_INFINITY })])

    expect(graph.cards[0]).toMatchObject({ x: 0, y: 0 })
  })

  it('分支点优先取节点自己的值，其次取会话元数据', () => {
    const sessions = sessionMap({ id: 's1', branch_from_seq: 7 })

    expect(buildMapGraph([node({ id: 'n1', session_id: 's1', branch_from_seq: 3 })], { sessions }).cards[0].branchFromSeq)
      .toBe(3)
    expect(buildMapGraph([node({ id: 'n2', session_id: 's1' })], { sessions }).cards[0].branchFromSeq)
      .toBe(7)
  })
})

describe('findCardBySession', () => {
  it('按会话 id 找卡片，找不到返回 null', () => {
    const graph = buildMapGraph([
      node({ id: 'n1', session_id: 's1' }),
      node({ id: 'n2', session_id: 's2' }),
    ])

    expect(findCardBySession(graph.cards, 's2')?.id).toBe('n2')
    expect(findCardBySession(graph.cards, 'nope')).toBeNull()
    expect(findCardBySession(graph.cards, '')).toBeNull()
  })
})

describe('ancestorsToExpand', () => {
  it('从近到远列出祖先节点 id', () => {
    const nodes = [
      node({ id: 'root' }),
      node({ id: 'a', parent_node_id: 'root' }),
      node({ id: 'b', parent_node_id: 'a' }),
    ]

    expect(ancestorsToExpand(nodes, 'b')).toEqual(['a', 'root'])
  })

  it('成环时终止，不无限循环', () => {
    const nodes = [
      node({ id: 'a', parent_node_id: 'b' }),
      node({ id: 'b', parent_node_id: 'a' }),
    ]

    expect(ancestorsToExpand(nodes, 'a')).toEqual(['b'])
  })

  it('根节点没有祖先', () => {
    expect(ancestorsToExpand([node({ id: 'root' })], 'root')).toEqual([])
  })
})
