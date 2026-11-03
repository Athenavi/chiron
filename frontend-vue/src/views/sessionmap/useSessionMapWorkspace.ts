/**
 * 画布状态层：加载 / 原子地改节点 / 防抖整体保存。
 *
 * ## 为什么保存是"整体覆盖 + 防抖"
 *
 * 后端从第一天起就是这么设计的（`internal/api/sessionmap.go:11-16`：写入热点是拖节点，
 * 逐字段 PATCH 会把一次拖动拆成几十个请求）。这里沿用：任何一次改动都标记脏，
 * 800ms 静默后提交**整份快照**。因为幂等，重试与合并都不需要额外协议。
 *
 * ## 为什么首帧要读旧 localStorage
 *
 * 老地图把用户拖出来的位置存在 `chiron-map:card-positions:v3`。新数据面的权威来源是
 * 后端 workspace，但在**第一次打开与后端 workspace 落库之间**，画布是空的 ——
 * 此时若完全无视旧值，用户升级后看到所有卡片挤在原点，观感是"我的布局丢了"。
 * 旧 key 的卡片 id 形如 `${sessionId}:turn:${seq}`（一张卡一个回合），与新的
 * 会话级节点不是同一个 id 空间，所以按**会话 id 前缀**回退，取该会话最后一个回合的位置。
 */
import { computed, onScopeDispose, ref } from 'vue'

import { t } from '../../i18n'

import { createWorkspace, deleteWorkspace, getWorkspace, listWorkspaces, saveWorkspace } from './api'
import { appendNode, moveNode, removeNode, toggleNodeCollapsed, type NewNodeInput } from './useMapActions'
import type { MapViewport, SessionMapNode, WorkspaceSummary } from './types'

/** 老地图的拖拽位置键（保留读取，见文件头）。 */
export const LEGACY_POSITIONS_KEY = 'chiron-map:card-positions:v3'
/** 拖动停止后多久提交一次。太快会把后端打满，太慢会让用户以为没保存。 */
export const SAVE_DEBOUNCE_MS = 800

export interface Point {
  x: number
  y: number
}

/**
 * 解析老 key，得到「会话 id → 位置」。
 *
 * 旧值形如 `[[cardId, {x, y}], ...]`，`cardId = ${sessionId}:turn:${seq}`。
 * 同一会话有多个回合时后写覆盖先写 —— 取最后一个回合的位置，正好是用户最后调整过的。
 * 任何畸形条目都跳过：这只是兼容兜底，坏数据不该让画布打不开。
 */
export function legacyPositionsBySession(raw: string | null | undefined): Map<string, Point> {
  const out = new Map<string, Point>()
  if (!raw) return out
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return out
  }
  if (!Array.isArray(parsed)) return out

  for (const item of parsed) {
    if (!Array.isArray(item) || item.length < 2) continue
    const [cardId, position] = item as [unknown, unknown]
    if (typeof cardId !== 'string') continue
    if (!position || typeof position !== 'object') continue
    const { x, y } = position as { x?: unknown; y?: unknown }
    if (typeof x !== 'number' || typeof y !== 'number') continue
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue
    const sessionId = cardId.split(':turn:')[0]
    if (!sessionId) continue
    out.set(sessionId, { x: Math.round(x), y: Math.round(y) })
  }
  return out
}

/**
 * 给后端给不出位置的节点补一个初始坐标。
 *
 * 只在 `x === 0 && y === 0` 时回退：后端存下来的 0,0 是用户真的把卡片拖到原点，
 * 不能覆盖；而"从未摆过"的节点在后端快照里正是 0,0（`smPersist` 不填默认值）。
 */
export function applyLegacyPositions(
  nodes: readonly SessionMapNode[],
  legacy: ReadonlyMap<string, Point>,
): SessionMapNode[] {
  if (legacy.size === 0) return [...nodes]
  return nodes.map(node => {
    if (node.x !== 0 || node.y !== 0) return node
    const point = node.session_id ? legacy.get(node.session_id) : undefined
    return point ? { ...node, x: point.x, y: point.y } : node
  })
}

export interface MapWorkspaceOptions {
  /** 注入 storage 便于测试；隐私模式下可以传 null */
  storage?: Pick<Storage, 'getItem'> | null
  /** 防抖窗口；测试里调小以免等待 */
  debounceMs?: number
}

/**
 * 一张画布的状态与操作。
 *
 * 所有对 `nodes` 的改动都走这里导出的函数，返回的永远是**新数组** ——
 * 于是 Vue 的响应式与"改动即脏"的判定都不需要额外标记。
 */
