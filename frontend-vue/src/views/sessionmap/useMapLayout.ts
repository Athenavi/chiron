/**
 * 画布自动布局。
 *
 * 旧实现（`app.js:707-745` `layoutConversationGraph`）把卡片按对话深度排成车道：
 * 每深一层右移一列，同层按下标纵向排开。这里保留同一套几何，因为用户对
 * "越往右越晚"的空间直觉已经形成了，换一套算法等于让所有老用户重新学一遍。
 *
 * 纯函数、无副作用：给定节点列表与尺寸，返回新的坐标映射。
 * 是否写回后端由调用方决定（用户点「整理布局」才写，不自动覆盖手摆的位置）。
 */
import type { SessionMapNode } from './types'

/** 卡片步进。与旧实现的 365/82+276+42 保持同量级，避免升级后画布突然变密。 */
export const LAYOUT_COLUMN_WIDTH = 340
export const LAYOUT_ROW_HEIGHT = 180
/** 根节点起始留白，给左上角的控件让位。 */
export const LAYOUT_ORIGIN_X = 80
export const LAYOUT_ORIGIN_Y = 80

/**
 * 计算每个节点的深度（到根的边数）。
 *
 * 成环时不能无限递归：用 `visiting` 记录当前路径，回边直接按深度 0 处理 ——
 * 环是脏数据，UI 只需要一个有限结果，而卡死整个画布是不可接受的。
 * 父节点不在列表里的节点视作根（与 `buildMapGraph` 的口径一致）。
 */
function depthOf(nodes: Map<string, SessionMapNode>): Map<string, number> {
  const depth = new Map<string, number>()
  const visiting = new Set<string>()

  const visit = (id: string): number => {
    const known = depth.get(id)
    if (known !== undefined) return known
    if (visiting.has(id)) return 0
    const parent = nodes.get(id)?.parent_node_id
    if (!parent || !nodes.has(parent)) {
      depth.set(id, 0)
      return 0
    }
    visiting.add(id)
    const d = visit(parent) + 1
    visiting.delete(id)
    depth.set(id, d)
    return d
  }

  for (const id of nodes.keys()) visit(id)
  return depth
}

/**
 * 按深度分列、同列按出现顺序纵向排开。
 *
 * 同列的顺序沿用**输入顺序**而不是 id 排序：输入来自后端快照，顺序在多次保存之间
 * 是稳定的，于是反复点「整理布局」得到的结果一致（不会每点一次就跳一次位置）。
 */
export function layoutNodes(nodes: readonly SessionMapNode[]): SessionMapNode[] {
  const byId = new Map(nodes.map(n => [n.id, n]))
  const depth = depthOf(byId)
  const laneCursor = new Map<number, number>()

  return nodes.map(node => {
    const column = depth.get(node.id) ?? 0
    const row = laneCursor.get(column) ?? 0
    laneCursor.set(column, row + 1)
    return {
      ...node,
      x: LAYOUT_ORIGIN_X + column * LAYOUT_COLUMN_WIDTH,
      y: LAYOUT_ORIGIN_Y + row * LAYOUT_ROW_HEIGHT,
    }
  })
}