export function useSessionMapWorkspace(options: MapWorkspaceOptions = {}) {
  const storage =
    options.storage === undefined
      ? (typeof localStorage === 'undefined' ? null : localStorage)
      : options.storage
  const debounceMs = options.debounceMs ?? SAVE_DEBOUNCE_MS

  const workspaces = ref<WorkspaceSummary[]>([])
  const activeId = ref('')
  const nodes = ref<SessionMapNode[]>([])
  const viewport = ref<MapViewport>({})

  const loading = ref(false)
  const loadError = ref('')
  const saveError = ref('')
  const saving = ref(false)

  const activeWorkspace = computed(() => workspaces.value.find(w => w.id === activeId.value) ?? null)
  const activeName = computed(() => activeWorkspace.value?.name ?? '')

  let timer: ReturnType<typeof setTimeout> | null = null
  let dirty = false

  function clearTimer() {
    if (timer) {
      clearTimeout(timer)
      timer = null
    }
  }

  /** 提交当前快照。失败留在 `saveError` 里 —— 静默失败会让人以为布局已保存。 */
  async function flush(): Promise<void> {
    clearTimer()
    if (!activeId.value || !dirty) return
    dirty = false
    saving.value = true
    try {
      await saveWorkspace(activeId.value, {
        name: activeName.value,
        viewport: viewport.value,
        nodes: nodes.value,
      })
      saveError.value = ''
    } catch {
      // 保持脏标记，下次改动会再试一次
      dirty = true
      saveError.value = t('chat.sessionmap.saveFailed')
    } finally {
      saving.value = false
    }
  }

  function scheduleSave() {
    dirty = true
    clearTimer()
    timer = setTimeout(() => {
      timer = null
      void flush()
    }, debounceMs)
  }

  /** 原子改节点：新数组 + 标脏。 */
  function setNodes(next: SessionMapNode[]) {
    nodes.value = next
    scheduleSave()
  }

  function replaceNodes(next: SessionMapNode[], { persist = true } = {}) {
    nodes.value = next
    if (persist) scheduleSave()
    else dirty = false
  }

  async function select(id: string): Promise<void> {
    if (!id) return
    // 切画布前先把未落库的改动写掉，否则会丢在上一张画布上
    if (dirty) await flush()
    activeId.value = id
    loading.value = true
    loadError.value = ''
    try {
      const ws = await getWorkspace(id)
      const legacy = legacyPositionsBySession(storage?.getItem(LEGACY_POSITIONS_KEY) ?? null)
      nodes.value = applyLegacyPositions(ws.nodes, legacy)
      viewport.value = ws.viewport ?? {}
      dirty = false
    } catch {
      loadError.value = t('chat.sessionmap.serviceFailed')
      nodes.value = []
    } finally {
      loading.value = false
    }
  }

  /** 拉列表；一张画布都没有时自动建一张空的，避免用户面对一个不可用的下拉。 */
  async function load(): Promise<void> {
    loading.value = true
    loadError.value = ''
    try {
      workspaces.value = await listWorkspaces()
      if (workspaces.value.length === 0) {
        const created = await createWorkspace(t('chat.sessionmap.defaultWorkspace'))
        if (!created.id) {
          loadError.value = t('chat.sessionmap.serviceFailed')
          return
        }
        workspaces.value = [{ id: created.id, name: created.name }]
        activeId.value = created.id
        nodes.value = created.nodes
        viewport.value = created.viewport ?? {}
        dirty = false
        return
      }
      const keep = workspaces.value.some(w => w.id === activeId.value)
      await select(keep ? activeId.value : workspaces.value[0].id)
    } catch {
      loadError.value = t('chat.sessionmap.serviceFailed')
    } finally {
      loading.value = false
    }
  }

  async function createNamed(name: string): Promise<void> {
    if (dirty) await flush()
    loading.value = true
    try {
      const created = await createWorkspace(name.trim() || t('chat.sessionmap.defaultWorkspace'))
      if (!created.id) {
        loadError.value = t('chat.sessionmap.serviceFailed')
        return
      }
      workspaces.value = [...workspaces.value, { id: created.id, name: created.name }]
      activeId.value = created.id
      nodes.value = created.nodes
      viewport.value = created.viewport ?? {}
      dirty = false
      loadError.value = ''
    } catch {
      loadError.value = t('chat.sessionmap.serviceFailed')
    } finally {
      loading.value = false
    }
  }

  /**
   * 删除当前画布。
   *
   * 删完必须重新加载列表：把已删除的 id 留在下拉里，用户再点一次就是 404。
   */
  async function removeActive(): Promise<void> {
    const id = activeId.value
    if (!id) return
    clearTimer()
    dirty = false
    loading.value = true
    try {
      await deleteWorkspace(id)
      activeId.value = ''
      nodes.value = []
      viewport.value = {}
      workspaces.value = workspaces.value.filter(w => w.id !== id)
      await load()
    } catch {
      loadError.value = t('chat.sessionmap.serviceFailed')
    } finally {
      loading.value = false
    }
  }

  function addNode(input: NewNodeInput) {
    setNodes(appendNode(nodes.value, input))
  }

  function dropNode(nodeId: string) {
    setNodes(removeNode(nodes.value, nodeId))
  }

  function dragNode(nodeId: string, position: Point) {
    setNodes(moveNode(nodes.value, nodeId, position))
  }

  function collapseNode(nodeId: string) {
    setNodes(toggleNodeCollapsed(nodes.value, nodeId))
  }

  function setViewport(next: MapViewport) {
    viewport.value = next
    scheduleSave()
  }

  // 组件卸载时把尾巴写完：否则用户最后一次拖动会丢在关闭浮层的瞬间
  onScopeDispose(() => {
    clearTimer()
    if (dirty) void flush()
  })

  return {
    workspaces,
    activeId,
    activeName,
    nodes,
    viewport,
    loading,
    loadError,
    saveError,
    saving,
    load,
    select,
    createNamed,
    removeActive,
    flush,
    addNode,
    dropNode,
    dragNode,
    collapseNode,
    replaceNodes,
    setViewport,
  }
}
